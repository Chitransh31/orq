#!/usr/bin/env python3
"""Regenerate controlled-selectivity figures from saved CSV/JSON only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

COLORS = {1: '#0072B2', 3: '#D55E00', 5: '#009E73', 8: '#CC79A7', 9: '#E69F00'}
CONTROLS = {'optimized': '#0072B2', 'fixed_a': '#D55E00', 'fixed_b': '#009E73'}


def load_points(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('status') != 'complete':
        raise ValueError('refusing to plot incomplete experiment artifacts')
    with (directory / 'points.csv').open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    seen = set()
    for row in rows:
        if row['point_id'] in seen:
            raise ValueError(f'duplicate point ID: {row["point_id"]}')
        seen.add(row['point_id'])
        row['query_id'] = int(row['query_id'])
        for field in ('target_percent', 'actual_percent', 'selected_rows', 'base_rows',
                      'median_seconds', 'median_ci_low', 'median_ci_high',
                      'estimated_qualifying_rows', 'join_output_rows'):
            row[field] = float(row[field]) if row[field] else math.nan
        if not all(math.isfinite(row[k]) for k in ('median_seconds', 'median_ci_low', 'median_ci_high')):
            raise ValueError(f'missing runtime: {row["point_id"]}')
        if not (0 <= row['median_ci_low'] <= row['median_seconds'] <= row['median_ci_high']):
            raise ValueError(f'invalid uncertainty interval: {row["point_id"]}')
        if not (directory / row['raw_dir'] / 'explain.json').is_file():
            raise ValueError(f'missing plan artifact: {row["point_id"]}')
    return manifest, rows


def plotted_rows(manifest, rows):
    result = []
    for row in rows:
        baseline = [r for r in rows if r['query_id'] == row['query_id'] and
                    r['input_mode'] == row['input_mode'] and r['control'] == row['control'] and
                    r['point_kind'] == 'common' and r['target_percent'] == 100]
        if len(baseline) != 1 or baseline[0]['median_seconds'] <= 0:
            raise ValueError(f'missing/invalid 100% baseline: {row["point_id"]}')
        result.append({**row, 'normalized_median': row['median_seconds']/baseline[0]['median_seconds'],
                       'baseline_point_id': baseline[0]['point_id'],
                       'milliseconds': row['median_seconds']*1000,
                       'ci_low_ms': row['median_ci_low']*1000, 'ci_high_ms': row['median_ci_high']*1000})
    modes = manifest.get('input_modes', {r['input_mode'] for r in rows})
    queries = {int(q) for q in manifest.get('query_definitions', {r['query_id']: {} for r in rows})}
    for mode in modes:
        for q in queries:
            controls = ('optimized',) if q == 1 else ('optimized', 'fixed_a', 'fixed_b')
            for control in controls:
                observed = [r['target_percent'] for r in rows if r['input_mode'] == mode and
                            r['query_id'] == q and r['control'] == control and r['point_kind'] == 'common']
                expected = [float(t) for t in manifest['targets_percent']]
                if sorted(observed) != sorted(expected):
                    raise ValueError(f'incomplete or duplicated common grid: Q{q} {mode} {control}')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', type=Path)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    manifest, raw = load_points(args.artifacts)
    rows = plotted_rows(manifest, raw)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    output = args.output_dir or args.artifacts / 'figures'
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'plotted.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    files = []
    def save(fig, name):
        for ext in ('pdf', 'svg', 'png'):
            path = output / f'{name}.{ext}'
            fig.savefig(path, dpi=180, bbox_inches='tight')
            files.append(path.name)
        plt.close(fig)
    xlabel = 'Target selectivity of varied input (%)'
    queries = sorted({r['query_id'] for r in rows})
    searches = json.loads((args.artifacts / 'transition_search.json').read_text())
    legends = {}
    for mode in sorted({r['input_mode'] for r in rows}):
        subset = [r for r in rows if r['input_mode'] == mode]
        title = f'SF {manifest["scale_factor"]} | {mode} | controlled input selectivity'
        fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
        for q in queries:
            data = sorted((r for r in subset if r['query_id'] == q and r['control'] == 'optimized' and r['point_kind'] == 'common'), key=lambda r: r['target_percent'])
            x = [r['target_percent'] for r in data]
            axes[0].plot(x, [r['normalized_median'] for r in data], '.-', color=COLORS[q], label=f'Q{q}')
            axes[1].plot(x, [r['milliseconds'] for r in data], '.-', color=COLORS[q], label=f'Q{q}')
            axes[1].fill_between(x, [r['ci_low_ms'] for r in data], [r['ci_high_ms'] for r in data], color=COLORS[q], alpha=.15)
        for ax in axes:
            ax.set_xlabel(xlabel)
            ax.legend()
            ax.grid(alpha=.2)
        axes[0].set_ylabel('Median / same query median at 100%')
        axes[1].set_ylabel('Median runtime (ms), 95% median CI')
        fig.suptitle(title)
        fig.text(.5, -.04, 'Varied inputs: Q1/Q3 lineitem; Q5 orders; Q8/Q9 part. k = floor(target × N / 100 + 0.5).\n'
                 'Actual percentages are rounded by row count; equal percentages need not imply equal work. Normalization uses a measured baseline.', ha='center', fontsize=9)
        fig.tight_layout()
        save(fig, f'{mode}-overview')

        # Each dimension gets categorical strips, never numerical plan-ID curves.
        fig, axes = plt.subplots(len(queries), 3, figsize=(16, max(4, len(queries)*1.25)), squeeze=False)
        for i, q in enumerate(queries):
            data = sorted((r for r in subset if r['query_id'] == q and r['control'] == 'optimized' and r['point_kind'] != 'boundary'), key=lambda r: r['target_percent'])
            for j, dimension in enumerate(('association', 'orientation', 'physical')):
                ax = axes[i, j]
                ids = list(dict.fromkeys(r[f'{dimension}_plan_id'] for r in data))
                labels = {pid: f'Q{q}-{dimension[0].upper()}{index+1}' for index, pid in enumerate(ids)}
                legends[f'{mode}/Q{q}/{dimension}'] = {labels[pid]: {'plan_id': pid, 'fingerprint': next(r[f'{dimension}_fingerprint'] for r in data if r[f'{dimension}_plan_id'] == pid)} for pid in ids}
                cmap = ListedColormap([plt.get_cmap('tab20')(i % 20) for i in range(len(ids))])
                ax.scatter([r['target_percent'] for r in data], [0]*len(data), c=[ids.index(r[f'{dimension}_plan_id']) for r in data], cmap=cmap, vmin=-.5, vmax=len(ids)-.5, marker='s', s=45)
                previous = None
                for row in data:
                    pid = row[f'{dimension}_plan_id']
                    if pid != previous:
                        ax.annotate(labels[pid], (row['target_percent'], 0), xytext=(0, 9), textcoords='offset points', fontsize=7, rotation=25)
                    previous = pid
                ax.set_xlim(-1, 103)
                ax.set_ylim(-.2, .45)
                ax.set_yticks([])
                ax.set_ylabel(f'Q{q}' + (' (no joins)' if q == 1 else ''))
                if i == 0:
                    ax.set_title(dimension + ' (sampled points)')
                if i == len(queries)-1:
                    ax.set_xlabel(xlabel)
        fig.suptitle(title + ' | Plan structure; gaps are unobserved')
        fig.tight_layout()
        save(fig, f'{mode}-plan-strips')

        for q in queries:
            fig, axes = plt.subplots(1, 3, figsize=(16, 4.3))
            for control, color in CONTROLS.items():
                data = sorted((r for r in subset if r['query_id'] == q and r['control'] == control and r['point_kind'] != 'boundary'), key=lambda r: r['target_percent'])
                if not data:
                    continue
                x = [r['target_percent'] for r in data]
                axes[1].plot(x, [r['join_output_rows'] for r in data], '.-', label=control, color=color)
                axes[2].plot(x, [r['milliseconds'] for r in data], '.-', label=control, color=color)
                axes[2].fill_between(x, [r['ci_low_ms'] for r in data], [r['ci_high_ms'] for r in data], color=color, alpha=.15)
                if control == 'optimized':
                    axes[0].plot(x, [r['selected_rows'] for r in data], '.-', label='actual qualifying rows')
                    axes[0].plot(x, [r['estimated_qualifying_rows'] for r in data], '.-', label='standalone input estimate')
            for ax, ylabel in zip(axes, ('Qualifying rows (before joins)', 'Sum of intermediate join-output rows', 'Query runtime (ms), 95% median CI')):
                ax.set_xlabel(xlabel)
                ax.set_ylabel(ylabel)
                ax.legend()
                ax.grid(alpha=.2)
            fig.suptitle(title + f' | Q{q} | includes refinement points')
            fig.tight_layout()
            save(fig, f'{mode}-q{q}-explanation')

        # Cost of materializing each identical selected relation is a separate panel.
        with (args.artifacts / 'preparations.csv').open(newline='') as handle:
            prep = [r for r in csv.DictReader(handle) if r['input_mode'] == mode]
        if prep:
            fig, ax = plt.subplots(figsize=(8, 4))
            for q in queries:
                data = sorted((r for r in prep if int(r['query_id']) == q), key=lambda r: int(r['selected_rows']))
                n = next(r['base_rows'] for r in subset if r['query_id'] == q)
                ax.scatter([100*int(r['selected_rows'])/n for r in data], [1000*float(r['seconds']) for r in data], s=12, color=COLORS[q], label=f'Q{q}')
            ax.set(xlabel='Actual selectivity of varied input (%)', ylabel='CTAS + ANALYZE (ms)', title=title + ' | Preparation costs (single measurements)')
            ax.legend()
            save(fig, f'{mode}-preparation')
            with (output / f'{mode}-preparation-plotted.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(prep[0]))
                writer.writeheader()
                writer.writerows(prep)
        for search in (s for s in searches if s['input_mode'] == mode):
            q = search['query_id']
            for index, transition in enumerate(search['transitions']):
                fig, axes = plt.subplots(1, 2, figsize=(14, 6))
                for ax, side in zip(axes, ('low', 'high')):
                    row = next(r for r in subset if r['query_id'] == q and r['control'] == 'optimized' and r['selected_rows'] == transition[f'{side}_count'])
                    tree = json.loads(row['physical_fingerprint'])
                    def draw(node, x, y, width):
                        name, relation, children = node
                        ax.text(x, y, relation or name, ha='center', va='center', fontsize=6,
                                bbox={'boxstyle': 'round', 'facecolor': '#E8F1F8', 'edgecolor': '#667788'})
                        for i, child in enumerate(children):
                            cx = x + width*((i+.5)/len(children)-.5)
                            ax.plot([x, cx], [y-.12, y-1+.12], color='#667788', lw=.7)
                            draw(child, cx, y-1, width/max(1, len(children)))
                    for tree_root in tree:
                        draw(tree_root, 0, 0, 12)
                    ax.set_title(f'Q{q}: k={int(row["selected_rows"])} ({row["actual_percent"]:.6g}%)')
                    ax.axis('off')
                fig.suptitle(', '.join(transition['changed_dimensions']) + f' | adjacent rows: {transition["adjacent_rows"]}')
                save(fig, f'{mode}-q{q}-transition-{index+1}')
    (output / 'plan_labels.json').write_text(json.dumps(legends, indent=2)+'\n')
    trace = {'source_manifest': str((args.artifacts / 'manifest.json').resolve()),
             'source_sha256': {name: hashlib.sha256((args.artifacts / name).read_bytes()).hexdigest()
                               for name in ('manifest.json', 'points.csv', 'samples.csv', 'preparations.csv', 'transition_search.json')},
             'figures': files, 'searches': searches,
             'notes': 'Boundary points retained in plotted.csv, excluded from common-grid and refinement figures. Missing estimates appear as gaps. See plan_labels.json for full categorical identities.'}
    (output / 'figure_manifest.json').write_text(json.dumps(trace, indent=2)+'\n')
    print(f'Figures saved: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
