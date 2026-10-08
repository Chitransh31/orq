#!/usr/bin/env python3
"""Immutable evidence/catalog, exact public reductions, and experiment scheduling.

No database, subprocess, network, or output activity occurs on import.
"""
from __future__ import annotations
import argparse
import csv
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import random
import re

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'scripts/tpch-selectivity-plans/manifest.json'
RELATIONS = {1: 'lineitem', 3: 'lineitem', 5: 'orders', 8: 'part', 9: 'part'}
BASES = {1: 600572, 3: 600572, 5: 150000, 8: 20000, 9: 20000}
GRID = {1: [], 3: [21500, 42940, 42941, 321757], 5: [1258, 2501, 2502, 76251],
        8: [194, 385, 386, 605, 823, 824, 845, 865, 866, 7714, 14562, 14563, 17282],
        9: [20, 37, 38, 1771, 3503, 3504, 6514, 9524, 9525, 12003, 14480, 14481, 17241]}
PRESENTATION = {1: [], 3: [21500, 321757], 5: [1258, 76251, 150000],
                8: [845, 7714, 20000], 9: [1771, 6514, 20000]}
SMOKE = {1: None, 3: 21500, 5: 1258, 8: 845, 9: 1771}
MODE_ARGS = {'same': ['-p','1','-s','same','-c','mpi','-b','-12','--wan-sim','off'],
             'lan': ['-p','3','-s','lan','-c','nocopy','-n','4','-b','-12','--wan-sim','off'],
             'wan': ['-p','3','-s','wan','-c','nocopy','-n','-1','-b','-1','--wan-sim','userspace-distributed']}

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def percent(value: str) -> Fraction:
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,18}|/[0-9]+)?', str(value)):
        raise ValueError('invalid percentage: ' + str(value))
    # Match the binary parser's uint64 limits before Fraction cancels factors.
    raw = str(value)
    components = raw.split('/') if '/' in raw else [raw.replace('.', '')]
    if any(int(component) > 2**64-1 for component in components):
        raise ValueError('percentage component exceeds uint64')
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError('invalid percentage') from error
    if not 0 <= result <= 100 or max(result.numerator, result.denominator) > 2**64-1:
        raise ValueError('percentage outside supported range')
    return result

def selected_count(value: str | Fraction, total: int) -> int:
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 2**64-1:
        raise ValueError('invalid base length')
    p = percent(str(value))
    n, d = p.numerator * total, 100 * p.denominator
    return n // d + (2 * (n % d) >= d)

def association_id(tree: str) -> str:
    return hashlib.sha1(tree.encode()).hexdigest()[:10]

def normalize_tree(tree: str) -> str:
    tokens = re.findall(r'join|[a-z][a-z0-9_]*|[(),]', tree)
    if ''.join(tokens) != tree:
        raise ValueError('invalid association characters')
    pos = 0
    def node():
        nonlocal pos
        if pos >= len(tokens): raise ValueError('truncated association')
        name = tokens[pos]; pos += 1
        if name != 'join':
            if name in '(),': raise ValueError('expected relation')
            return name
        if pos >= len(tokens) or tokens[pos] != '(': raise ValueError('expected opening parenthesis')
        pos += 1; a = node()
        if pos >= len(tokens) or tokens[pos] != ',': raise ValueError('expected comma')
        pos += 1; b = node()
        if pos >= len(tokens) or tokens[pos] != ')': raise ValueError('expected closing parenthesis')
        pos += 1
        return 'join(' + ','.join(sorted((a,b))) + ')'
    result = node()
    if pos != len(tokens): raise ValueError('trailing association tokens')
    return result

def load_evidence(source: Path):
    rows = list(csv.DictReader((source / 'points.csv').open()))
    return rows, json.loads((source / 'transition_search.json').read_text())

def derive_regimes(rows):
    result = {}
    for row in rows:
        if (row['control'], row['input_mode'], row['point_kind']) != ('optimized','materialized','common'):
            continue
        key = (int(row['query_id']), row['association_plan_id'])
        result.setdefault(key, []).append(int(row['selected_rows']))
    return {key: [min(values),max(values)] for key,values in result.items()}

def derive_boundaries(search):
    return {int(entry['query_id']): [(t['low_count'],t['high_count']) for t in entry['transitions']
                                    if 'association' in t['changed_dimensions']]
            for entry in search if entry['input_mode'] == 'materialized'}

def load_catalog(path=CATALOG, verify=True):
    catalog = json.loads(Path(path).read_text())
    if verify: validate_catalog(catalog)
    return catalog

def validate_catalog(catalog):
    if catalog['schema_version'] != 1: raise ValueError('unsupported catalog schema')
    for path, expected in catalog['evidence_sha256'].items():
        if sha256(ROOT/path) != expected: raise ValueError('evidence hash mismatch: '+path)
    # Preserve the pre-existing frozen normalized-EXPLAIN checks.
    from report_tpch_plain_and_3pc import load_plan_catalog
    canonical = load_plan_catalog()['variants']['duckdb-canonical']['queries']
    targets = set()
    for variant in catalog['variants']:
        if variant['target'] in targets: raise ValueError('duplicate target')
        targets.add(variant['target'])
        tree = variant['association']
        if variant['association_plan_id']:
            if normalize_tree(tree) != tree or association_id(tree) != variant['association_plan_id']:
                raise ValueError('association ID/normalization mismatch')
        if variant['variant'] == 'duckdb-canonical' and tree != canonical[variant['query']]['association_tree']:
            raise ValueError('canonical association drift')
        if variant['family'] == 'duckdb-selectivity' and not variant['supporting_points']:
            raise ValueError('candidate lacks optimized evidence')
    return catalog

def build_grid(preset='full', catalog=None):
    if preset not in ('full','smoke','presentation'): raise ValueError('unknown grid')
    catalog = catalog or load_catalog()
    output = []
    for q in RELATIONS:
        counts = [None] if q == 1 else ([SMOKE[q]] if preset == 'smoke' else (PRESENTATION if preset == 'presentation' else GRID)[q])
        for b in counts:
            p = Fraction(10) if b is None else Fraction(100*b, BASES[q])
            point = {'query':f'q{q}', 'relation':RELATIONS[q], 'source_count':b,
                     'source_base_rows':BASES[q], 'percent':str(p),
                     'point_id':f'q{q}-p10' if b is None else f'q{q}-b{b}-of{BASES[q]}'}
            regimes = catalog['regimes'][f'q{q}']
            match = next((r for r in regimes if b is not None and r['low'] <= b <= r['high']),None)
            point['source_association_plan_id'] = match['association_plan_id'] if match else 'ab22b54f50'
            point['source_assignment'] = 'observed' if any(b in r['observed_counts'] for r in regimes) else 'inferred-interval'
            point['boundary_role'] = next((role for pair in catalog['boundaries'][f'q{q}']
                                           for role,k in [('low',pair[0]),('high',pair[1])] if b==k), 'representative')
            if preset == 'presentation' and b == BASES[q]:
                point['source_association_plan_id'] = None
                point['source_assignment'] = 'full-input-anchor; no source-selection claim'
                point['boundary_role'] = 'full-input-anchor'
            output.append(point)
    return output

def schedule(preset='full', modes=('same','lan','wan'), repetitions=3, seed=20260909, catalog=None, point_ids=None, block_start=0):
    if repetitions < 1 or block_start < 0 or len(set(modes)) != len(modes) or any(m not in MODE_ARGS for m in modes):
        raise ValueError('invalid modes/repetitions')
    catalog = catalog or load_catalog(); grid=build_grid(preset,catalog)
    if point_ids is not None:
        if not point_ids or len(set(point_ids)) != len(point_ids):raise ValueError('empty/duplicate point selection')
        if set(point_ids)-{p['point_id'] for p in grid}:raise ValueError('unknown point selection')
        grid=[p for p in grid if p['point_id'] in point_ids]
    rng=random.Random(seed); output=[]
    for mode in modes:
        for block in range(block_start + repetitions):
            points=grid.copy();rng.shuffle(points)
            for point in points:
                variants=[v for v in catalog['variants'] if v['query']==point['query']];rng.shuffle(variants)
                for variant in variants:
                    if block < block_start:continue
                    output.append({'mode':mode,'block':block,'point':point,'variant':variant,
                                   'sample_id':f"{mode}/{point['point_id']}/{variant['target']}/rep-{block}"})
    return output

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['verify-evidence','grid'])
    parser.add_argument('--manifest',type=Path,default=CATALOG)
    args=parser.parse_args();catalog=load_catalog(args.manifest)
    if args.command=='grid':print(json.dumps(build_grid(catalog=catalog),indent=2))
    else:print(f"Verified {len(catalog['variants'])} targets and {len(catalog['evidence_sha256'])} artifact hashes")

# Predicate/key contracts for the actual invocation trace (orientation is ORQ-specific).
UNIQUE_KEYS = {'customer': {'[CustKey]'}, 'orders': {'[OrderKey]'},
               'lineitem': {'[OrderKey]', '[LineNumber]'}, 'part': {'[PartKey]'},
               'partsupp': {'[PartKey]', '[SuppKey]'}, 'supplier': {'[SuppKey]'},
               'nation': {'[NationKey]'}, 'nation_region': {'[NationKey]'},
               'nation_name': {'[NationKey]'}, 'region': {'[RegionKey]'}}
EDGES = {
    'q3': [('customer','orders','[CustKey]'),('orders','lineitem','[OrderKey]')],
    'q5': [('region','nation','[RegionKey]'),('nation','customer','[NationKey]'),
           ('customer','orders','[CustKey]'),('orders','lineitem','[OrderKey]'),
           ('lineitem','supplier','[SuppKey]'),('nation','supplier','[NationKey]'),
           ('customer','supplier','[NationKey]')],
    'q8': [('part','lineitem','[PartKey]'),('supplier','lineitem','[SuppKey]'),
           ('lineitem','orders','[OrderKey]'),('orders','customer','[CustKey]'),
           ('customer','nation_region','[NationKey]'),('nation_region','region','[RegionKey]'),
           ('supplier','nation_name','[NationKey]')],
    'q9': [('part','lineitem','[PartKey]'),('partsupp','lineitem','[PartKey]'),
           ('partsupp','lineitem','[SuppKey]'),('supplier','lineitem','[SuppKey]'),
           ('supplier','partsupp','[SuppKey]'),('orders','lineitem','[OrderKey]'),
           ('nation','supplier','[NationKey]'),('part','partsupp','[PartKey]')]}

def relations(tree):
    normalize_tree(tree)
    return set(re.findall(r'[a-z][a-z0-9_]*',tree))-{'join'}

def join_keys(query,left,right):
    a,b=relations(left),relations(right)
    if query=='q5' and 'lineitem' in a & b:
        return {'[OrderKey]','[LineNumber]','[Name]'}
    return {k for x,y,k in EDGES.get(query,[]) if (x in a and y in b) or (y in a and x in b)}

if __name__=='__main__':main()
