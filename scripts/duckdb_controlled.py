"""Controlled input-selectivity experiments; invoked by analyze_duckdb_tpch.py.

No work happens on import. Raw artifacts are deliberately independent of plotting.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from pathlib import Path
import platform
import random
import re
import statistics
import time

TARGETS = ('0.01', '0.025', '0.05', '0.1', '0.25', '0.5', '1', '2', '3',
           '5', '7.5', '10', '15', '20', '25', '30', '35', '40', '45', '50',
           '55', '60', '65', '70', '75', '80', '85', '90', '95', '99', '100')
RELATIONS = {1: 'lineitem', 3: 'lineitem', 5: 'orders', 8: 'part', 9: 'part'}
KEYS = {'lineitem': ('l_orderkey', 'l_linenumber'), 'orders': ('o_orderkey',),
        'part': ('p_partkey',)}
REPLACED = {
    1: "l.l_shipdate <= DATE '1998-09-02'",
    3: "l.l_shipdate > DATE '1995-03-15'",
    5: "o.o_orderdate >= DATE '1994-01-01'\n  AND o.o_orderdate < DATE '1995-01-01'",
    8: "p.p_type = 'ECONOMY ANODIZED STEEL'",
    9: "p.p_name LIKE '%green%'",
}
FIXED = {1: [], 3: ["c.c_mktsegment = 'BUILDING'", "o.o_orderdate < DATE '1995-03-15'"],
         5: ["r.r_name = 'ASIA'"],
         8: ["r.r_name = 'AMERICA'", "o.o_orderdate BETWEEN DATE '1995-01-01' AND DATE '1996-12-31'",
             "CASE nation = 'BRAZIL' (expression, not a filter)"], 9: []}
TABLES = {'c': 'customer', 'o': 'orders', 'l': 'lineitem', 'r': 'region',
          'n': 'nation', 'n1': 'nation', 'n2': 'nation', 's': 'supplier',
          'p': 'part', 'ps': 'partsupp'}
ORDERS = {3: ('c o l', 'l o c'), 5: ('r n c o l s', 'o l s c n r'),
          8: ('p l o c n1 r s n2', 'r n1 c o l p s n2'),
          9: ('n s l p o ps', 'p ps l o s n')}
EDGES = {
    3: [('c', 'o', 'c.c_custkey = o.o_custkey'), ('o', 'l', 'o.o_orderkey = l.l_orderkey')],
    5: [('r', 'n', 'r.r_regionkey = n.n_regionkey'), ('n', 'c', 'n.n_nationkey = c.c_nationkey'),
        ('c', 'o', 'c.c_custkey = o.o_custkey'), ('o', 'l', 'o.o_orderkey = l.l_orderkey'),
        ('l', 's', 'l.l_suppkey = s.s_suppkey'), ('n', 's', 'n.n_nationkey = s.s_nationkey')],
    8: [('p', 'l', 'p.p_partkey = l.l_partkey'), ('l', 'o', 'l.l_orderkey = o.o_orderkey'),
        ('o', 'c', 'o.o_custkey = c.c_custkey'), ('c', 'n1', 'c.c_nationkey = n1.n_nationkey'),
        ('n1', 'r', 'n1.n_regionkey = r.r_regionkey'), ('l', 's', 'l.l_suppkey = s.s_suppkey'),
        ('s', 'n2', 's.s_nationkey = n2.n_nationkey')],
    9: [('n', 's', 'n.n_nationkey = s.s_nationkey'), ('s', 'l', 's.s_suppkey = l.l_suppkey'),
        ('l', 'p', 'l.l_partkey = p.p_partkey'), ('l', 'o', 'l.l_orderkey = o.o_orderkey'),
        ('ps', 'l', 'ps.ps_partkey = l.l_partkey'), ('ps', 'l', 'ps.ps_suppkey = l.l_suppkey'),
        # This equality is implied by the original two part-key equalities.
        # It connects the requested part->partsupp prefix; keep all originals too.
        ('p', 'ps', 'p.p_partkey = ps.ps_partkey')],
}
RANK_METHOD = 'row_number ORDER BY md5(seed:stable-key[:stable-key]), stable primary keys; persisted once'


def selected_count(percent, total):
    value = Decimal(str(percent))
    if not value.is_finite() or not 0 <= value <= 100 or total < 0:
        raise ValueError('invalid percentage or table length')
    return int((value * total / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def join_clause(query, order):
    aliases = ORDERS[query][order].split()
    seen = {aliases[0]}
    clause = f'FROM {TABLES[aliases[0]]} AS {aliases[0]}'
    used = []
    for alias in aliases[1:]:
        conditions = [condition for a, b, condition in EDGES[query]
                      if (a == alias and b in seen) or (b == alias and a in seen)]
        if not conditions:
            raise ValueError(f'disconnected fixed order: Q{query} {alias}')
        used.extend(conditions)
        clause += f'\nJOIN {TABLES[alias]} AS {alias} ON ' + ' AND '.join(conditions)
        seen.add(alias)
    assert sorted(used) == sorted(e[2] for e in EDGES[query])
    return clause


def controlled_sql(base, query, count, mode='direct', control='optimized'):
    relation = RELATIONS[query]
    alias = {'lineitem': 'l', 'orders': 'o', 'part': 'p'}[relation]
    predicate = f'{alias}.selectivity_rank <= {count}' if mode == 'direct' else 'TRUE'
    if base.count(REPLACED[query]) != 1:
        raise ValueError(f'canonical predicate drift in Q{query}')
    sql = base.replace(REPLACED[query], predicate)
    if query != 1:
        # The inner FROM is the only FROM followed by a concrete base table.
        sql, changes = re.subn(r'FROM (?:customer|region|part|nation) AS .*?\bWHERE\b',
                              join_clause(query, int(control == 'fixed_b')) + '\nWHERE',
                              sql, count=1, flags=re.S)
        assert changes == 1
    source = f'controlled_{relation}' if mode == 'direct' else f'selected_{relation}'
    return re.sub(rf'\b{relation} AS {alias}\b', f'{source} AS {alias}', sql)


def rank_select(table, seed):
    keys = KEYS[table]
    identity = " || ':' || ".join(f'CAST({key} AS VARCHAR)' for key in keys)
    return (f"SELECT *, row_number() OVER (ORDER BY md5('{int(seed)}:' || {identity}), "
            f"{', '.join(keys)}) AS selectivity_rank FROM {table}")


def prepare_ranks(connection, seed, api, output):
    evidence = {}
    for table, keys in KEYS.items():
        sql = rank_select(table, seed)
        start = time.perf_counter()
        connection.execute(f'CREATE TABLE controlled_{table} AS {sql}')
        connection.execute(f'ANALYZE controlled_{table}')
        elapsed = time.perf_counter() - start
        n, distinct, low, high = connection.execute(
            f'SELECT count(*), count(DISTINCT selectivity_rank), min(selectivity_rank), '
            f'max(selectivity_rank) FROM controlled_{table}').fetchone()
        if n == 0 or (distinct, low, high) != (n, 1, n):
            raise RuntimeError(f'invalid rank permutation for {table}')
        on = ' AND '.join(f'a.{key}=b.{key}' for key in keys)
        mismatch = connection.execute(
            f'SELECT count(*) FROM controlled_{table} a FULL JOIN ({sql}) b ON {on} '
            'WHERE a.selectivity_rank IS DISTINCT FROM b.selectivity_rank').fetchone()[0]
        if mismatch:
            raise RuntimeError(f'ranks not reproducible: {table}')
        evidence[table] = {'rows': n, 'unique_ranks': distinct, 'min_rank': low,
                           'max_rank': high, 'reproduction_mismatches': mismatch,
                           'preparation_seconds': elapsed, 'sql': sql,
                           'nested_selections': 'verified permutation; all predicates are rank <= k'}
    api._write_json(output / 'rank_validation.json', evidence)
    return evidence


def canonicalize_plan(root):
    """Normalize storage table names only; preserve nation roles and physical nodes."""
    if isinstance(root, list):
        return [canonicalize_plan(node) for node in root]
    if not isinstance(root, dict):
        return root
    result = {key: canonicalize_plan(value) for key, value in root.items()}
    if 'Table' in result:
        result['Table'] = re.sub(r'\b(?:controlled_|selected_)(lineitem|orders|part)\b', r'\1', result['Table'])
    return result


def physical_tree(root, api):
    def node(value):
        name = api._operator_name(value).strip().upper()
        relation = api._scan_relation(value, api._nation_scan_count(root) > 1)
        join_type = str((value.get('extra_info') or {}).get('Join Type', ''))
        return [name + (':' + join_type if join_type else ''), relation, [node(c) for c in api._children(value)]]
    return json.dumps([node(n) for n in api._root_nodes(root)], separators=(',', ':'))


def fingerprints(root, api):
    root = canonicalize_plan(root)
    return {'association': api.plan_fingerprints(root)[0],
            'orientation': api.plan_orientation_fingerprint(root),
            'physical': physical_tree(root, api)}


def expected_orientation(query, control):
    aliases = ORDERS[query][int(control == 'fixed_b')].split()
    names = [('nation_region' if a == 'n1' else 'nation_name' if a == 'n2' else TABLES[a])
             for a in aliases]
    value = names[0]
    for name in names[1:]:
        value = f'join({value},{name})'
    return value


def close_value(a, b):
    if isinstance(a, (float, Decimal)) or isinstance(b, (float, Decimal)):
        if a is None or b is None:
            return a is b
        return (math.isnan(float(a)) and math.isnan(float(b))) or math.isclose(float(a), float(b), rel_tol=1e-8, abs_tol=1e-8)
    return a == b


def equivalent(query, expected, actual):
    # Group keys determine identity; sort independently of floating-point ordering.
    key_columns = {1: (0, 1), 3: (0, 2, 3), 5: (0,), 8: (0,), 9: (0, 1)}[query]
    key = lambda row: tuple(row[i] for i in key_columns)
    return len(expected) == len(actual) and all(
        all(close_value(a, b) for a, b in zip(left, right))
        for left, right in zip(sorted(expected, key=key), sorted(actual, key=key)))


def validate_topn(expected_all, actual):
    """Q3 allows arbitrary membership only at equal revenue/orderdate boundaries."""
    if len(actual) != min(10, len(expected_all)):
        return False
    by_key = {row[0]: row for row in expected_all}
    if len({row[0] for row in actual}) != len(actual):
        return False
    for row in actual:
        if row[0] not in by_key or not all(close_value(a, b) for a, b in zip(row, by_key[row[0]])):
            return False
    scores = sorted(expected_all, key=lambda r: (-float(r[1]), r[2]))[:10]
    return all(close_value(a[1], b[1]) and a[2] == b[2] for a, b in zip(scores, actual))


def uncertainty(samples, seed):
    rng = random.Random(seed)
    boot = sorted(statistics.median(rng.choices(samples, k=len(samples))) for _ in range(2000))
    return statistics.median(samples), boot[49], boot[1949]


def refinement(counts, probe, budget):
    """Probe quarters even with equal endpoints, then bisect observed changes."""
    known = dict(counts)
    probes = 0
    for left, right in zip(sorted(counts), sorted(counts)[1:]):
        for numerator in (1, 2, 3):
            k = left + (right - left) * numerator // 4
            if k not in known and probes < budget:
                known[k] = probe(k)
                probes += 1
    while probes < budget:
        intervals = [(a, b) for a, b in zip(sorted(known), sorted(known)[1:])
                     if b - a > 1 and known[a] != known[b]]
        if not intervals:
            break
        for a, b in intervals:
            if probes >= budget:
                break
            k = (a + b) // 2
            known[k] = probe(k)
            probes += 1
    transitions = [{'low_count': a, 'high_count': b, 'adjacent_rows': b-a == 1,
                    'changed_dimensions': [key for key in known[a] if known[a][key] != known[b][key]]}
                   for a, b in zip(sorted(known), sorted(known)[1:]) if known[a] != known[b]]
    return {'probes': probes, 'budget': budget, 'budget_exhausted': probes >= budget,
            'transitions': transitions, 'unchanged_at_all_probed_points': not transitions,
            'search_scope': 'quarters and midpoints of shared-grid intervals, then observed-change bisection; not exhaustive'}


def run(args, api):
    repo = Path(api.__file__).resolve().parents[1]
    output, database = api._prepare_paths(args, repo)
    manifest = {'schema_version': 2, 'experiment': 'TPC-H query structures with controlled input selectivity',
                'sweep_preset': 'controlled-common', 'scale_factor': args.scale_factor,
                'duckdb_version': api.duckdb.__version__, 'threads': args.threads,
                'platform': platform.platform(), 'python_version': platform.python_version(),
                'seed': args.seed, 'rank_generation_method': RANK_METHOD,
                'targets_percent': TARGETS, 'warmups': args.warmups,
                'timing_repetitions': args.timing_repetitions, 'profile_repetitions': args.repetitions,
                'timing_schedule': 'randomized point order; randomized control order in each warmup/timing block per point',
                'timing_scope': 'execute through fetchall, no profiling; materialization and ANALYZE excluded',
                'uncertainty': '95% percentile bootstrap median interval, 2000 resamples, seeded',
                'result_tolerance': {'relative': 1e-8, 'absolute': 1e-8,
                                     'q3': 'compare all groups separately and permit valid Top-N boundary ties'},
                'disabled_optimizers': {'optimized': '', 'fixed_a': 'join_order,build_side_probe_side',
                                        'fixed_b': 'join_order,build_side_probe_side'},
                'database': str(database), 'started_at_utc': datetime.now(timezone.utc).isoformat(),
                'input_modes': ['direct', 'materialized'] if args.input_mode == 'both' else [args.input_mode],
                'include_zero': args.include_zero,
                'max_transition_probes': 0 if args.skip_transition_refinement else args.max_transition_probes,
                'query_definitions': {q: {'varied_relation': RELATIONS[q],
                    'replaced_predicate': REPLACED[q], 'fixed_predicates': FIXED[q],
                    'fixed_orders': ORDERS.get(q, ()), 'denominator': 'original base-table rows'}
                    for q in args.queries},
                'point_manifest': 'points.csv (target/actual percentages, selected rows, plan IDs, raw artifact paths)',
                **api._git_metadata(repo), 'status': 'running'}
    api._write_json(output / 'manifest.json', manifest)
    connection = api.duckdb.connect(str(database))
    points, samples, operators, joins, preparations, searches = [], [], [], [], [], []
    def checkpoint():
        for name, rows, fields in [('points', points, ('point_id', 'query_id', 'input_mode', 'control')),
                                   ('samples', samples, ('point_id', 'block', 'seconds')),
                                   ('operators', operators, api.OPERATOR_FIELDS),
                                   ('joins', joins, api.JOIN_FIELDS),
                                   ('preparations', preparations, ('query_id', 'input_mode', 'selected_rows', 'seconds'))]:
            api.write_csv(output / f'{name}.csv', rows, fields)
        api._write_json(output / 'transition_search.json', searches)
        api._write_json(output / 'manifest.json', manifest)
    try:
        connection.execute('SET threads=1')
        connection.execute('SET enable_progress_bar=false')
        manifest['tpch_extension'] = api.ensure_tpch_extension(connection)
        connection.execute(f'CALL dbgen(sf={args.scale_factor:.15g})')
        connection.execute('ANALYZE')
        legacy = api.build_scenarios(connection, args.queries)
        canonical = {s.query_id: api.query_sql(s) for s in legacy if s.canonical}
        official = dict(connection.execute('SELECT query_nr, query FROM tpch_queries()').fetchall())
        # Canonical results are validated separately from all experimental variants.
        validations = {}
        for q, sql in canonical.items():
            authored = connection.execute(sql).fetchall()
            bundled = connection.execute(official[q]).fetchall()
            match = equivalent(q, authored, bundled)
            if q == 3:
                all_rows = connection.execute(re.sub(r'LIMIT 10\s*$', '', sql)).fetchall()
                match = validate_topn(all_rows, authored) and validate_topn(all_rows, bundled)
            validations[q] = {'matches': match}
            folder = output / 'canonical_validation' / f'q{q:02d}'
            folder.mkdir(parents=True)
            api._write_text(folder / 'custom.sql', sql)
            api._write_text(folder / 'bundled.sql', official[q])
            if not match:
                raise RuntimeError(f'canonical validation failed: Q{q}')
        api._write_json(output / 'canonical_validation.json', validations)
        # Original-predicate sweeps stay separately labelled supporting evidence.
        appendix = []
        for scenario in legacy:
            appendix.extend(api.audit_scenario(connection, scenario))
        api.write_csv(output / 'original_predicate_audits.csv', appendix, api.AUDIT_FIELDS)
        api._write_json(output / 'original_predicate_scenarios.json', [s.__dict__ for s in legacy])
        ranks = prepare_ranks(connection, args.seed, api, output)
        manifest['rank_validation'] = ranks
        rng = random.Random(args.seed)
        modes = ('direct', 'materialized') if args.input_mode == 'both' else (args.input_mode,)
        cache = {}

        def measure(q, input_mode, k, target, kind):
            relation = RELATIONS[q]
            n = ranks[relation]['rows']
            # Different common targets can round to the same k at small scales.
            suffix = f'.p{str(target).replace(".", "p")}' if kind == 'common' else ''
            prefix = f'q{q:02d}.{input_mode}.k{k}.{kind}{suffix}'
            folder = output / 'plans' / prefix
            folder.mkdir(parents=True, exist_ok=True)
            observed = connection.execute(f'SELECT count(*) FROM controlled_{relation} WHERE selectivity_rank <= {k}').fetchone()[0]
            if observed != k:
                raise RuntimeError(f'count audit failed: {prefix}')
            if input_mode == 'materialized':
                prep_sql = f'CREATE OR REPLACE TEMP TABLE selected_{relation} AS SELECT * FROM controlled_{relation} WHERE selectivity_rank <= {k}'
                start = time.perf_counter()
                connection.execute(prep_sql)
                connection.execute(f'ANALYZE selected_{relation}')
                preparations.append({'query_id': q, 'input_mode': input_mode, 'selected_rows': k,
                                     'point_kind': kind, 'seconds': time.perf_counter()-start,
                                     'sql': prep_sql, 'scope': 'CTAS plus ANALYZE'})
                api._write_text(folder / 'prepare.sql', prep_sql + f';\nANALYZE selected_{relation};')
            controls = ['optimized'] if q == 1 else ['optimized', 'fixed_a', 'fixed_b']
            sqls = {c: controlled_sql(canonical[q], q, k, input_mode, c) for c in controls}
            reference_sql = controlled_sql(canonical[q], q, k)
            if q == 3:
                reference_sql = re.sub(r'LIMIT 10\s*$', '', reference_sql)
            with api.optimizer_mode(connection, 'optimized'):
                reference = connection.execute(reference_sql).fetchall()
            per_control = {}
            for control, sql in sqls.items():
                directory = folder / control
                directory.mkdir(exist_ok=True)
                api._write_text(directory / 'query.sql', sql)
                with api.optimizer_mode(connection, 'optimized' if control == 'optimized' else 'forced'):
                    result = connection.execute(sql).fetchall()
                    match = validate_topn(reference, result) if q == 3 else equivalent(q, reference, result)
                    if q == 3:
                        all_result = connection.execute(re.sub(r'LIMIT 10\s*$', '', sql)).fetchall()
                        match = match and equivalent(q, reference, all_result)
                    if not match:
                        raise RuntimeError(f'results differ: {prefix}/{control}')
                    root = api.explain_json(connection, sql, analyze=False)
                    api._write_json(directory / 'explain.json', root)
                    api._write_text(directory / 'explain.txt', api.explain_text(connection, sql, analyze=False))
                    fp = fingerprints(root, api)
                    empty = 'EMPTY_RESULT' in fp['physical']
                    verification = 'not_applicable' if q == 1 or control == 'optimized' else 'verified'
                    if q != 1 and control != 'optimized' and fp['orientation'] != expected_orientation(q, control):
                        if empty:
                            verification = 'empty_plan_elided; order not verifiable'
                        else:
                            raise RuntimeError(f'fixed tree mismatch: {prefix}/{control}: {fp["orientation"]}')
                per_control[control] = {'point_id': prefix + '.' + control, 'query_id': q,
                    'input_mode': input_mode, 'control': control, 'point_kind': kind,
                    'target_percent': float(target), 'selected_rows': k, 'base_rows': n,
                    'actual_percent': 100*k/n, 'varied_relation': relation,
                    'replaced_predicate': REPLACED[q], 'fixed_predicates': json.dumps(FIXED[q]),
                    'rank_generation_method': RANK_METHOD, 'seed': args.seed,
                    'result_equivalent': True, 'result_checksum': api.result_checksum(api.normalized_result(result)),
                    'fixed_order_verification': verification, 'raw_dir': str(directory.relative_to(output)),
                    **{f'{key}_fingerprint': value for key, value in fp.items()},
                    **{f'{key}_plan_id': api._fingerprint_id(value) for key, value in fp.items()}}
            timings = {c: [] for c in controls}
            for block in range(-args.warmups, args.timing_repetitions):
                order = controls.copy()
                rng.shuffle(order)
                for position, control in enumerate(order):
                    with api.optimizer_mode(connection, 'optimized' if control == 'optimized' else 'forced'):
                        start = time.perf_counter()
                        connection.execute(sqls[control]).fetchall()
                        elapsed = time.perf_counter()-start
                    if block >= 0:
                        timings[control].append(elapsed)
                        samples.append({'point_id': per_control[control]['point_id'], 'block': block,
                                        'position': position, 'seconds': elapsed})
            # Profiling is strictly outside all timed repetitions.
            for control in controls:
                row = per_control[control]
                row['median_seconds'], row['median_ci_low'], row['median_ci_high'] = uncertainty(timings[control], args.seed)
                join_outputs = []
                estimates = []
                for repetition in range(args.repetitions):
                    with api.optimizer_mode(connection, 'optimized' if control == 'optimized' else 'forced'):
                        analyzed = api.explain_json(connection, sqls[control], analyze=True)
                    directory = output / row['raw_dir']
                    api._write_json(directory / f'analyze-{repetition}.json', analyzed)
                    normalized = canonicalize_plan(analyzed)
                    op = api.flatten_plan(normalized, query_id=q, scenario=prefix, mode=control)
                    for item in op:
                        item.update(run_id=row['point_id'], repetition=repetition)
                    operators.extend(op)
                    jr = api._join_rows(normalized, row['point_id'], q, prefix, control)
                    for item in jr:
                        item['repetition'] = repetition
                    joins.extend(jr)
                    join_outputs.append(sum(item['actual_cardinality'] or 0 for item in jr))
                    # Scan/filter estimates can include pushed join filters: keep their scope explicit.
                    estimates.extend(item['estimated_cardinality'] for item in op
                                     if item['relation_set'] == relation and item['estimated_cardinality'] is not None
                                     and ('SCAN' in item['operator_name'] or 'FILTER' in item['operator_name']))
                row['join_output_rows'] = statistics.median(join_outputs)
                row['varied_scan_estimated_rows'] = min(estimates) if estimates else None
                row['estimate_scope'] = 'lowest varied-relation scan/filter estimate; may include pushed join filters'
                # Standalone predicate estimate isolates the experimental filter from join pushdown.
                source = f'controlled_{relation}' if input_mode == 'direct' else f'selected_{relation}'
                predicate_sql = f'SELECT * FROM {source}' + (f' WHERE selectivity_rank <= {k}' if input_mode == 'direct' else '')
                with api.optimizer_mode(connection, 'optimized'):
                    predicate_plan = api.explain_json(connection, predicate_sql, analyze=False)
                api._write_json(output / row['raw_dir'] / 'predicate-explain.json', predicate_plan)
                predicate_ops = api.flatten_plan(predicate_plan, query_id=q, scenario=prefix, mode=control)
                row['estimated_qualifying_rows'] = next((op['estimated_cardinality'] for op in predicate_ops if op['estimated_cardinality'] is not None), 0 if 'EMPTY_RESULT' in str(predicate_plan) else None)
                points.append(row)
            cache[q, input_mode, k] = {key: per_control['optimized'][f'{key}_fingerprint'] for key in ('association', 'orientation', 'physical')}
            checkpoint()
            return cache[q, input_mode, k]

        jobs = [(q, mode, target, 'common') for q in args.queries for mode in modes for target in TARGETS]
        if args.include_zero:
            jobs += [(q, mode, '0', 'boundary') for q in args.queries for mode in modes]
        rng.shuffle(jobs)
        for q, mode, target, kind in jobs:
            print(f'Q{q} {mode} {target}% [{kind}]', flush=True)
            measure(q, mode, selected_count(target, ranks[RELATIONS[q]]['rows']), target, kind)
        for q in args.queries:
            for mode in modes:
                n = ranks[RELATIONS[q]]['rows']
                endpoints = {selected_count(t, n): cache[q, mode, selected_count(t, n)] for t in TARGETS}
                def probe(k):
                    print(f'Q{q} {mode} transition probe k={k}', flush=True)
                    return measure(q, mode, k, 100*k/n, 'refinement')
                search = refinement(endpoints, probe, 0 if args.skip_transition_refinement else args.max_transition_probes)
                searches.append({'query_id': q, 'input_mode': mode, **search})
        catalog = []
        for q in args.queries:
            for mode in modes:
                groups = {}
                for row in points:
                    if row['query_id'] == q and row['input_mode'] == mode:
                        groups.setdefault(row['association_plan_id'], []).append(row)
                for plan_id, rows in groups.items():
                    representative = sorted(rows, key=lambda r: r['selected_rows'])[len(rows)//2]
                    catalog.append({'query_id': q, 'input_mode': mode, 'association_plan_id': plan_id,
                                    'association': representative['association_fingerprint'],
                                    'representative': representative,
                                    'supporting_point_ids': [r['point_id'] for r in rows]})
        api._write_json(output / 'orq_candidates.json', {'candidates': catalog,
            'limitations': 'ORQ must measure physical table lengths, widths, sorting work, communication, and validity counts. Filters do not automatically shrink secure tables. Materialized DuckDB costs do not establish cheap ORQ reductions. ORQ implementation is outside this phase.'})
        manifest.update(status='complete', finished_at_utc=datetime.now(timezone.utc).isoformat(),
                        point_count=len(points), canonical_validation=validations)
        api._write_text(output / 'report.md', '# Controlled input selectivity\n\n'
            'TPC-H query structures with controlled input selectivity. See manifest.json for protocol, '
            'points.csv for row-count rounding and timing intervals, samples.csv for unprofiled samples, '
            'and plans/ for exact SQL and complete plans.\n\n'
            'transition_search.json reports unchanged cases, search budgets and observed transitions. '
            'The search is not exhaustive. Fixed orders constrain association and build/probe orientation; '
            'empty plans may elide joins and are explicitly marked unverified.\n\n'
            'Preparation costs are in preparations.csv; query-only times exclude these costs. '
            'rank_validation.json records one-time rank preparation.\n\n'
            'Regenerate figures: `python3 scripts/plot_duckdb_tpch.py <this-directory>`.\n\n'
            '## Original-predicate appendix\n\n'
            'original_predicate_scenarios.json and original_predicate_audits.csv document original '
            'sweeps and attainable percentages; canonical_validation/ contains separate bundled-query validation. '
            'Run the legacy preset for their complete profiles.\n\n'
            '## ORQ handoff\n\n'
            'orq_candidates.json links observed associations, representative selectivities, intermediate rows '
            'and timings. Measure physical table lengths, widths, sorting, communication and validity counts '
            'in ORQ: filters do not automatically shrink secure tables. DuckDB materialization motivates '
            'experiments but does not establish equivalent ORQ reductions or end-to-end benefits.')
        checkpoint()
    except Exception as exc:
        manifest.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        checkpoint()
        raise
    finally:
        connection.close()
    print(f'Artifacts saved: {output}. Plot with scripts/plot_duckdb_tpch.py', flush=True)
    return 0
