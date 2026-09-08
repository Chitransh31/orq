"""Helper checks only: no DBGEN, extension downloads, or experiment runner."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import duckdb_controlled as controlled
import plot_duckdb_tpch as plotting


class PureHelperTests(unittest.TestCase):
    def test_shared_grid_rounding(self):
        self.assertEqual(31, len(controlled.TARGETS))
        self.assertEqual(120114, controlled.selected_count('20', 600572))
        self.assertEqual(1, controlled.selected_count('0.025', 2000))
        self.assertEqual(0, controlled.selected_count('0.01', 2000))
        for n in (2000, 15000, 600572):
            ks = [controlled.selected_count(t, n) for t in controlled.TARGETS]
            self.assertEqual(sorted(ks), ks)
            self.assertEqual(n, ks[-1])
        for value in ('NaN', '-1', '101'):
            with self.assertRaises(ValueError):
                controlled.selected_count(value, 100)

    def test_fixed_orders_keep_every_join_condition(self):
        for q in controlled.ORDERS:
            for order in (0, 1):
                sql = controlled.join_clause(q, order)
                for _, _, condition in controlled.EDGES[q]:
                    self.assertEqual(1, sql.count(condition), (q, order, condition))

    def test_quarters_find_hidden_transition_with_equal_endpoints(self):
        calls = []
        def probe(k):
            calls.append(k)
            return {'association': 'b' if 4 <= k <= 6 else 'a'}
        result = controlled.refinement({0: {'association': 'a'}, 10: {'association': 'a'}}, probe, 20)
        self.assertEqual([(3, 4), (6, 7)], [(r['low_count'], r['high_count']) for r in result['transitions']])
        self.assertTrue(all(r['adjacent_rows'] for r in result['transitions']))
        self.assertEqual(len(calls), len(set(calls)))
        limited = controlled.refinement({0: {'association': 'a'}, 100: {'association': 'b'}}, probe, 2)
        self.assertEqual(2, limited['probes'])
        self.assertTrue(limited['budget_exhausted'])

    def test_q3_valid_ties_and_invalid_results(self):
        from datetime import date
        rows = [(i, 10., date(1995, 1, 1), 0) for i in range(11)]
        self.assertTrue(controlled.validate_topn(rows, rows[1:]))
        self.assertFalse(controlled.validate_topn(rows, rows[:9]))
        self.assertFalse(controlled.validate_topn(rows, rows[:9] + [rows[0]]))
        worse = rows + [(12, 1., date(1995, 1, 1), 0)]
        self.assertFalse(controlled.validate_topn(worse, rows[:9] + [worse[-1]]))

    def test_result_tolerance_and_group_identity(self):
        self.assertTrue(controlled.equivalent(5, [('A', 1.)], [('A', 1.000000001)]))
        self.assertFalse(controlled.equivalent(5, [('A', 1.)], [('B', 1.)]))
        self.assertFalse(controlled.equivalent(5, [('A', 1.)], [('A', 1.1)]))

    def test_plot_normalization_requires_baseline_and_full_grid(self):
        rows = [dict(query_id=1, input_mode='direct', control='optimized', point_kind='common',
                     point_id=str(t), target_percent=t, median_seconds=t/10,
                     median_ci_low=t/11, median_ci_high=t/9) for t in (20, 100)]
        result = plotting.plotted_rows({'targets_percent': [20, 100]}, rows)
        self.assertAlmostEqual(.2, result[0]['normalized_median'])
        self.assertEqual('100', result[0]['baseline_point_id'])
        with self.assertRaises(ValueError):
            plotting.plotted_rows({'targets_percent': [20, 100]}, rows[:1])
        with self.assertRaises(ValueError):
            plotting.plotted_rows({'targets_percent': [20, 50, 100]}, rows)


@unittest.skipUnless(importlib.util.find_spec('duckdb'), 'DuckDB package not installed')
class DuckDBHelperTests(unittest.TestCase):
    def setUp(self):
        import duckdb
        import analyze_duckdb_tpch
        self.api = analyze_duckdb_tpch
        self.db = duckdb.connect(':memory:')

    def tearDown(self):
        self.db.close()

    def test_rank_permutation_reproducibility_and_nested_counts(self):
        for table, keys in controlled.KEYS.items():
            columns = ', '.join(f'i AS {key}' for key in keys)
            self.db.execute(f'CREATE TABLE {table} AS SELECT {columns} FROM range(1, 101) t(i)')
        with tempfile.TemporaryDirectory() as folder:
            evidence = controlled.prepare_ranks(self.db, 20260908, self.api, Path(folder))
        for table in controlled.KEYS:
            self.assertEqual(0, evidence[table]['reproduction_mismatches'])
            previous = set()
            for k in (0, 1, 10, 20, 100):
                rows = set(self.db.execute(f'SELECT * FROM controlled_{table} WHERE selectivity_rank <= {k}').fetchall())
                self.assertEqual(k, len(rows))
                self.assertTrue(previous <= rows)
                previous = rows
            first = self.db.execute(controlled.rank_select(table, 1)).fetchall()
            second = self.db.execute(controlled.rank_select(table, 2)).fetchall()
            self.assertNotEqual(first, second)

    def test_sql_only_changes_varied_predicate(self):
        # Canonical scenarios can be rendered without DBGEN or executing queries.
        params = {
            1: {'cutoff': '1998-09-02'},
            3: {'cutoff': '1995-03-15', 'segment': 'BUILDING'},
            5: {'start': '1994-01-01', 'end': '1995-01-01', 'region': 'ASIA'},
            8: {'type_predicate': controlled.REPLACED[8], 'region': 'AMERICA', 'case_nation': 'BRAZIL', 'start': '1995-01-01', 'end': '1996-12-31'},
            9: {'part_predicate': controlled.REPLACED[9]},
        }
        for q, p in params.items():
            base = self.api.query_sql(self.api.Scenario(q, 'canonical', '', 0, '', p))
            for control in ('optimized', 'fixed_a', 'fixed_b'):
                low = controlled.controlled_sql(base, q, 10, 'direct', control)
                high = controlled.controlled_sql(base, q, 20, 'direct', control)
                self.assertEqual(low.replace('<= 10', '<= 20'), high)
                self.assertNotIn(controlled.REPLACED[q], low)
                if q == 3:
                    self.assertIn("o.o_orderdate < DATE '1995-03-15'", low)
                    self.assertIn("c.c_mktsegment = 'BUILDING'", low)
                materialized = controlled.controlled_sql(base, q, 10, 'materialized', control)
                self.assertNotIn('selectivity_rank', materialized)
                self.assertIn('selected_' + controlled.RELATIONS[q], materialized)

    def test_real_sql_equivalence_and_fixed_trees_on_small_schema(self):
        statements = [
            "CREATE TABLE region AS SELECT i AS r_regionkey, CASE WHEN i=0 THEN 'ASIA' ELSE 'AMERICA' END AS r_name FROM range(2) t(i)",
            "CREATE TABLE nation AS SELECT i AS n_nationkey, i AS n_regionkey, CASE WHEN i=0 THEN 'CHINA' ELSE 'BRAZIL' END AS n_name FROM range(2) t(i)",
            "CREATE TABLE customer AS SELECT i AS c_custkey, i%2 AS c_nationkey, 'BUILDING' AS c_mktsegment FROM range(1,21) t(i)",
            "CREATE TABLE supplier AS SELECT i AS s_suppkey, i%2 AS s_nationkey FROM range(1,21) t(i)",
            "CREATE TABLE orders AS SELECT i AS o_orderkey, i AS o_custkey, DATE '1995-02-01' AS o_orderdate, 0 AS o_shippriority FROM range(1,21) t(i)",
            "CREATE TABLE part AS SELECT i AS p_partkey, 'green part' AS p_name, 'ECONOMY ANODIZED STEEL' AS p_type FROM range(1,21) t(i)",
            "CREATE TABLE partsupp AS SELECT i AS ps_partkey, i AS ps_suppkey, 1.0 AS ps_supplycost FROM range(1,21) t(i)",
            "CREATE TABLE lineitem AS SELECT i AS l_orderkey, 1 AS l_linenumber, i AS l_partkey, i AS l_suppkey, DATE '1995-04-01' AS l_shipdate, 2 AS l_quantity, 10.0*i AS l_extendedprice, 0.1 AS l_discount, 0.1 AS l_tax, 'N' AS l_returnflag, 'O' AS l_linestatus FROM range(1,21) t(i)",
        ]
        for sql in statements:
            self.db.execute(sql)
        scenarios = self.api.build_scenarios(self.db, (1,3,5,8,9))
        bases = {s.query_id: self.api.query_sql(s) for s in scenarios if s.canonical}
        with tempfile.TemporaryDirectory() as folder:
            controlled.prepare_ranks(self.db, 20260908, self.api, Path(folder))
        for q, base in bases.items():
            relation = controlled.RELATIONS[q]
            reference = self.db.execute(controlled.controlled_sql(base, q, 10)).fetchall()
            self.db.execute(f'CREATE OR REPLACE TEMP TABLE selected_{relation} AS SELECT * FROM controlled_{relation} WHERE selectivity_rank <= 10')
            self.db.execute(f'ANALYZE selected_{relation}')
            for mode in ('direct', 'materialized'):
                for control in ('optimized', 'fixed_a', 'fixed_b'):
                    sql = controlled.controlled_sql(base, q, 10, mode, control)
                    with self.api.optimizer_mode(self.db, 'optimized' if control == 'optimized' else 'forced'):
                        result = self.db.execute(sql).fetchall()
                        root = self.api.explain_json(self.db, sql, analyze=False)
                    self.assertTrue(controlled.equivalent(q, reference, result), (q, mode, control))
                    if q != 1 and control != 'optimized':
                        fp = controlled.fingerprints(root, self.api)
                        self.assertEqual(controlled.expected_orientation(q, control), fp['orientation'], (q, mode, control))

    def test_physical_fingerprint_sees_unary_operators_and_normalizes_storage(self):
        def scan(name):
            return {'name': 'SEQ_SCAN', 'extra_info': {'Table': name}, 'children': []}
        a = controlled.fingerprints([scan('controlled_part')], self.api)
        b = controlled.fingerprints([scan('selected_part')], self.api)
        self.assertEqual(a, b)
        c = controlled.fingerprints([{'name': 'FILTER', 'children': [scan('controlled_part')]}], self.api)
        self.assertEqual(a['association'], c['association'])
        self.assertNotEqual(a['physical'], c['physical'])


if __name__ == '__main__':
    unittest.main()
