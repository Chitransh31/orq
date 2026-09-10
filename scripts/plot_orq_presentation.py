#!/usr/bin/env python3
"""Render saved ORQ presentation evidence. Never executes queries."""
import argparse
import json
from pathlib import Path
import re


def plot(directory):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    directory=Path(directory);data=json.loads((directory/'plot_data.json').read_text())
    cfg=data['experiment']['config']
    if cfg['phase']!='benchmark':raise ValueError('performance plots require benchmark evidence')
    eligible={(c['mode'],c['point_id']) for c in data['comparisons'] if c['status']=='success' and c['metric']=='query_seconds'}
    cells=[c for c in data['cells'] if (c['mode'],c['point_id']) in eligible and c['metrics']['query_seconds']['count']>=3]
    if not cells:raise ValueError('no complete comparisons with at least three repetitions')
    figures=directory/'figures';figures.mkdir(exist_ok=True)
    points={p['point_id']:p for p in data['points']}
    colors=plt.get_cmap('tab10')
    def color(target,targets):
        if re.fullmatch(r'q[0-9]+',target):return '#777777'
        if target.endswith('_duckdb'):return '#E69F00'
        return colors((targets.index(target)+1)%10)
    def finish(fig,name,note='medians, raw observations and observed ranges'):
        fig.suptitle(f"SF {cfg['scale_factor']} · public input reduction · {note}",fontsize=10)
        fig.tight_layout(rect=(0,0,1,.96))
        for ext in ('svg','pdf','png'):fig.savefig(figures/(name+'.'+ext),dpi=220)
        plt.close(fig)
    def bars(ax,group,metric):
        group=sorted(group,key=lambda c:c['target']);targets=[c['target'] for c in group]
        for i,c in enumerate(group):
            m=c['metrics'][metric];v=m['median'];scale=1e9 if 'bytes' in metric else 1
            ax.bar(i,v/scale,color=color(c['target'],targets))
            ax.errorbar(i,v/scale,yerr=[[(v-m['min'])/scale],[(m['max']-v)/scale]],color='black',capsize=3)
            for s in data['samples']:
                if s['status']=='success' and s['mode']==c['mode'] and s['point']['point_id']==c['point_id'] and s['variant']['target']==c['target']:
                    value=sum(p[metric] for p in s['scopes']) if 'bytes' in metric else next(p[metric] for p in s['scopes'] if p['party']==0)
                    ax.plot(i,value/scale,'k.',markersize=4)
        ax.set_xticks(range(len(group)),targets,rotation=35,ha='right')
        ax.set_ylabel('Query bytes sent (GB, 10⁹)' if 'bytes' in metric else 'Query time (seconds)')
    for mode in sorted({c['mode'] for c in cells}):
        fig,axes=plt.subplots(1,3,figsize=(13,4.8))
        for ax,q in zip(axes,('q5','q8','q9')):
            group=[c for c in cells if c['mode']==mode and c['query']==q and points[c['point_id']]['percent']=='100']
            if group:bars(ax,group,'query_seconds')
            else:ax.text(.5,.5,'No completed 100% comparison',ha='center',transform=ax.transAxes)
            ax.set_title(q.upper()+' · '+mode)
        finish(fig,'full-input-'+mode)
        fig,axes=plt.subplots(1,2,figsize=(12,4.8))
        for ax,q in zip(axes,('q8','q9')):
            group=[c for c in cells if c['query']==q and c['mode']==mode]
            targets=sorted({c['target'] for c in group})
            for target in targets:
                observations=[]
                for c in group:
                    if c['target']!=target:continue
                    base=next(g for g in group if g['point_id']==c['point_id'] and g['variant']['variant']=='original')
                    sample=next(s for s in data['samples'] if s['status']=='success' and s['point']['point_id']==c['point_id'])
                    item=next(i for i in sample['inputs'] if i['relation']==sample['point']['relation'])
                    if c['metrics']['query_seconds']['median']<=0:continue
                    x=100*item['rows']/item['base_rows']
                    observations.append((x,base['metrics']['query_seconds']['median']/c['metrics']['query_seconds']['median']))
                    def times(name):
                        return {s['block']:next(p['query_seconds'] for p in s['scopes'] if p['party']==0)
                                for s in data['samples'] if s['status']=='success' and s['mode']==mode
                                and s['point']['point_id']==c['point_id'] and s['variant']['target']==name}
                    baseline_times=times(base['target']);candidate_times=times(target)
                    raw=[baseline_times[b]/v for b,v in candidate_times.items() if v>0 and b in baseline_times]
                    if raw:ax.scatter([x]*len(raw),raw,s=12,alpha=.5,color=color(target,targets))
                observations.sort()
                if observations:ax.plot(*zip(*observations),marker=['o','s','^','D','v','x'][targets.index(target)%6],linestyle=['-','--',':','-.'][targets.index(target)%4],label=target,color=color(target,targets))
            ax.axhline(1,color='black',linestyle='--',linewidth=.8)
            ax.set(title=q.upper()+' · '+mode,xlabel='Actual retained input (%)',ylabel='Original / candidate median time')
            if targets:ax.legend(fontsize=7)
            else:ax.text(.5,.5,'No completed comparison',ha='center',transform=ax.transAxes)
        finish(fig,'input-size-'+mode,'lines: ratio of medians; small dots: paired ratios')
        chosen='q8-b7714-of20000'
        group=[c for c in cells if c['mode']==mode and c['point_id']==chosen]
        if group:
            fig,axes=plt.subplots(1,2,figsize=(12,5))
            bars(axes[0],group,'query_seconds');bars(axes[1],group,'query_bytes')
            for ax in axes:ax.set_title('Q8 · 38.570% Part · '+mode)
            finish(fig,'q8-runtime-bytes-'+mode)
            new=[c for c in group if c['variant']['family']=='duckdb-selectivity']
            chosen_cells=[c for c in group if c['variant']['variant'] in ('original','duckdb-canonical')]
            if new:chosen_cells.append(min(new,key=lambda c:c['metrics']['query_seconds']['median']))
            fig,axes=plt.subplots(1,len(chosen_cells),figsize=(15,5))
            for ax,cell in zip(axes,chosen_cells):
                sample=next(s for s in data['samples'] if s['status']=='success' and s['mode']==mode and s['point']['point_id']==chosen and s['variant']['target']==cell['target'])
                events={j['association']:j for j in sample['joins']};cursor=[0]
                def tree(text):
                    if not text.startswith('join('):
                        x=cursor[0];cursor[0]+=1;ax.text(x,0,text,rotation=45,ha='right',fontsize=7);return x,0
                    inside=text[5:-1];depth=0
                    for i,ch in enumerate(inside):
                        if ch=='(':depth+=1
                        elif ch==')':depth-=1
                        elif ch==',' and depth==0:break
                    left,right=inside[:i],inside[i+1:]
                    x1,y1=tree(left);x2,y2=tree(right);x=(x1+x2)/2;y=max(y1,y2)+1
                    ax.plot([x1,x,x2],[y1,y,y2],color='gray')
                    event=events.get(text,{})
                    label=f"⋈ {event.get('output_rows','?'):,}" if isinstance(event.get('output_rows'),int) else '⋈'
                    ax.text(x,y,label,fontsize=7,ha='center',bbox=dict(facecolor='white',edgecolor='none',pad=1))
                    return x,y
                tree(sample['scopes'][0]['association'])
                ax.set_title(cell['target']+'\nphysical output rows · one traced repetition',fontsize=9);ax.axis('off');ax.margins(.15)
            finish(fig,'q8-plan-work-'+mode,'recorded join trace; all repetitions exported separately')
    (figures/'README.md').write_text('Lines connect tested points for readability; no unmeasured transition is established. Error bars are observed ranges, not confidence intervals. Full-input figures use only new samples. Plan diagrams annotate one recorded repetition; all repetitions are in physical_work.csv.\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    plot(parser.parse_args().directory)
