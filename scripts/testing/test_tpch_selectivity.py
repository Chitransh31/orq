"""Focused selectivity protocol tests; no cluster or DBGEN required."""
import copy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import tpch_selectivity as model
import report_tpch_selectivity as report
import run_tpch_selectivity as runner

class GridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog=model.load_catalog()
    def test_exact_counts(self):
        self.assertEqual(model.selected_count('1073500/150143',598660),42803)
        self.assertEqual(model.selected_count('1073525/150143',598660),42804)
        self.assertEqual(model.selected_count('0.5',100),1)
        self.assertEqual(model.selected_count('0',100),0)
        self.assertEqual(model.selected_count('100',0),0)
        for value in ['NaN','-1','101','1/0','1e-2','part=1','1.','100.000000000000000000','18446744073709551616/18446744073709551616']:
            with self.assertRaises(ValueError):model.selected_count(value,100)
    def test_schedule_full_and_smoke(self):
        full=model.schedule(catalog=self.catalog)
        self.assertEqual(1638,len(full));self.assertEqual(1638,len({s['sample_id'] for s in full}))
        self.assertEqual(full,model.schedule(catalog=self.catalog))
        self.assertEqual(20,len(model.schedule('smoke',['lan'],1,catalog=self.catalog)))
        self.assertEqual(35,len(model.build_grid(catalog=self.catalog)))
        for slot in full:
            self.assertEqual(slot['point']['query'],slot['variant']['query'])
    def test_hidden_q8_and_canonical(self):
        q8=[v for v in self.catalog['variants'] if v['query']=='q8']
        self.assertEqual(6,len(q8))
        self.assertEqual('9d0bde819f',next(v for v in q8 if v['variant']=='duckdb-canonical')['association_plan_id'])
        hidden=next(v for v in q8 if v['target']=='q8_selduckdb_d')
        self.assertEqual('350e47ddc5',hidden['association_plan_id'])
        self.assertTrue(all('.refinement.optimized' in p for p in hidden['supporting_points']))
    def test_boundaries_and_small_sf_collisions(self):
        grid=model.build_grid(catalog=self.catalog)
        for q,bounds in self.catalog['boundaries'].items():
            counts={p['source_count'] for p in grid if p['query']==q}
            for low,high in bounds:self.assertTrue({low,high}<=counts)
        self.assertEqual(model.selected_count('4.115',2000),model.selected_count('4.12',2000))
    def test_forced_controls_excluded(self):
        base=dict(query_id='3',input_mode='materialized',point_kind='common',control='optimized',association_plan_id='a',selected_rows='10')
        self.assertEqual({(3,'a'):[10,10]},model.derive_regimes([base,{**base,'control':'fixed_a','selected_rows':'100'}]))
    def test_every_candidate_links_to_optimized_materialized_evidence(self):
        import csv
        source=ROOT/'results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/points.csv'
        with source.open() as f:rows={row['point_id']:row for row in csv.DictReader(f)}
        for variant in self.catalog['variants']:
            if variant['family']!='duckdb-selectivity':continue
            for point_id in variant['supporting_points']:
                row=rows[point_id]
                self.assertEqual(('optimized','materialized'),(row['control'],row['input_mode']))
                self.assertEqual(variant['association_plan_id'],row['association_plan_id'])
                self.assertEqual(variant['association'],row['association_fingerprint'])

    def test_association_grammar_and_hash(self):
        self.assertEqual('join(customer,orders)',model.normalize_tree('join(orders,customer)'))
        self.assertEqual('9e2220c981',model.association_id('join(join(customer,orders),lineitem)'))
        for tree in ['join(join(a,b),c','join(a,b,c)','join(a)','join(a,b))']:
            with self.assertRaises(ValueError):model.normalize_tree(tree)
    def test_tamper_fails(self):
        catalog=copy.deepcopy(self.catalog);key=next(iter(catalog['evidence_sha256']));catalog['evidence_sha256'][key]='0'*64
        with self.assertRaisesRegex(ValueError,'hash mismatch'):model.validate_catalog(catalog)
    def test_binary_argv(self):
        slot=model.schedule('smoke',['same'],1,catalog=self.catalog)[0]
        config=dict(data_seed=20260818,selectivity_seed=20260908,phase='benchmark',scale_factor='0.1',threads=16,hosts='zf01,zf02,zf03')
        cmd=runner.command(slot,config)
        binary=next(v[3:] for v in cmd if v.startswith('-a='))
        self.assertIn('-tpch-seed 20260818',binary);self.assertNotIn('--tpch-seed=',binary)
        self.assertIn('-DQUERY_PROFILE=1',next(v for v in cmd if v.startswith('-m=')))
        self.assertNotIn('--hosts',cmd)
    def test_wan_cost_hints(self):
        slot=model.schedule('smoke',['wan'],1,catalog=self.catalog)[0]
        config=dict(data_seed=20260818,selectivity_seed=20260908)
        self.assertEqual(['-l','6.5','-w','12'],runner.binary_args(slot,config)[-4:])

    def test_dry_run(self):
        result=subprocess.run(['bash',str(ROOT/'scripts/run-tpch-selectivity-3pc.sh'),'--grid','smoke','--modes','lan','--repetitions','1','--dry-run'],capture_output=True,text=True,check=True)
        self.assertEqual(20,len([l for l in result.stdout.splitlines() if l.startswith('DRY RUN: python3')]))

    def test_interrupted_attempt_without_receipt_is_archived(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            (directory/'run.log').write_text('partial run')
            (directory/'selection.bin').write_bytes(b'partial selection')
            attempt=runner.archive_prior_attempt(directory)
            self.assertIsNotNone(attempt)
            self.assertEqual('partial run',(attempt/'run.log').read_text())
            self.assertEqual(b'partial selection',(attempt/'selection.bin').read_bytes())
            self.assertFalse((directory/'run.log').exists())
            self.assertIsNone(runner.archive_prior_attempt(directory))

class ReportTests(unittest.TestCase):
    def test_integer_bytes_and_dispersion(self):
        n=2**60
        self.assertEqual(n+1,report.stats([n,n+1,n+2])['median'])
        self.assertEqual(1,report.stats([n,n+1,n+2])['mad'])
        self.assertIsNone(report.stats([])['median'])
    def test_marker_parse(self):
        parsed=report.parse_experiment_markers('[TPCH_SCOPE] {"party":0}\n[TPCH_SCOPE] {broken}\n[TPCH_CORRECTNESS] sqlite=passed')
        self.assertEqual([{'party':0}],parsed['scopes']);self.assertTrue(parsed['errors']);self.assertTrue(parsed['sqlite_passed'])
    def test_comparisons_and_incomplete(self):
        cells=[]
        for target,family,value in [('original','original',10),('duckdb-canonical','duckdb-canonical',8),('duckdb-selectivity-a','duckdb-selectivity',12)]:
            cells.append(dict(mode='same',point_id='q3-b21500-of600572',target={'original':'q3','duckdb-canonical':'q3_duckdb','duckdb-selectivity-a':'q3_selduckdb_a'}[target],variant={'variant':target,'family':family},status='success',
                metrics={m:report.stats([0 if 'bytes' in m else value]*3) for m in ('query_seconds','query_bytes','workload_seconds','bytes_sent_total')}))
        comparisons=report.build_comparisons(cells)
        self.assertEqual('q3_duckdb',comparisons[0]['best_target'])
        self.assertEqual('q3_selduckdb_a',comparisons[0]['best_transferred_target'])
        self.assertEqual(20,comparisons[0]['deltas']['original']['improvement_percent'])
        self.assertIsNone(comparisons[1]['deltas']['original']['improvement_percent'])
        cells[0]['status']='incomplete'
        self.assertTrue(all(c['status']=='incomplete' for c in report.build_comparisons(cells)))


class ArtifactValidationTests(unittest.TestCase):
    def fixture(self, directory):
        import struct
        catalog=model.load_catalog()
        slot=next(s for s in model.schedule('smoke',['same'],1,catalog=catalog) if s['variant']['target']=='q3_duckdb')
        config=dict(grid='smoke',phase='correctness',modes=['same'],repetitions=1,threads=16,scale_factor='0.1',
                    schedule_seed=20260909,data_seed=20260818,selectivity_seed=20260908,hosts='zf01,zf02,zf03')
        tree=slot['variant']['association'];k=model.selected_count(slot['point']['percent'],598660)
        digest=lambda x:hashlib.sha256(x.encode()).hexdigest()
        inputs=[dict(relation=rel,base_rows=n,rows=k if rel=='lineitem' else n,data_seed=20260818,
                     sha256=digest(rel),key_sha256=digest(rel+'keys'),schema=columns)
                for rel,n,columns in [('lineitem',598660,['[OrderKey]','Revenue']),('orders',150000,['[OrderKey]','[CustKey]','[OrderDate]']),('customer',15000,['[CustKey]'])]]
        reduction=dict(relation='lineitem',base_rows=598660,selected_rows=k,percent=slot['point']['percent'],seed=20260908,base_sha256=digest('base'))
        joins=[dict(association='join(customer,orders)',left='customer',right='orders',keys=['[CustKey]'],
                    left_rows=15000,right_rows=150000,output_rows=150000,left_columns=['[CustKey]'],
                    right_columns=['[CustKey]','[OrderKey]','[OrderDate]'],columns=['[CustKey]','[OrderKey]','[OrderDate]']),
               dict(association=tree,left='join(customer,orders)',right='lineitem',keys=['[OrderKey]'],left_rows=150000,right_rows=k,output_rows=k,
                    left_columns=['[OrderKey]','[OrderDate]','GroupRevenue'],right_columns=['[OrderKey]','Revenue'],columns=['[OrderKey]','[OrderDate]','GroupRevenue'])]
        scope=dict(party=0,query='q3',association=tree,input_setup_seconds=1,query_seconds=2,workload_seconds=3,
                   initial_bytes=0,input_bytes=0,query_bytes=0,bytes_sent_total=0)
        result=b'logical-output';(directory/'result.bin').write_bytes(result)
        (directory/'selection.bin').write_bytes(b''.join(struct.pack('<Q',i) for i in range(k)))
        lines=['TPC-H synthetic data seed: 20260818',f'[QUERY_PLAN] query=q3 variant=duckdb-canonical association={tree}']
        for prefix,items in [('TPCH_INPUT',inputs),('TPCH_REDUCTION',[reduction]),('TPCH_JOIN',joins),('TPCH_SCOPE',[scope]),
                             ('TPCH_RESULT',[dict(query='q3',rows=10,sha256=hashlib.sha256(result).hexdigest())])]:
            lines += [f'[{prefix}] '+json.dumps(item) for item in items]
        lines.append('[TPCH_CORRECTNESS] sqlite=passed');(directory/'run.log').write_text('\n'.join(lines)+'\n')
        args=runner.binary_args(slot,config)
        (directory/'profile.json').write_text(json.dumps(dict(config=dict(exp_name=slot['variant']['target'],scale_factor=0.1,setting='same',communicator='mpi',batch_size=-12,exp_args=args,threads=16,protocol=1,cmake_args=['-DEXTRA=-DTPCH_SELECTIVITY_EXPERIMENT']),raw_data=[{'Overall':2.0}])))
        (directory/'receipt.json').write_text(json.dumps(dict(sample_id=slot['sample_id'],exit_code=0,binary_args=args,
            executable_sha256=digest('binary'),selection_sha256=model.sha256(directory/'selection.bin'))))
        return slot,config
    def test_valid_saved_sample(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);slot,config=self.fixture(directory)
            sample=report.validate_sample(directory,slot,config)
            self.assertEqual('success',sample['status'],sample['issues'])
    def test_missing_parties_wrong_keys_stale_result_and_count(self):
        import tempfile
        mutations=[('"keys": ["[CustKey]"]','"keys": ["[OrderKey]"]','key contract'),
                   ('"selected_rows": 21432','"selected_rows": 21431','selection-index'),
                   ('"data_seed": 20260818','"data_seed": 1','seed mismatch')]
        for old,new,message in mutations:
            with self.subTest(message=message),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);slot,config=self.fixture(directory)
                text=(directory/'run.log').read_text();self.assertIn(old,text);(directory/'run.log').write_text(text.replace(old,new))
                sample=report.validate_sample(directory,slot,config)
                self.assertTrue(any(message in e for e in sample['issues']),sample['issues'])
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);slot,config=self.fixture(directory)
            (directory/'result.bin').write_bytes(b'stale output')
            self.assertIn('result bytes/hash mismatch',report.validate_sample(directory,slot,config)['issues'])
            slot['mode']='lan'
            self.assertIn('missing/duplicate party scopes',report.validate_sample(directory,slot,config)['issues'])
    def test_profile_configuration_must_match_launch(self):
        import tempfile
        for key,value,message in [('exp_name','q9','profile target'),('scale_factor',0.2,'scale factor'),
                                  ('setting','wan','setting'),('communicator','nocopy','communicator'),
                                  ('batch_size',-1,'batch size')]:
            with self.subTest(key=key),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);slot,config=self.fixture(directory)
                profile=json.loads((directory/'profile.json').read_text())
                profile['config'][key]=value
                (directory/'profile.json').write_text(json.dumps(profile))
                sample=report.validate_sample(directory,slot,config)
                self.assertTrue(any(message in issue for issue in sample['issues']))

    def test_corrupt_input_and_byte_metadata(self):
        import tempfile
        for old,new,message in [
            ('"bytes_sent_total": 0','"bytes_sent_total": 0.5','invalid scope'),
            ('"rows": 21432','"rows": 21431','retained count'),
            ('"key_sha256": "','"key_sha256": "bad','input digest')]:
            with self.subTest(message=message),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);slot,config=self.fixture(directory)
                text=(directory/'run.log').read_text()
                (directory/'run.log').write_text(text.replace(old,new))
                sample=report.validate_sample(directory,slot,config)
                self.assertEqual('incomplete',sample['status'])

    def test_missing_schedule_entries_cannot_report_complete(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);slot,config=self.fixture(directory)
            slots=model.schedule('smoke',['same'],1)
            manifest=dict(config=config,schedule_sha256=hashlib.sha256(json.dumps(slots,sort_keys=True).encode()).hexdigest(),catalog_sha256=model.sha256(model.CATALOG))
            (directory/'manifest.json').write_text(json.dumps(manifest));(directory/'schedule.json').write_text(json.dumps(slots[:-1]))
            with self.assertRaisesRegex(ValueError,'complete declared matrix'):report.build_report(directory)
    def test_negative_new_plan_delta_is_retained(self):
        cells=[]
        for name,value,family in [('original',10,'original'),('duckdb-canonical',8,'duckdb-canonical'),('new',12,'duckdb-selectivity')]:
            cells.append(dict(mode='same',point_id='q3-b21500-of600572',target={'original':'q3','duckdb-canonical':'q3_duckdb','new':'q3_selduckdb_a'}[name],variant={'variant':name,'family':family},status='success',
                metrics={m:report.stats([value]*3) for m in ('query_seconds','query_bytes','workload_seconds','bytes_sent_total')}))
        comparisons=report.build_comparisons(cells)
        self.assertEqual(-50,comparisons[0]['best_transferred_deltas']['duckdb-canonical']['improvement_percent'])

if __name__=='__main__':unittest.main()
