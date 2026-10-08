#!/usr/bin/env python3
"""Validate and report fixed ORQ associations on identically reduced inputs."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics
import math

import tpch_selectivity as model
from report_tpch_plain_and_3pc import load_json_documents, parse_log

PREFIXES = {'TPCH_INPUT':'inputs','TPCH_REDUCTION':'reductions','TPCH_SCOPE':'scopes',
            'TPCH_JOIN':'joins','TPCH_RESULT':'results','TPCH_SHARE':'sharing'}

def stats(values):
    values=list(values)
    if not values:return {'count':0,'median':None,'mad':None,'min':None,'max':None,'stdev':None}
    if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<0 for v in values):
        raise ValueError('invalid measurement')
    med=statistics.median(values)
    return dict(count=len(values),median=med,mad=statistics.median(abs(v-med) for v in values),
                min=min(values),max=max(values),stdev=statistics.stdev(values) if len(values)>1 else None)

def parse_experiment_markers(text):
    result={v:[] for v in PREFIXES.values()};result['errors']=[]
    for line in text.splitlines():
        match=re.search(r'\[(TPCH_\w+)\] (\{.*\})$',line)
        if match and match[1] in PREFIXES:
            try: result[PREFIXES[match[1]]].append(json.loads(match[2]))
            except json.JSONDecodeError:result['errors'].append('malformed '+match[1])
    result['sqlite_passed']='[TPCH_CORRECTNESS] sqlite=passed' in text
    return result

def input_identity(sample):
    return {i['relation']:{k:i[k] for k in ('base_rows','rows','data_seed','sha256','key_sha256','schema')}
            for i in sample['inputs']}

def expected_relations(query):
    return {'q1':{'lineitem'},'q3':{'lineitem','orders','customer'},
            'q5':{'customer','orders','lineitem','supplier','nation','region'},
            'q8':{'part','supplier','lineitem','orders','customer','nation','region'},
            'q9':{'part','supplier','lineitem','partsupp','orders','nation'}}[query]

def validate_sample(directory, slot, config):
    directory=Path(directory); errors=[]
    text=(directory/'run.log').read_text(errors='replace') if (directory/'run.log').exists() else ''
    raw=parse_experiment_markers(text); legacy=parse_log(text);errors+=raw.pop('errors')
    receipt=json.loads((directory/'receipt.json').read_text()) if (directory/'receipt.json').exists() else {}
    if receipt.get('exit_code')!=0: errors.append('missing or failed execution receipt')
    if receipt.get('sample_id')!=slot['sample_id']:errors.append('receipt identity mismatch')
    if 'Returned non-zero exit code' in text:errors.append('generic runner failed')
    variant=slot['variant'];point=slot['point'];query=point['query']
    markers=legacy['query_plan_markers']
    if len(markers)!=1 or any(m['query']!=query or m['variant']!=variant['marker_variant'] or
                             m['association']!=variant['association'] for m in markers):
        errors.append('QUERY_PLAN mismatch or missing/duplicate marker')
    parties=1 if slot['mode']=='same' else 3
    scopes={s.get('party'):s for s in raw['scopes']}
    if len(raw['scopes'])!=parties or set(scopes)!=set(range(parties)):errors.append('missing/duplicate party scopes')
    for scope in scopes.values():
        if scope.get('query')!=query:errors.append('scope query mismatch')
        if variant['association_plan_id'] or query!='q5':
            if scope.get('association')!=variant['association']:errors.append('executed association mismatch')
        try:
            for key in ('input_setup_seconds','query_seconds','workload_seconds','initial_bytes','input_bytes','query_bytes','bytes_sent_total'):
                stats([scope[key]])
                if 'bytes' in key and (isinstance(scope[key],bool) or not isinstance(scope[key],int)):
                    raise ValueError('byte counts must be integers')
            if scope['initial_bytes']+scope['input_bytes']+scope['query_bytes']!=scope['bytes_sent_total']:
                errors.append('byte scopes do not reconcile')
        except (ValueError,KeyError,TypeError):errors.append('invalid scope metrics')
    inputs=raw['inputs'];relations=[i.get('relation') for i in inputs]
    if set(relations)!=expected_relations(query) or len(set(relations))!=len(inputs):errors.append('missing/duplicate input relations')
    for item in inputs:
        if item.get('data_seed')!=config['data_seed']:errors.append('input seed mismatch')
        if any(not re.fullmatch('[0-9a-f]{64}',item.get(key,'')) for key in ('sha256','key_sha256')):errors.append('missing input digest')
        for key in ('base_rows','rows'):
            if isinstance(item.get(key),bool) or not isinstance(item.get(key),int) or item[key]<0:
                raise ValueError('invalid input row count')
        if item.get('relation')==point['relation']:
            if item.get('rows')!=model.selected_count(point['percent'],item.get('base_rows',-1)):
                errors.append('retained count mismatch')
        elif item.get('rows')!=item.get('base_rows'):errors.append('unexpected nonvaried reduction')
    reductions=raw['reductions']
    if len(reductions)!=1:errors.append('expected one public reduction')
    elif (reductions[0].get('relation')!=point['relation'] or reductions[0].get('seed')!=config['selectivity_seed']
          or model.percent(reductions[0].get('percent','-1'))!=model.percent(point['percent'])):
        errors.append('reduction configuration mismatch')
    if reductions and not re.fullmatch('[0-9a-f]{64}',reductions[0].get('base_sha256','')):
        errors.append('missing full varied-input digest')
    if f'TPC-H synthetic data seed: {config["data_seed"]}' not in text:errors.append('binary did not recognize data seed')
    if len(reductions)==1:
        varied=next((item for item in inputs if item['relation']==point['relation']),None)
        if varied and (reductions[0].get('base_rows')!=varied['base_rows'] or reductions[0].get('selected_rows')!=varied['rows']):
            errors.append('reduction/input row counts disagree')
    joins=raw['joins']
    expected_count=7 if query=='q5' and variant['variant']=='original' else variant['association'].count('join(')
    if len(joins)!=expected_count:errors.append('join count mismatch')
    available=expected_relations(query).copy()
    if query=='q8':available.discard('nation');available.update(('nation_region','nation_name'))
    unique={leaf:[set(model.UNIQUE_KEYS[leaf])] for leaf in available}
    for event in joins:
        if event.get('left') not in available or event.get('right') not in available:errors.append('disconnected actual join trace')
        actual=model.normalize_tree(f"join({event.get('left','')},{event.get('right','')})")
        if actual!=event.get('association'):errors.append('join lineage mismatch')
        keys=set(event.get('keys',[]))
        if keys!=model.join_keys(query,event['left'],event['right']):errors.append('join predicate/key contract mismatch')
        left_columns=set(event.get('left_columns',[]));right_columns=set(event.get('right_columns',[]))
        if not keys<=left_columns or not keys<=right_columns:errors.append('join keys missing from input schema')
        left_keys=[key for key in unique.get(event['left'],[]) if key<=left_columns]
        if not any(key<=keys for key in left_keys):errors.append('left input is not proven unique on join keys')
        output_columns=set(event.get('columns',[]))
        inherited=[key for key in unique.get(event['right'],[]) if key<=output_columns]
        if any(key<=keys for key in unique.get(event['right'],[])):
            inherited += [key for key in left_keys if key<=output_columns]
        if query=='q3' and keys=={'[OrderKey]'}:
            inherited.append({'[OrderKey]'})  # Existing fused revenue aggregation, audited in C++ tests.
        unique[actual]=inherited
        available.add(actual)
        if not event.get('keys') or event.get('output_rows',-1)<0:errors.append('missing join keys/size')
    if joins and scopes and joins[-1]['association']!=scopes.get(0,{}).get('association'):errors.append('final lineage mismatch')
    if slot['mode']=='wan':
        relay=legacy['userspace_relays']
        if len(relay)!=3 or {r.get('rank') for r in relay}!={0,1,2}:errors.append('missing relay diagnostics')
        if any(r.get('errors') or (r.get('rank') in (0,1) and (r.get('bytes',0)<=0 or r.get('connections',0)<=0)) for r in relay):
            errors.append('invalid relay traffic/cleanup')
    documents=load_json_documents(directory/'profile.json') if (directory/'profile.json').exists() else []
    groups=[g for d in documents for g in (d if isinstance(d,list) else [d])]
    timings=[sample for g in groups for sample in g.get('raw_data',[])]
    if len(timings)!=1 or not isinstance(timings[0], dict) or 'Overall' not in timings[0]:
        errors.append('missing/duplicate Overall sample')
    for group in groups:
        cfg=group.get('config',{})
        expected_args=['-tpch-seed',str(config['data_seed']),'-tpch-selectivity',point['relation']+'='+point['percent'],
                       '-tpch-selectivity-seed',str(config['selectivity_seed'])]
        if slot['mode']=='wan':expected_args+=['-l','6.5','-w','12']
        if cfg.get('exp_args')!=expected_args or receipt.get('binary_args')!=expected_args:
            errors.append('profile/receipt binary arguments differ from specification')
        definitions=' '.join(cfg.get('cmake_args',[]))
        if ('-DQUERY_PROFILE=1' in definitions)!=(config['phase']=='benchmark'):
            errors.append('profiling/SQLite build phase mismatch')
        if '-DTPCH_SELECTIVITY_EXPERIMENT' not in definitions:errors.append('missing experiment build definition')
        if cfg.get('exp_name')!=variant['target']:errors.append('profile target mismatch')
        if cfg.get('scale_factor')!=float(config['scale_factor']):errors.append('scale factor mismatch')
        if cfg.get('setting')!=slot['mode']:errors.append('setting mismatch')
        if cfg.get('communicator')!=('mpi' if parties==1 else 'nocopy'):errors.append('communicator mismatch')
        if cfg.get('batch_size')!=(-1 if slot['mode']=='wan' else -12):errors.append('batch size mismatch')
        if parties==3 and cfg.get('hosts')!=config['hosts'].split(','):errors.append('host order mismatch')
        if cfg.get('threads')!=config['threads']:errors.append('thread count mismatch')
        if cfg.get('protocol')!=parties:errors.append('protocol mismatch')
        if slot['mode']=='wan' and cfg.get('wan_sim')!='userspace-distributed':errors.append('WAN backend mismatch')
    if config['phase']=='correctness':
        result_path=directory/'result.bin'
        if not raw['sqlite_passed'] or len(raw['results'])!=1 or not result_path.exists():errors.append('missing SQLite/result correctness evidence')
        elif model.sha256(result_path)!=raw['results'][0].get('sha256'):errors.append('result bytes/hash mismatch')
        selection_path=directory/'selection.bin'
        if not selection_path.exists() or model.sha256(selection_path)!=receipt.get('selection_sha256'):
            errors.append('missing or changed selection-index audit')
        elif reductions:
            import struct
            selection_bytes=selection_path.read_bytes()
            if len(selection_bytes)!=reductions[0]['selected_rows']*8:
                errors.append('selection-index length mismatch')
            else:
                indices=[x[0] for x in struct.iter_unpack('<Q',selection_bytes)]
                if len(set(indices))!=len(indices) or indices!=sorted(indices) or any(i>=reductions[0]['base_rows'] for i in indices):
                    errors.append('invalid selection indices')
    result=dict(sample_id=slot['sample_id'],point=point,variant=variant,mode=slot['mode'],block=slot['block'],
                status='success' if not errors else 'incomplete',issues=errors,**raw,
                stage_seconds=timings[0] if len(timings)==1 else {},operator_profile=legacy['operator_profile_seconds'],
                relay_diagnostics=legacy['userspace_relays'],launch_wall_seconds=receipt.get('launch_wall_seconds'),
                executable_sha256=receipt.get('executable_sha256'),selection_sha256=receipt.get('selection_sha256'),raw_dir=str(directory))
    return result

def build_report(output):
    output=Path(output); manifest=json.loads((output/'manifest.json').read_text())
    config=manifest['config'];slots=json.loads((output/'schedule.json').read_text());samples=[]
    expected=model.schedule(config['grid'],config['modes'],config['repetitions'],config['schedule_seed'],point_ids=config.get('point_ids'),block_start=config.get('block_start',0))
    if slots!=expected or not slots:raise ValueError('schedule does not match the complete declared matrix')
    digest=hashlib.sha256(json.dumps(slots,sort_keys=True).encode()).hexdigest()
    if digest!=manifest['schedule_sha256']:raise ValueError('schedule hash mismatch')
    if model.sha256(model.CATALOG)!=manifest['catalog_sha256']:raise ValueError('catalog hash mismatch')
    identity={}; result_identity={}; untouched={}; binaries={}; varied_bases={}; selection_sets={}
    for slot in slots:
        directory=output/slot['sample_id']
        try:sample=validate_sample(directory,slot,config)
        except (KeyError,ValueError,TypeError,OSError) as error:
            sample=dict(sample_id=slot['sample_id'],point=slot['point'],variant=slot['variant'],mode=slot['mode'],
                        block=slot['block'],status='incomplete',issues=['invalid artifact: '+str(error)],inputs=[])
        if sample['status']=='success':
            key=slot['point']['point_id']; observed=input_identity(sample)
            if key in identity and observed!=identity[key]:sample['issues'].append('inputs differ across variants/modes/repetitions')
            else:identity[key]=observed
            reduction=sample['reductions'][0];query=slot['point']['query']
            base=(reduction['base_rows'],reduction['base_sha256'])
            if query in varied_bases and varied_bases[query]!=base:
                sample['issues'].append('full varied input changed across points/variants')
            else:varied_bases[query]=base
            if config['phase']=='correctness':
                import struct
                retained=set(x[0] for x in struct.iter_unpack('<Q',(Path(sample['raw_dir'])/'selection.bin').read_bytes()))
                k=reduction['selected_rows']
                for (other_query,other_k),other_set in selection_sets.items():
                    if other_query==query and not (retained<=other_set if k<=other_k else other_set<=retained):
                        sample['issues'].append('selected inputs are not nested/equal at their counts')
                selection_sets[(query,k)]=retained
            for relation,item in observed.items():
                if relation==slot['point']['relation']:continue
                baseline_key=(slot['point']['query'],relation)
                if baseline_key in untouched and untouched[baseline_key]!=item:
                    sample['issues'].append('nonvaried input changed across selectivity points: '+relation)
                else:untouched[baseline_key]=item
            binary_key=(slot['mode'],slot['variant']['target'])
            binary=sample['executable_sha256']
            if not binary or (binary_key in binaries and binaries[binary_key]!=binary):
                sample['issues'].append('missing or differing executable hash within mode/target')
            else:binaries[binary_key]=binary
            if config['phase']=='correctness':
                digest=sample['results'][0]['sha256']
                if key in result_identity and digest!=result_identity[key]:sample['issues'].append('logical output differs across variants/modes')
                else:result_identity[key]=digest
            if sample['issues']:sample['status']='incomplete'
        samples.append(sample)
    grouped=defaultdict(list)
    for s in samples:grouped[(s['mode'],s['point']['point_id'],s['variant']['target'])].append(s)
    cells=[]
    for (mode,point,target),group in grouped.items():
        valid=[s for s in group if s['status']=='success'];metrics={}
        if valid:
            for metric in ('query_seconds','workload_seconds','input_setup_seconds'):
                metrics[metric]=stats(next(scope[metric] for scope in s['scopes'] if scope['party']==0) for s in valid)
            for metric in ('query_bytes','input_bytes','bytes_sent_total'):
                metrics[metric]=stats(sum(scope[metric] for scope in s['scopes']) for s in valid)
        stages={name:stats(sample['stage_seconds'][name] for sample in valid if name in sample['stage_seconds'])
                for name in sorted({name for sample in valid for name in sample['stage_seconds']})}
        complete=len(valid)==config['repetitions'] and len(group)==config['repetitions']
        cells.append(dict(mode=mode,point_id=point,query=group[0]['point']['query'],target=target,
                          variant=group[0]['variant'],status='success' if complete else 'incomplete',metrics=metrics,stage_seconds=stages,
                          samples=[s['sample_id'] for s in group]))
    comparisons=build_comparisons(cells)
    return dict(schema_version=1,experiment=manifest,points=list({s['point']['point_id']:s['point'] for s in slots}.values()),
                samples=samples,cells=cells,comparisons=comparisons,
                complete=all(s['status']=='success' for s in samples) and all(c['status']=='success' for c in cells))

def build_comparisons(cells):
    grouped=defaultdict(list)
    for c in cells:grouped[(c['mode'],c['point_id'])].append(c)
    output=[]
    point_specs={p["point_id"]:p for preset in ('full','presentation') for p in model.build_grid(preset)}
    for (mode,point),group in grouped.items():
        query=point_specs[point]['query']
        required={v['target'] for v in model.load_catalog()['variants'] if v['query']==query}
        complete=(len(group)==len(required) and {c['target'] for c in group}==required
                  and all(c['status']=='success' for c in group))
        for metric in ('query_seconds','query_bytes','workload_seconds','bytes_sent_total'):
            row=dict(mode=mode,point_id=point,metric=metric,status='success' if complete else 'incomplete')
            if complete:
                best=min(group,key=lambda c:c['metrics'][metric]['median'])
                transferred=[c for c in group if c['variant']['family']=='duckdb-selectivity']
                row['best_target']=best['target']
                row['best_transferred_target']=min(transferred,key=lambda c:c['metrics'][metric]['median'])['target'] if transferred else None
                row['deltas']={}
                row['target_deltas']={}
                point_spec=point_specs[point]
                source=next((c for c in group if point_spec['source_association_plan_id'] and c['variant'].get('association_plan_id')==point_spec['source_association_plan_id']),None)
                row['source_association_plan_id']=point_spec['source_association_plan_id']
                row['source_target']=source['target'] if source else None
                best_value=best['metrics'][metric]['median']
                row['source_regret']=source['metrics'][metric]['median']-best_value if source else None
                for name in ('original','duckdb-canonical'):
                    baseline=next(c for c in group if c['variant']['variant']==name)['metrics'][metric]['median']
                    value=best['metrics'][metric]['median']
                    row['deltas'][name]=dict(improvement=baseline-value,improvement_percent=100*(baseline-value)/baseline if baseline else None,
                                             speedup=baseline/value if value else None)
                    for cell in group:
                        v=cell['metrics'][metric]['median']
                        row['target_deltas'].setdefault(cell['target'],{})[name]=dict(
                            improvement=baseline-v,improvement_percent=100*(baseline-v)/baseline if baseline else None,
                            speedup=baseline/v if v else None)
                row['best_transferred_deltas']=row['target_deltas'].get(row['best_transferred_target'])
                median=best['metrics'][metric]['median'];mad=best['metrics'][metric]['mad']
                row['noisy']=bool(median and mad/median>0.1)
                row['interpretation']='descriptive; three repetitions are not a significance test'
            output.append(row)
    return output

def render_markdown(data):
    cfg=data['experiment']['config']
    lines=['# ORQ selectivity association experiment','',f"Status: {'complete' if data['complete'] else 'incomplete'}; phase `{cfg['phase']}`; SF {cfg['scale_factor']}; {cfg['threads']} threads; {cfg['repetitions']} repetitions.",'',
           'Public MD5 primary-key reduction occurs before sharing. Original ORQ predicates are retained. DuckDB thresholds are DBGEN-specific; source associations are candidates, not predicted ORQ winners.', '',
           ('Profiled query time includes finalization; correctness query time excludes output opening and SQLite checks. '
            'Both exclude input setup. Workload time includes generation, audit, reduction, sharing, and query work; '
            'it excludes runtime/launcher initialization. Total bytes include runtime initialization, while query bytes do not. '
            'Bytes are summed application bytes sent by all parties. Userspace WAN is qualitative simulation.'), '',
           '| Mode | Point | Target | Association | Query median ± MAD (s) | Query bytes median ± MAD | Workload median (s) | Status |',
           '|---|---|---|---|---:|---:|---:|---|']
    def value(cell,key,dispersion=True):
        stat=cell['metrics'].get(key,{})
        if stat.get('median') is None:return '—'
        return f"{stat['median']:.6g}"+(f" ± {stat['mad']:.3g}" if dispersion else '')
    for cell in sorted(data['cells'],key=lambda c:(c['mode'],c['point_id'],c['target'])):
        v=cell['variant'];aid=v['association_plan_id'] or 'original'
        source=f"[{aid}]({model.ROOT/v['raw_dir']/'explain.json'})" if v['raw_dir'] else aid
        lines.append(f"| {cell['mode']} | {cell['point_id']} | {cell['target']} | {source} | {value(cell,'query_seconds')} | {value(cell,'query_bytes')} | {value(cell,'workload_seconds',False)} | {cell['status']} |")
    lines+=['','| Mode | Point | Metric | Best target | Best transferred target | Improvement vs original (%) | Improvement vs canonical (%) |',
            '|---|---|---|---|---|---:|---:|']
    for c in data['comparisons']:
        if c['status']!='success':continue
        def delta(name):
            x=c['deltas'][name]['improvement_percent'];return '—' if x is None else f'{x:.3f}'
        lines.append(f"| {c['mode']} | {c['point_id']} | {c['metric']} | {c['best_target']} | {c['best_transferred_target'] or '—'} | {delta('original')} | {delta('duckdb-canonical')} |")
    lines+=['','| Mode | Point | Metric | Best new target improvement vs original (%) | Best new target improvement vs canonical (%) | Source association | Source regret (metric units) |',
            '|---|---|---|---:|---:|---|---:|']
    for c in data['comparisons']:
        if c['status']!='success':continue
        d=c['best_transferred_deltas']
        def transferred(name):
            x=d[name]['improvement_percent'] if d else None
            return '—' if x is None else f'{x:.3f}'
        regret=c['source_regret']
        lines.append(f"| {c['mode']} | {c['point_id']} | {c['metric']} | {transferred('original')} | {transferred('duckdb-canonical')} | {c['source_association_plan_id']} | {regret if regret is not None else '—'} |")
    lines+=['','## Inputs and physical work','',
            '| Point | Relation | Base rows | Retained rows | Actual percent |', '|---|---|---:|---:|---:|']
    seen=set()
    for sample in data['samples']:
        point=sample['point']
        if point['point_id'] in seen or sample['status']!='success':continue
        item=next(i for i in sample['inputs'] if i['relation']==point['relation']);seen.add(point['point_id'])
        lines.append(f"| {point['point_id']} | {item['relation']} | {item['base_rows']} | {item['rows']} | {100*item['rows']/item['base_rows']:.9g} |")
    lines+=['','Per-stage medians/MAD/range and raw samples, operator categories, and every join input/output length and column encoding are retained in `results.json`. Actual lengths reflect ORQ trimming/padding, not secret validity counts.',
            '', '## Limitations','',
            'All applicable targets are measured at each point. Best-of-all includes baselines by definition; individual target rows must also be inspected for losses. Three repetitions provide limited uncertainty information; retain raw samples and MAD/range, and treat overlapping/noisy outcomes as inconclusive.',
            '', 'Secure filters do not automatically shrink physical buffers. Join output lengths and columns are saved in results.json; DuckDB join_output_rows sums valid join cardinalities and is not an ORQ physical-length estimate. DuckDB materialized timings exclude CTAS+ANALYZE; these results do not measure a full private reduction pipeline.', '', '## Validation failures','']
    failures=[s for s in data['samples'] if s['status']!='success']
    lines+=['- '+s['sample_id']+': '+ '; '.join(s['issues']) for s in failures] or ['None.']
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);args=p.parse_args()
    data=build_report(args.output_dir)
    (args.output_dir/'results.json').write_text(json.dumps(data,indent=2)+'\n')
    (args.output_dir/'report.md').write_text(render_markdown(data))
    from export_tpch_presentation import export_report
    export_report(data,args.output_dir/'presentation')
    return 0 if data['complete'] else 1
if __name__=='__main__':raise SystemExit(main())
