#!/usr/bin/env python3
"""Run immutable, paired fixed-association experiments; reuse ORQ's cluster launcher."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import signal

import tpch_selectivity as model
import report_tpch_selectivity as reporter

ATTEMPT_ARTIFACTS = ("run.log","profile.json","result.bin","selection.bin","receipt.json")

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--grid',choices=['full','smoke','presentation'],default='full')
    p.add_argument('--points', help='Comma-separated exact point IDs; all candidates remain included')
    p.add_argument('--block-start',type=int,default=0,help='First paired repetition ID; use a separate output for extra blocks')
    p.add_argument('--modes',default='same,lan,wan')
    p.add_argument('--phase',choices=['correctness','benchmark'],default='benchmark')
    p.add_argument('--plan-set',choices=['all'],default='all')
    p.add_argument('--hosts',default='zf01,zf02,zf03')
    p.add_argument('--wan-mode',choices=['userspace'],default='userspace')
    p.add_argument('--scale-factor',default='0.1')
    p.add_argument('--threads',type=int,default=16)
    p.add_argument('--repetitions',type=int,default=3)
    p.add_argument('--data-seed',type=int,default=20260818)
    p.add_argument('--selectivity-seed',type=int,default=20260908)
    p.add_argument('--schedule-seed',type=int,default=20260909)
    p.add_argument('--output-dir',type=Path)
    p.add_argument('--correctness-dir',type=Path)
    p.add_argument('--resume',action='store_true')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--no-warmups',action='store_true',help='For correctness/smoke only; full benchmark always warms each target')
    p.add_argument('--timeout-seconds',type=int,default=7200)
    return p

def source_hashes():
    paths=set((model.ROOT/'bench/queries/tpch').glob('*.cpp')) | set((model.ROOT/'bench/queries/tpch').glob('*.h'))
    paths.update(model.ROOT/p for p in ['CMakeLists.txt','scripts/tpch_selectivity.py','scripts/run_tpch_selectivity.py',
        'scripts/report_tpch_selectivity.py','scripts/run-tpch-selectivity-3pc.sh','scripts/run_experiment.py',
        'scripts/report_tpch_plain_and_3pc.py','scripts/tpch-selectivity-plans/manifest.json',
        'scripts/tpch_supervisor.py','scripts/export_tpch_presentation.py','scripts/plot_orq_presentation.py'])
    # Core edits are also part of the executable's provenance.
    paths.update((model.ROOT/'include').rglob('*.h'))
    return {str(p.relative_to(model.ROOT)):model.sha256(p) for p in sorted(paths)}

def binary_args(slot,config):
    args=['-tpch-seed',str(config['data_seed']),'-tpch-selectivity',
          slot['point']['relation']+'='+slot['point']['percent'],
          '-tpch-selectivity-seed',str(config['selectivity_seed'])]
    if slot['mode']=='wan':args+=['-l','6.5','-w','12']
    return args

def command(slot,config):
    extra='-DTPCH_SELECTIVITY_EXPERIMENT,-DINSTRUMENT_TABLES,-DPRINT_COMMUNICATOR_STATISTICS'
    if config['phase']=='benchmark':extra+=',-DQUERY_PROFILE=1'
    argv=['python3',str(model.ROOT/'scripts/run_experiment.py'),'-e','1',*model.MODE_ARGS[slot['mode']],
          '-f',config['scale_factor'],'-T',str(config['threads']),'-m=-DEXTRA='+extra,
          '-a='+shlex.join(binary_args(slot,config))]
    if slot['mode']!='same':argv+=['--hosts',config['hosts']]
    return argv+[slot['variant']['target']]

def write_json(path,data):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,indent=2)+'\n');temporary.replace(path)

def archive_prior_attempt(directory):
    """Preserve any owned artifacts, including a run interrupted before receipt capture."""
    directory=Path(directory)
    present=[directory/name for name in ATTEMPT_ARTIFACTS if (directory/name).exists()]
    if not present:return None
    attempt=directory/('attempt-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    attempt.mkdir()
    for path in present:shutil.move(path,attempt/path.name)
    return attempt

def validate_correctness(config,path,slots):
    if path is None:raise ValueError('benchmark requires --correctness-dir with matching validated reduced-input results')
    proof=reporter.build_report(path)
    pc=proof['experiment']['config']
    for key in ('data_seed','selectivity_seed','scale_factor'):
        if pc[key]!=config[key]:raise ValueError('correctness configuration mismatch: '+key)
    if pc['phase']!='correctness' or proof['experiment']['source_hashes']!=source_hashes():
        raise ValueError('correctness evidence is for different source code or phase')
    if not proof['complete']:raise ValueError('correctness evidence is incomplete')
    approved={(s['point']['point_id'],s['variant']['target']) for s in proof['samples'] if s['status']=='success'}
    required={(s['point']['point_id'],s['variant']['target']) for s in slots}
    if not required<=approved:raise ValueError('correctness evidence does not cover every selected point/target')
    return {s['point']['point_id']:reporter.input_identity(s) for s in proof['samples']}

def preflight(config):
    for tool in ('python3','cmake','make','mpirun'):
        if shutil.which(tool) is None:raise ValueError('missing executable: '+tool)
    if any(m!='same' for m in config['modes']):
        if shutil.which('startmpc') is None:raise ValueError('startmpc must be on PATH')
        for host in config['hosts'].split(','):
            result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',host,
                                   'test -d '+shlex.quote(str(model.ROOT))+' && command -v python3 && nproc'],capture_output=True,text=True)
            if result.returncode:raise ValueError('cluster preflight failed for '+host+': '+result.stderr.strip())

def launch(output,slot,config):
    directory=output/slot['sample_id'];directory.mkdir(parents=True,exist_ok=True)
    # Preserve prior attempts rather than silently replacing failed evidence.
    archive_prior_attempt(directory)
    for name in ('output.json','tpch-result.bin','tpch-selection-'+slot['point']['relation']+'.bin'):
        (model.ROOT/'build'/name).unlink(missing_ok=True)
    argv=command(slot,config);start=time.monotonic()
    print('RUN '+slot['sample_id'],flush=True)
    was_interrupted=False
    with (directory/'run.log').open('w') as log:
        # start_new_session allows a bounded stop of the owned launcher on interruption.
        child=subprocess.Popen(argv,cwd=model.ROOT/'scripts',stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        from tpch_supervisor import process_identity
        write_json(output/'active_process.json',dict(sample_id=slot['sample_id'],process=process_identity(child.pid)))
        try:code=child.wait(timeout=config['timeout_seconds'])
        except (KeyboardInterrupt,subprocess.TimeoutExpired) as error:
            was_interrupted=isinstance(error,KeyboardInterrupt)
            import signal
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
            code=130 if was_interrupted else 124
        write_json(output/'active_process.json',dict(sample_id=slot['sample_id'],process=None))
    for source,dest in [('output.json','profile.json'),('tpch-result.bin','result.bin'),('tpch-selection-'+slot['point']['relation']+'.bin','selection.bin')]:
        path=model.ROOT/'build'/source
        if path.exists():shutil.copyfile(path,directory/dest)
    executable=model.ROOT/'build'/slot['variant']['target']
    receipt=dict(sample_id=slot['sample_id'],exit_code=code,launch_wall_seconds=time.monotonic()-start,
                 binary_args=binary_args(slot,config),command=argv,
                 executable_sha256=model.sha256(executable) if executable.exists() else None,
                 cmake_cache_sha256=model.sha256(model.ROOT/'build/CMakeCache.txt'),
                 selection_sha256=model.sha256(directory/'selection.bin') if (directory/'selection.bin').exists() else None)
    write_json(directory/'receipt.json',receipt)
    if was_interrupted:raise KeyboardInterrupt
    sample=reporter.validate_sample(directory,slot,config)
    if sample['status']!='success':raise ValueError(slot['sample_id']+': '+'; '.join(sample['issues']))
    return sample

def main(argv=None):
    args=parser().parse_args(argv);modes=args.modes.split(',')
    if args.threads<1 or args.repetitions<1 or args.timeout_seconds<1:raise ValueError('positive threads/repetitions/timeout required')
    if any(not 0<=s<=2**64-1 for s in (args.data_seed,args.selectivity_seed,args.schedule_seed)):raise ValueError('seed outside uint64')
    if not 0<float(args.scale_factor)<float('inf'):raise ValueError('positive finite scale factor required')
    if len(args.hosts.split(','))!=3 or any(not __import__('re').fullmatch(r'[A-Za-z0-9._-]+',h) for h in args.hosts.split(',')):
        raise ValueError('exactly three valid hosts required')
    if args.phase=='benchmark' and args.grid in ('full','presentation') and (args.repetitions<3 or args.no_warmups):
        raise ValueError('full/presentation benchmark requires >=3 repetitions and warmups')
    if args.block_start < 0 or (args.grid=='presentation' and args.block_start+args.repetitions>9):
        raise ValueError('invalid block range; presentation supports at most nine blocks')
    point_ids=args.points.split(',') if args.points else None
    catalog=model.load_catalog();slots=model.schedule(args.grid,modes,args.repetitions,args.schedule_seed,catalog,point_ids,args.block_start)
    config={k:getattr(args,k) for k in ('grid','phase','hosts','wan_mode','scale_factor','threads','repetitions','data_seed',
                                      'selectivity_seed','schedule_seed','no_warmups','timeout_seconds')}
    config['modes']=modes
    if point_ids is not None:config['point_ids']=sorted(point_ids)
    if args.block_start:config['block_start']=args.block_start
    if args.dry_run:
        print(f'DRY RUN: {len(slots)} timed/correctness launches; public-pre-sharing reduction; retain-orq-original predicates')
        for slot in slots:print('DRY RUN: '+shlex.join(command(slot,config)))
        return 0
    (model.ROOT/'build').mkdir(exist_ok=True)
    with (model.ROOT/'build/.tpch-selectivity.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return execute(args,config,slots)

def execute(args,config,slots):
    os.environ['PATH']=str(Path.home()/'bin')+os.pathsep+os.environ.get('PATH','')
    preflight(config)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    expected_inputs=validate_correctness(config,args.correctness_dir,slots) if args.phase=='benchmark' else {}
    output=(args.output_dir or model.ROOT/'results/tpch-orq-selectivity'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')).resolve()
    manifest=dict(schema_version=1,config=config,source_hashes=source_hashes(),catalog_sha256=model.sha256(model.CATALOG),
                  schedule_sha256=hashlib.sha256(json.dumps(slots,sort_keys=True).encode()).hexdigest())
    if (output/'manifest.json').exists():
        if not args.resume:raise ValueError('output already exists; use --resume')
        if json.loads((output/'manifest.json').read_text())!=manifest:raise ValueError('resume manifest differs; source/configuration changed')
        if json.loads((output/'schedule.json').read_text())!=slots:raise ValueError('resume schedule changed')
    else:
        if args.resume:raise ValueError('cannot resume without manifest')
        output.mkdir(parents=True,exist_ok=False);write_json(output/'manifest.json',manifest);write_json(output/'schedule.json',slots)
        metadata={'started_at':datetime.now(timezone.utc).isoformat(),'coordinator':os.uname().nodename,
                  'platform':list(os.uname()),'cpus':os.cpu_count(),'predicate_policy':'retain-orq-original',
                  'reduction_policy':'public-pre-sharing','timing_scope':'query and setup-through-finalization workload',
                  'wan_description':'6.5 ms/direction, 12 Gbit/s application cap/link; qualitative userspace relay',
                  'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=model.ROOT,text=True).strip(),
                  'git_diff_sha256':hashlib.sha256(subprocess.check_output(['git','diff','--binary'],cwd=model.ROOT)).hexdigest()}
        write_json(output/'meta.json',metadata)
    progress={'state':'running','total':len(slots),'completed':0,'failed':0,'active_sample':None}
    def update_progress(**changes):
        progress.update(changes);progress['updated_at']=datetime.now(timezone.utc).isoformat()
        write_json(output/'progress.json',progress)
    update_progress()
    warmed=set();last_secure=None
    try:
        for slot in slots:
            if source_hashes()!=manifest['source_hashes']:raise ValueError('source changed during run')
            receipt=output/slot['sample_id']/'receipt.json'
            if args.resume and receipt.exists():
                prior=reporter.validate_sample(receipt.parent,slot,config)
                if prior['status']=='success':
                    update_progress(completed=progress['completed']+1)
                    continue
            if slot['mode']!='same' and last_secure is not None:
                time.sleep(max(0,30-(time.monotonic()-last_secure)))
            warmkey=(slot['mode'],slot['variant']['target'])
            if config['phase']=='benchmark' and not config['no_warmups'] and warmkey not in warmed:
                warm=dict(slot);warm['sample_id']='warmups/'+slot['mode']+'/'+slot['variant']['target']
                warm['point']=next(s['point'] for s in slots if s['mode']==slot['mode'] and s['variant']['target']==slot['variant']['target']) if config['grid']=='presentation' else next(p for p in model.build_grid('smoke') if p['query']==slot['point']['query'])
                warm_receipt=output/warm['sample_id']/'receipt.json'
                valid_warm=False
                if warm_receipt.exists():
                    try:valid_warm=reporter.validate_sample(warm_receipt.parent,warm,config)['status']=='success'
                    except (OSError,ValueError,KeyError,TypeError):pass
                if not valid_warm:
                    update_progress(active_sample=warm['sample_id'])
                    launch(output,warm,config)
                warmed.add(warmkey)
                if slot['mode']!='same':time.sleep(30)
            update_progress(active_sample=slot['sample_id'])
            sample=launch(output,slot,config)
            point=slot['point']['point_id'];identity=reporter.input_identity(sample)
            if point in expected_inputs and identity!=expected_inputs[point]:raise ValueError('input identity differs from correctness/previous variant at '+point)
            expected_inputs[point]=identity
            if slot['mode']!='same':last_secure=time.monotonic()
            write_json(output/'input_audit.json',expected_inputs)
            update_progress(completed=progress['completed']+1,active_sample=None)
    except BaseException as error:
        update_progress(state='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',
                        failed=progress['failed']+1,error=str(error))
        raise
    finally:
        data=reporter.build_report(output);write_json(output/'results.json',data)
        (output/'report.md').write_text(reporter.render_markdown(data))
        from export_tpch_presentation import export_report
        export_report(data,output/'presentation')
        if data['complete']:update_progress(state='completed',active_sample=None)
    print('Report: '+str(output/'report.md'))
    return 0 if data['complete'] else 1
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as error:raise SystemExit(str(error))
