#!/usr/bin/env python3
"""Export evidence from saved reports only; never builds or launches a workload."""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path

import tpch_selectivity as model
import report_tpch_selectivity as report

METRICS = ('query_seconds','workload_seconds','input_setup_seconds','query_bytes','input_bytes','bytes_sent_total')


def write_csv(path, rows, fields):
    with Path(path).open('w', newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)


def sample_metrics(sample):
    if sample['status']!='success':return {}
    coordinator=next(s for s in sample['scopes'] if s['party']==0)
    return {key:sum(s[key] for s in sample['scopes']) if 'bytes' in key else coordinator[key]
            for key in METRICS}


def merge_reports(reports):
    """Combine disjoint batches/extra blocks, refusing duplicate or changed evidence."""
    if len(reports)==1:return reports[0]
    base=reports[0]['experiment']
    ignored={'point_ids','repetitions','block_start'}
    stable=lambda cfg:{k:v for k,v in cfg.items() if k not in ignored}
    samples=[];seen=set();identities={};binaries={};points={};results={};bases={};untouched={}
    for data in reports:
        exp=data['experiment']
        if (stable(exp['config'])!=stable(base['config']) or exp['source_hashes']!=base['source_hashes']
                or exp['catalog_sha256']!=base['catalog_sha256']):
            raise ValueError('cannot pool different sources/configurations/catalogs')
        for sample in data['samples']:
            key=(sample['mode'],sample['point']['point_id'],sample['variant']['target'],sample['block'])
            if key in seen:raise ValueError('overlapping sample blocks; do not count a run twice')
            seen.add(key);samples.append(sample);points[sample['point']['point_id']]=sample['point']
            if sample['status']!='success':continue
            point=sample['point']['point_id'];identity=report.input_identity(sample)
            if point in identities and identity!=identities[point]:raise ValueError('input identity changed between batches')
            identities[point]=identity
            query=sample['point']['query'];reduction=sample['reductions'][0]
            full=(reduction['base_rows'],reduction['base_sha256'])
            if query in bases and bases[query]!=full:raise ValueError('full varied input changed between batches')
            bases[query]=full
            for relation,item in identity.items():
                if relation==sample['point']['relation']:continue
                k=(query,relation)
                if k in untouched and untouched[k]!=item:raise ValueError('nonvaried input changed between batches')
                untouched[k]=item
            k=(sample['mode'],sample['variant']['target'])
            if k in binaries and binaries[k]!=sample['executable_sha256']:raise ValueError('binary changed between batches')
            binaries[k]=sample['executable_sha256']
            if exp['config']['phase']=='correctness':
                digest=sample['results'][0]['sha256']
                if point in results and results[point]!=digest:raise ValueError('outputs changed between batches')
                results[point]=digest
    grouped=defaultdict(list)
    for sample in samples:grouped[(sample['mode'],sample['point']['point_id'],sample['variant']['target'])].append(sample)
    paired=defaultdict(list)
    for (mode,point,target),group in grouped.items():paired[(mode,point)].append({s['block'] for s in group})
    for blocks in paired.values():
        if any(b!=blocks[0] for b in blocks) or len(blocks[0])>9:raise ValueError('unpaired or excessive repetition blocks')
    cells=[]
    for (mode,point,target),group in grouped.items():
        valid=[s for s in group if s['status']=='success']
        metrics={key:report.stats(sample_metrics(s)[key] for s in valid) for key in METRICS}
        stages={name:report.stats(s['stage_seconds'][name] for s in valid if name in s['stage_seconds'])
                for name in {name for s in valid for name in s['stage_seconds']}}
        cells.append(dict(mode=mode,point_id=point,query=group[0]['point']['query'],target=target,
                          variant=group[0]['variant'],status='success' if len(valid)==len(group) else 'incomplete',
                          metrics=metrics,stage_seconds=stages,samples=[s['sample_id'] for s in group]))
    comparisons=report.build_comparisons(cells)
    return dict(schema_version=1,experiment=base,source_experiments=[d['experiment'] for d in reports],
                samples=samples,cells=cells,points=list(points.values()),comparisons=comparisons,
                complete=all(d['complete'] for d in reports) and all(c['status']=='success' for c in comparisons))


def export_report(data, directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    points={p['point_id']:p for p in data['points']}
    samples=[];physical=[];stages=[];identities=[]
    for sample in data['samples']:
        point=sample['point']
        common=dict(sample_id=sample['sample_id'],query=point['query'],point_id=point['point_id'],
                    target=sample['variant']['target'],mode=sample['mode'],block=sample['block'],
                    status=sample['status'],raw_dir=sample.get('raw_dir',''))
        varied=next((i for i in sample.get('inputs',[]) if i['relation']==point['relation']),{})
        samples.append(dict(common,requested_percent=point['percent'],relation=point['relation'],
                            actual_percent=100*varied['rows']/varied['base_rows'] if varied.get('base_rows') else None,
                            retained_rows=varied.get('rows'),base_rows=varied.get('base_rows'),
                            association=sample['variant']['association'],
                            source_assignment=point['source_assignment'],input_sha256=varied.get('sha256'),
                            launch_wall_seconds=sample.get('launch_wall_seconds'),**sample_metrics(sample)))
        for index,event in enumerate(sample.get('joins',[])):
            physical.append(dict(common,join_index=index,**event))
        for name,value in sample.get('stage_seconds',{}).items():stages.append(dict(common,stage=name,seconds=value))
        for item in sample.get('inputs',[]):identities.append(dict(common,**item))
    common_fields=['sample_id','query','point_id','target','mode','block','status','raw_dir']
    write_csv(directory/'samples.csv',samples,common_fields+['relation','requested_percent','actual_percent',
              'base_rows','retained_rows','association','source_assignment','input_sha256',*METRICS,'launch_wall_seconds'])
    def structured_csv(name,rows,leading):
        fields=leading+sorted({k for r in rows for k in r}-set(leading))
        write_csv(directory/name,[{k:json.dumps(v,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows],fields)
    structured_csv('physical_work.csv',physical,common_fields+['join_index'])
    structured_csv('inputs.csv',identities,common_fields)
    write_csv(directory/'stages.csv',stages,common_fields+['stage','seconds'])
    cells={(c['mode'],c['point_id'],c['target']):c for c in data['cells']}
    rows=[];repeat=[];claims=[]
    for comparison in data['comparisons']:
        mode= comparison['mode'];point=comparison['point_id'];metric=comparison['metric']
        if comparison['status']!='success':
            rows.append(dict(mode=mode,point_id=point,metric=metric,status='incomplete'));continue
        for target,baselines in comparison['target_deltas'].items():
            cell=cells[(mode,point,target)];measurement=cell['metrics'][metric]
            for baseline,delta in baselines.items():
                base=next(c for c in data['cells'] if c['mode']==mode and c['point_id']==point and c['variant']['variant']==baseline)
                bs=base['metrics'][metric]
                overlap=measurement['min']<=bs['max'] and bs['min']<=measurement['max']
                noisy=bool(measurement['median'] and measurement['mad']/measurement['median']>0.1)
                row=dict(mode=mode,query=cell['query'],point_id=point,metric=metric,target=target,baseline=base['target'],
                         status='success',count=measurement['count'],median=measurement['median'],mad=measurement['mad'],
                         minimum=measurement['min'],maximum=measurement['max'],baseline_median=bs['median'],
                         overlap=overlap,noisy=noisy,source_assignment=points[point]['source_assignment'],**delta)
                rows.append(row)
                if metric=='query_seconds' and target!=base['target']:
                    if overlap or noisy:
                        repeat.append(dict(mode=mode,point_id=point,target=target,baseline=base['target'],
                                           count=measurement['count'],action='add three blocks for ALL candidates' if measurement['count']<9 else 'report inconclusive',
                                           reason='observed ranges overlap' if overlap else 'MAD/median exceeds 10%'))
                    if measurement['count']>=3 and data['experiment']['config']['phase']=='benchmark':
                        speed=delta['speedup'];pct=delta['improvement_percent']
                        if speed is not None and pct is not None:
                            claims.append(f"| 6/7 | {cell['query']} {point}, {mode}: {target} median {measurement['median']:.3f} s versus {base['target']} {bs['median']:.3f} s; speedup {speed:.3f}×; runtime reduction {pct:.2f}%. | {measurement['count']} | {'Uncertain difference; descriptive only' if overlap or noisy else 'Descriptive comparison; not a significance test'} | {cell['samples']} |")
    write_csv(directory/'comparisons.csv',rows,['mode','query','point_id','metric','target','baseline','status','count',
              'median','mad','minimum','maximum','baseline_median','improvement','improvement_percent','speedup','overlap','noisy','source_assignment'])
    write_csv(directory/'repeat_recommendations.csv',repeat,['mode','point_id','target','baseline','count','action','reason'])
    (directory/'slide_claims.md').write_text('\n'.join([
        '# Measured claim ledger','',
        f"Dataset: phase {data['experiment']['config']['phase']}; SF {data['experiment']['config']['scale_factor']}; overall complete: {data['complete']}.",
        'Rows below are descriptive candidate comparisons, including losses. Missing combinations never enter winner claims.',
        'Public reduction precedes sharing; original ORQ predicates remain. Source assignments may be inferred. No end-to-end private compaction claim.',
        'Speedup = baseline median / candidate median. Runtime reduction = 100 × (baseline − candidate) / baseline.',
        'Historical means must remain separate. CSV/raw_dir fields link each observation to saved evidence.', '',
        '| Slide | Measured sentence | n | Qualification | Sample IDs |','|---|---|---:|---|---|',
        *claims, '' if claims else 'No eligible benchmark claims yet. Correctness/single-observation smoke data are not slide-ready speedup evidence.',
        '', 'See samples.csv, comparisons.csv, physical_work.csv, stages.csv, inputs.csv and repeat_recommendations.csv.','']) )
    (directory/'plot_data.json').write_text(json.dumps(data,indent=2)+'\n')
    (directory/'README.md').write_text('''# Presentation exports

No experiment is launched by this exporter. Figures require matplotlib and are generated separately:

    .venv-duckdb-experiment/bin/python scripts/plot_orq_presentation.py PATH/TO/presentation

The plotter writes SVG, PDF and PNG. Each new run's exports remain separate from historical plots.
Physical work exports contain join lengths/widths and stage observations, not private intermediate cardinalities.
Repetition recommendations are requests for complete paired blocks, never automatic launches.
''')
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('runs',nargs='+',type=Path)
    p.add_argument('--output-dir',type=Path,required=True)
    args=p.parse_args()
    export_report(merge_reports([report.build_report(path) for path in args.runs]),args.output_dir)


if __name__=='__main__':main()
