"""Local-only presentation and supervision tests; no MPC, SSH, or benchmark binary."""
import copy
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import tpch_selectivity as model
import report_tpch_selectivity as reporter
import run_tpch_selectivity as runner
import tpch_supervisor as supervisor
import export_tpch_presentation as exporter


def fake_report(point_ids=None,block_start=0,repetitions=3):
    """Construct explicit synthetic measurements, never experimental results."""
    catalog=model.load_catalog()
    point_ids=point_ids or ['q8-b7714-of20000']
    slots=model.schedule('presentation',['wan'],repetitions,catalog=catalog,point_ids=point_ids,block_start=block_start)
    samples=[]
    for slot in slots:
        point=slot['point'];target=slot['variant']['target'];n=20000;k=model.selected_count(point['percent'],n)
        value=20 if target.endswith('_a') else 30
        samples.append(dict(slot,status='success',issues=[],raw_dir='/synthetic-test-only/'+slot['sample_id'],
            scopes=[dict(party=party,query_seconds=value+slot['block']%3,workload_seconds=value+2,
                         input_setup_seconds=2,query_bytes=(2**60)+party,input_bytes=party,bytes_sent_total=2**60+2*party,
                         association=slot['variant']['association']) for party in range(3)],
            inputs=[dict(relation=point['relation'],base_rows=n,rows=k,data_seed=20260818,sha256=point['point_id'],key_sha256='keys',schema=['key'])],
            reductions=[dict(base_rows=n,base_sha256='stable-full-input',selected_rows=k)],
            stage_seconds={'Overall':value,'Join':value-2},joins=[],executable_sha256='binary-'+target,
            launch_wall_seconds=value+3,results=[]))
    cells=[]
    for point in point_ids:
        for variant in [v for v in catalog['variants'] if v['query']==point.split('-')[0]]:
            group=[s for s in samples if s['point']['point_id']==point and s['variant']['target']==variant['target']]
            cells.append(dict(mode='wan',point_id=point,query=variant['query'],target=variant['target'],variant=variant,status='success',
                              metrics={key:reporter.stats(exporter.sample_metrics(s)[key] for s in group) for key in exporter.METRICS},
                              stage_seconds={},samples=[s['sample_id'] for s in group]))
    config=dict(grid='presentation',phase='benchmark',modes=['wan'],repetitions=repetitions,block_start=block_start,
                point_ids=point_ids,scale_factor='0.1',threads=16,data_seed=20260818,selectivity_seed=20260908,
                schedule_seed=20260909,hosts='zf01,zf02,zf03')
    return dict(experiment=dict(config=config,source_hashes={'test':'fixture'},catalog_sha256='catalog'),
                points=list({s['point']['point_id']:s['point'] for s in samples}.values()),samples=samples,cells=cells,
                comparisons=reporter.build_comparisons(cells),complete=True)


class MatrixTests(unittest.TestCase):
    def test_exact_matrix_and_fallback(self):
        slots=model.schedule('presentation',['wan'])
        self.assertEqual(159,len(slots))
        self.assertEqual(53,len(model.schedule('presentation',['same'],1)))
        self.assertEqual(20,len({s['variant']['target'] for s in slots}))
        points=[p['point_id'] for p in model.build_grid('presentation') if p['point_id'] not in ('q8-b845-of20000','q9-b1771-of20000')]
        self.assertEqual(123,len(model.schedule('presentation',['wan'],point_ids=points)))
        anchors=[p for p in model.build_grid('presentation') if p['percent']=='100']
        self.assertEqual({'q5','q8','q9'},{p['query'] for p in anchors})
        self.assertTrue(all(p['source_association_plan_id'] is None for p in anchors))
        for n in (0,1,20000,598660):self.assertEqual(n,model.selected_count('100',n))

    def test_subsets_and_extra_blocks(self):
        point='q8-b7714-of20000'
        first=model.schedule('presentation',['wan'],point_ids=[point])
        more=model.schedule('presentation',['wan'],point_ids=[point],block_start=3)
        self.assertEqual({3,4,5},{s['block'] for s in more})
        self.assertFalse({s['sample_id'] for s in first}&{s['sample_id'] for s in more})
        self.assertEqual(more,model.schedule('presentation',['wan'],point_ids=[point],block_start=3))
        for ids in ([],[point,point],['missing']):
            with self.assertRaises(ValueError):model.schedule('presentation',point_ids=ids)

    def test_missing_candidate_never_wins(self):
        data=fake_report()
        self.assertTrue(all(c['status']=='incomplete' for c in reporter.build_comparisons(data['cells'][:-1])))

    def test_correctness_gate_rejects_other_source_or_coverage(self):
        proof=fake_report();proof['experiment']['config']['phase']='correctness'
        cfg=proof['experiment']['config'];slots=model.schedule('presentation',['wan'])
        with mock.patch.object(reporter,'build_report',return_value=proof),mock.patch.object(runner,'source_hashes',return_value={'new':'source'}):
            with self.assertRaisesRegex(ValueError,'different source'):runner.validate_correctness(cfg,Path('/fake'),slots)
        with mock.patch.object(reporter,'build_report',return_value=proof),mock.patch.object(runner,'source_hashes',return_value={'test':'fixture'}):
            with self.assertRaisesRegex(ValueError,'every selected'):runner.validate_correctness(cfg,Path('/fake'),slots)


    def test_full_retention_index_audit(self):
        from test_tpch_selectivity import ArtifactValidationTests
        import struct
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);slot,cfg=ArtifactValidationTests().fixture(path)
            slot['point']=dict(slot['point'],percent='100')
            lines=[]
            for line in (path/'run.log').read_text().splitlines():
                if line.startswith('[TPCH_INPUT] '):
                    item=json.loads(line.split('] ',1)[1])
                    if item['relation']=='lineitem':item.update(base_rows=3,rows=3)
                    line='[TPCH_INPUT] '+json.dumps(item)
                if line.startswith('[TPCH_REDUCTION] '):
                    item=json.loads(line.split('] ',1)[1]);item.update(base_rows=3,selected_rows=3,percent='100')
                    line='[TPCH_REDUCTION] '+json.dumps(item)
                lines.append(line)
            (path/'run.log').write_text('\n'.join(lines)+'\n')
            selection=path/'selection.bin';selection.write_bytes(struct.pack('<QQQ',0,1,2))
            receipt=json.loads((path/'receipt.json').read_text());receipt['binary_args']=runner.binary_args(slot,cfg)
            receipt['selection_sha256']=model.sha256(selection)
            (path/'receipt.json').write_text(json.dumps(receipt))
            profile=json.loads((path/'profile.json').read_text());profile['config']['exp_args']=receipt['binary_args']
            (path/'profile.json').write_text(json.dumps(profile))
            sample=reporter.validate_sample(path,slot,cfg)
            self.assertEqual('success',sample['status'],sample['issues'])
            selection.write_bytes(struct.pack('<QQQ',0,1,3));receipt['selection_sha256']=model.sha256(selection)
            (path/'receipt.json').write_text(json.dumps(receipt))
            self.assertIn('invalid selection indices',reporter.validate_sample(path,slot,cfg)['issues'])

    def test_presentation_no_warmups_rejected_before_launch(self):
        with mock.patch.object(runner,'execute',side_effect=AssertionError('must not launch')):
            with self.assertRaisesRegex(ValueError,'requires'):runner.main(['--grid','presentation','--no-warmups'])


class ExportTests(unittest.TestCase):
    def test_integer_bytes_and_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            exporter.export_report(fake_report(),tmp)
            with (Path(tmp)/'samples.csv').open() as stream:rows=list(csv.DictReader(stream))
            self.assertEqual(3*(2**60)+3,int(rows[0]['query_bytes']))
            claims=(Path(tmp)/'slide_claims.md').read_text()
            self.assertIn('runtime reduction',claims)
            self.assertIn('Descriptive',claims)
            self.assertTrue((Path(tmp)/'physical_work.csv').exists())

    def test_no_claims_for_correctness_or_one_sample(self):
        for data in (fake_report(repetitions=1),fake_report()):
            if data['experiment']['config']['repetitions']==3:data['experiment']['config']['phase']='correctness'
            with tempfile.TemporaryDirectory() as tmp:
                exporter.export_report(data,tmp)
                self.assertIn('No eligible benchmark claims',(Path(tmp)/'slide_claims.md').read_text())

    def test_pair_merge_and_rejections(self):
        first=fake_report();extra=fake_report(block_start=3)
        merged=exporter.merge_reports([first,extra])
        self.assertEqual(6,merged['cells'][0]['metrics']['query_seconds']['count'])
        with self.assertRaisesRegex(ValueError,'overlapping'):exporter.merge_reports([first,first])
        changed=copy.deepcopy(extra);changed['samples'][0]['inputs'][0]['sha256']='different'
        with self.assertRaisesRegex(ValueError,'input identity'):exporter.merge_reports([first,changed])
        changed=copy.deepcopy(extra);changed['experiment']['config']['threads']=8
        with self.assertRaisesRegex(ValueError,'different sources'):exporter.merge_reports([first,changed])
        changed=copy.deepcopy(extra);changed['samples'].pop()
        with self.assertRaisesRegex(ValueError,'unpaired'):exporter.merge_reports([first,changed])

    def test_raw_tamper_prevents_subset_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);slots=model.schedule('presentation',['wan'],point_ids=['q8-b7714-of20000'])
            import hashlib
            config=dict(grid='presentation',modes=['wan'],repetitions=3,schedule_seed=20260909,point_ids=['q8-b7714-of20000'])
            manifest=dict(config=config,schedule_sha256=hashlib.sha256(json.dumps(slots,sort_keys=True).encode()).hexdigest(),catalog_sha256=model.sha256(model.CATALOG))
            (path/'manifest.json').write_text(json.dumps(manifest));(path/'schedule.json').write_text(json.dumps(slots[:-1]))
            with self.assertRaisesRegex(ValueError,'complete declared matrix'):reporter.build_report(path)



class RunnerLifecycleTests(unittest.TestCase):
    def test_resume_skips_valid_samples_and_refuses_changed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'run'
            args=runner.parser().parse_args(['--grid','smoke','--phase','correctness','--modes','same',
                '--repetitions','1','--no-warmups','--output-dir',str(output)])
            config=dict(grid='smoke',phase='correctness',modes=['same'],repetitions=1,threads=16,scale_factor='0.1',
                        data_seed=20260818,selectivity_seed=20260908,schedule_seed=20260909,hosts='zf01,zf02,zf03',
                        no_warmups=True,timeout_seconds=7200)
            slots=model.schedule('smoke',['same'],1)[:1]
            sample=dict(status='success',inputs=[])
            proof=dict(complete=True)
            def launch(directory,slot,cfg):
                path=directory/slot['sample_id'];path.mkdir(parents=True)
                (path/'receipt.json').write_text('{}')
                return sample
            with mock.patch.object(runner,'preflight'),mock.patch.object(runner,'source_hashes',return_value={'source':'one'}), \
                 mock.patch.object(runner.subprocess,'check_output',side_effect=lambda *a,**k:'fixture' if k.get('text') else b'fixture'), \
                 mock.patch.object(runner,'launch',side_effect=launch) as mocked_launch, \
                 mock.patch.object(reporter,'build_report',return_value=proof), \
                 mock.patch.object(reporter,'render_markdown',return_value='test only'), \
                 mock.patch.object(reporter,'validate_sample',return_value=sample), \
                 mock.patch.object(exporter,'export_report'):
                # execute installs handlers; restore after this local mocked invocation.
                handlers={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
                try:
                    self.assertEqual(0,runner.execute(args,config,slots))
                    self.assertEqual(1,mocked_launch.call_count)
                    args.resume=True
                    self.assertEqual(0,runner.execute(args,config,slots))
                    self.assertEqual(1,mocked_launch.call_count)
                    self.assertEqual('completed',json.loads((output/'progress.json').read_text())['state'])
                    changed=dict(config,threads=8)
                    with self.assertRaisesRegex(ValueError,'manifest differs'):runner.execute(args,changed,slots)
                finally:
                    for sig,handler in handlers.items():signal.signal(sig,handler)

@unittest.skipUnless(sys.platform=='linux' and hasattr(os,'pidfd_open'),'requires Linux process identity')
class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.registry=self.root/'registry'
        self.children=[]

    def tearDown(self):
        for d in self.registry.glob('*'):
            if not d.is_dir() or not (d/'spec.json').exists():continue
            status=supervisor.observed_status(d)
            for field in ('supervisor','runner'):
                supervisor.safe_signal(status.get(field),signal.SIGTERM)
        for _ in range(60):
            active=[d for d in self.registry.glob('*') if d.is_dir() and (d/'spec.json').exists() and supervisor.observed_status(d).get('supervisor_alive')]
            if not active:break
            time.sleep(.1)
        # Reap direct dummy supervisors, without waiting for unrelated processes.
        for pid in self.children:
            try:os.waitpid(pid,0)
            except ChildProcessError:pass
        self.tmp.cleanup()

    def spec(self,code,output='output'):
        return dict(kind='dummy-test',command=[sys.executable,'-c',code],cwd=str(self.root),output_dir=str(self.root/output))

    def start(self,name,code):
        result=supervisor.start_run(self.registry,name,self.spec(code,name+'-output'))
        self.children.append(result['supervisor_pid']);return self.registry/name

    def wait_state(self,path,state,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=supervisor.observed_status(path)
            if value['observed_state']==state:return value
            time.sleep(.05)
        self.fail('timed out: '+json.dumps(supervisor.observed_status(path)))

    def test_survives_launcher_exit_and_keeps_log(self):
        code="import sys;sys.path.insert(0,sys.argv[1]);import tpch_supervisor as s,json;print(json.dumps(s.start_run(sys.argv[2],'detached',json.loads(sys.argv[3]))))"
        spec=self.spec("import time;time.sleep(.5);print('dummy completed',flush=True)")
        parent=subprocess.run([sys.executable,'-c',code,str(ROOT/'scripts'),str(self.registry),json.dumps(spec)],capture_output=True,text=True,check=True)
        result=json.loads(parent.stdout)
        status=self.wait_state(Path(result['control_dir']),'completed')
        self.assertEqual(0,status['exit_code'])
        self.assertIn('dummy completed',(Path(result['control_dir'])/'runner.log').read_text())

    def test_failure_and_explicit_resume(self):
        marker=self.root/'once'
        code=f"from pathlib import Path;import sys;p=Path({str(marker)!r});was=p.exists();p.touch();sys.exit(0 if was else 7)"
        path=self.start('retry',code);status=self.wait_state(path,'failed')
        self.assertEqual(7,status['exit_code'])
        result=supervisor.start_run(self.registry,'retry',resume=True);self.children.append(result['supervisor_pid'])
        self.wait_state(path,'completed')
        self.assertTrue(list(path.glob('*-status.json')))
        with self.assertRaisesRegex(ValueError,'completed'):supervisor.start_run(self.registry,'retry',resume=True)

    def test_duplicate_rejection_and_safe_stop(self):
        path=self.start('running',"import time;print('ready',flush=True);time.sleep(20)")
        self.wait_state(path,'running')
        with self.assertRaises(ValueError):supervisor.start_run(self.registry,'running',self.spec('pass'))
        with self.assertRaisesRegex(ValueError,'active'):supervisor.start_run(self.registry,'second',self.spec('pass'))
        self.assertFalse((self.registry/'second').exists())
        state=supervisor.observed_status(path)
        stale=dict(state['supervisor'],start_ticks='wrong')
        self.assertFalse(supervisor.safe_signal(stale,signal.SIGTERM))
        self.assertTrue(supervisor.safe_signal(state['supervisor'],signal.SIGTERM))
        self.wait_state(path,'interrupted')

    def test_dead_process_is_stale_not_success(self):
        control=self.registry/'stale';control.mkdir(parents=True)
        supervisor.write_json(control/'spec.json',dict(self.spec('pass'),host=supervisor.socket.gethostname()))
        supervisor.write_json(control/'status.json',dict(state='running',heartbeat_epoch=0,supervisor=None,runner=None))
        self.assertEqual('stale',supervisor.observed_status(control)['observed_state'])


    def test_orphan_launcher_is_unknown_and_blocks_resume(self):
        output=self.root/'orphan-output';output.mkdir()
        control=self.registry/'orphan';control.mkdir(parents=True)
        supervisor.write_json(control/'spec.json',dict(self.spec('pass','orphan-output'),host=supervisor.socket.gethostname()))
        supervisor.write_json(control/'status.json',dict(state='running',heartbeat_epoch=0,supervisor=None,runner=None))
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
        try:
            supervisor.write_json(output/'active_process.json',dict(process=supervisor.process_identity(child.pid)))
            state=supervisor.observed_status(control)
            self.assertEqual('unknown',state['observed_state']);self.assertTrue(state['workload_alive'])
            with self.assertRaisesRegex(ValueError,'unknown'):supervisor.start_run(self.registry,'orphan',resume=True)
        finally:child.terminate();child.wait()

    def test_source_mismatch_rejects_resume(self):
        path=self.start('source',"import sys;sys.exit(1)");self.wait_state(path,'failed')
        spec=supervisor.read_json(path/'spec.json');spec.update(kind='experiment',source_hashes={'wrong':'source'})
        supervisor.write_json(path/'spec.json',spec)
        with self.assertRaisesRegex(ValueError,'source changed'):supervisor.start_run(self.registry,'source',resume=True)


if __name__=='__main__':unittest.main()
