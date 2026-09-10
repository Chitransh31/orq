# Implemented association reference

Generated from the frozen catalog and validated SF 0.01 plaintext invocation traces. Each left input is unique on the listed join-key tuple, as checked by the reporter. `tN` names the output of an earlier call; repeated base relations in Q5 original are intentional. These are ORQ PK/FK orientations, not transferred DuckDB hash build/probe choices.

All targets below passed the full-grid plaintext checks and the 3PC LAN correctness smoke. See [validation](VALIDATION.md) for artifacts and limits.

## q1

Variant `original-orq`; association ID `original non-DuckDB descriptor`.

```text
lineitem
```

No joins (Q1 control).

## q1_duckdb

Variant `duckdb-canonical`; association ID `ab22b54f50`.

```text
lineitem
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q01.materialized.k60.common.p0p01/optimized/explain.json).

No joins (Q1 control).

## q3

Variant `original-orq`; association ID `original non-DuckDB descriptor`.

```text
join(join(customer,orders),lineitem)
```

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `customer` | `orders` | `[CustKey]` |
| `t1` | `t0` | `lineitem` | `[OrderKey]` |

The OrderKey join uses the existing fused revenue aggregation, preserving unique OrderKey output before any remaining customer join.

## q3_duckdb

Variant `duckdb-canonical`; association ID `9e2220c981`.

```text
join(join(customer,orders),lineitem)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q03.materialized.k42941.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `customer` | `orders` | `[CustKey]` |
| `t1` | `t0` | `lineitem` | `[OrderKey]` |

The OrderKey join uses the existing fused revenue aggregation, preserving unique OrderKey output before any remaining customer join.

## q3_selduckdb_a

Variant `duckdb-selectivity-a`; association ID `d0d6191431`.

```text
join(customer,join(lineitem,orders))
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q03.materialized.k60.common.p0p01/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `orders` | `lineitem` | `[OrderKey]` |
| `t1` | `customer` | `t0` | `[CustKey]` |

The OrderKey join uses the existing fused revenue aggregation, preserving unique OrderKey output before any remaining customer join.

## q5

Variant `original-orq`; association ID `original non-DuckDB descriptor`.

```text
orq-original-duplicated-lineitem-branches
```

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `region` | `nation` | `[RegionKey]` |
| `t1` | `t0` | `supplier` | `[NationKey]` |
| `t2` | `t1` | `lineitem` | `[SuppKey]` |
| `t3` | `nation` | `customer` | `[NationKey]` |
| `t4` | `t3` | `orders` | `[CustKey]` |
| `t5` | `t4` | `lineitem` | `[OrderKey]` |
| `t6` | `t5` | `t2` | `[OrderKey], [LineNumber], [Name]` |

## q5_duckdb

Variant `duckdb-canonical`; association ID `caa229bb1d`.

```text
join(join(join(join(customer,join(nation,region)),orders),lineitem),supplier)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q05.materialized.k2502.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `region` | `nation` | `[RegionKey]` |
| `t1` | `t0` | `customer` | `[NationKey]` |
| `t2` | `t1` | `orders` | `[CustKey]` |
| `t3` | `t2` | `lineitem` | `[OrderKey]` |
| `t4` | `supplier` | `t3` | `[SuppKey], [NationKey]` |

## q5_selduckdb_a

Variant `duckdb-selectivity-a`; association ID `2510969359`.

```text
join(join(join(join(customer,orders),join(nation,region)),lineitem),supplier)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q05.materialized.k15.common.p0p01/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `region` | `nation` | `[RegionKey]` |
| `t1` | `customer` | `orders` | `[CustKey]` |
| `t2` | `t0` | `t1` | `[NationKey]` |
| `t3` | `t2` | `lineitem` | `[OrderKey]` |
| `t4` | `supplier` | `t3` | `[SuppKey], [NationKey]` |

## q8

Variant `original-orq`; association ID `original non-DuckDB descriptor`.

```text
join(join(join(join(join(customer,join(join(lineitem,part),orders)),nation_region),region),supplier),nation_name)
```

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `part` | `lineitem` | `[PartKey]` |
| `t1` | `orders` | `t0` | `[OrderKey]` |
| `t2` | `customer` | `t1` | `[CustKey]` |
| `t3` | `nation_region` | `t2` | `[NationKey]` |
| `t4` | `region` | `t3` | `[RegionKey]` |
| `t5` | `supplier` | `t4` | `[SuppKey]` |
| `t6` | `nation_name` | `t5` | `[NationKey]` |

## q8_duckdb

Variant `duckdb-canonical`; association ID `9d0bde819f`.

```text
join(join(join(join(customer,join(join(lineitem,part),orders)),join(nation_region,region)),supplier),nation_name)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q08.materialized.k2.common.p0p01/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `part` | `lineitem` | `[PartKey]` |
| `t1` | `orders` | `t0` | `[OrderKey]` |
| `t2` | `customer` | `t1` | `[CustKey]` |
| `t3` | `region` | `nation_region` | `[RegionKey]` |
| `t4` | `t3` | `t2` | `[NationKey]` |
| `t5` | `supplier` | `t4` | `[SuppKey]` |
| `t6` | `nation_name` | `t5` | `[NationKey]` |

## q8_selduckdb_a

Variant `duckdb-selectivity-a`; association ID `196b1b2fe8`.

```text
join(join(join(join(join(customer,join(nation_region,region)),orders),lineitem),part),join(nation_name,supplier))
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q08.materialized.k866.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `region` | `nation_region` | `[RegionKey]` |
| `t1` | `t0` | `customer` | `[NationKey]` |
| `t2` | `t1` | `orders` | `[CustKey]` |
| `t3` | `t2` | `lineitem` | `[OrderKey]` |
| `t4` | `nation_name` | `supplier` | `[NationKey]` |
| `t5` | `part` | `t3` | `[PartKey]` |
| `t6` | `t4` | `t5` | `[SuppKey]` |

## q8_selduckdb_b

Variant `duckdb-selectivity-b`; association ID `fa001ba951`.

```text
join(join(join(join(customer,join(nation_region,region)),join(join(lineitem,part),orders)),supplier),nation_name)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q08.materialized.k386.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `region` | `nation_region` | `[RegionKey]` |
| `t1` | `t0` | `customer` | `[NationKey]` |
| `t2` | `part` | `lineitem` | `[PartKey]` |
| `t3` | `orders` | `t2` | `[OrderKey]` |
| `t4` | `t1` | `t3` | `[CustKey]` |
| `t5` | `supplier` | `t4` | `[SuppKey]` |
| `t6` | `nation_name` | `t5` | `[NationKey]` |

## q8_selduckdb_c

Variant `duckdb-selectivity-c`; association ID `11123bb461`.

```text
join(join(join(join(join(customer,join(nation_region,region)),orders),lineitem),join(nation_name,supplier)),part)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q08.materialized.k14563.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `region` | `nation_region` | `[RegionKey]` |
| `t1` | `t0` | `customer` | `[NationKey]` |
| `t2` | `t1` | `orders` | `[CustKey]` |
| `t3` | `t2` | `lineitem` | `[OrderKey]` |
| `t4` | `nation_name` | `supplier` | `[NationKey]` |
| `t5` | `t4` | `t3` | `[SuppKey]` |
| `t6` | `part` | `t5` | `[PartKey]` |

## q8_selduckdb_d

Variant `duckdb-selectivity-d`; association ID `350e47ddc5`.

```text
join(join(join(customer,join(nation_region,region)),join(join(lineitem,part),orders)),join(nation_name,supplier))
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q08.materialized.k824.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `region` | `nation_region` | `[RegionKey]` |
| `t1` | `t0` | `customer` | `[NationKey]` |
| `t2` | `part` | `lineitem` | `[PartKey]` |
| `t3` | `orders` | `t2` | `[OrderKey]` |
| `t4` | `t1` | `t3` | `[CustKey]` |
| `t5` | `nation_name` | `supplier` | `[NationKey]` |
| `t6` | `t5` | `t4` | `[SuppKey]` |

## q9

Variant `original-orq`; association ID `original non-DuckDB descriptor`.

```text
join(join(join(join(join(nation,supplier),lineitem),part),orders),partsupp)
```

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `nation` | `supplier` | `[NationKey]` |
| `t1` | `t0` | `lineitem` | `[SuppKey]` |
| `t2` | `part` | `t1` | `[PartKey]` |
| `t3` | `orders` | `t2` | `[OrderKey]` |
| `t4` | `partsupp` | `t3` | `[PartKey], [SuppKey]` |

## q9_duckdb

Variant `duckdb-canonical`; association ID `69c40b364a`.

```text
join(join(join(lineitem,part),orders),join(join(nation,supplier),partsupp))
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q09.materialized.k3504.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `part` | `lineitem` | `[PartKey]` |
| `t1` | `orders` | `t0` | `[OrderKey]` |
| `t2` | `nation` | `supplier` | `[NationKey]` |
| `t3` | `t2` | `partsupp` | `[SuppKey]` |
| `t4` | `t3` | `t1` | `[PartKey], [SuppKey]` |

## q9_selduckdb_a

Variant `duckdb-selectivity-a`; association ID `735f00eb6f`.

```text
join(join(join(join(join(lineitem,part),supplier),nation),orders),partsupp)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q09.materialized.k2.common.p0p01/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `part` | `lineitem` | `[PartKey]` |
| `t1` | `supplier` | `t0` | `[SuppKey]` |
| `t2` | `nation` | `t1` | `[NationKey]` |
| `t3` | `orders` | `t2` | `[OrderKey]` |
| `t4` | `partsupp` | `t3` | `[PartKey], [SuppKey]` |

## q9_selduckdb_b

Variant `duckdb-selectivity-b`; association ID `e34bdfd89e`.

```text
join(join(join(join(lineitem,part),join(nation,supplier)),orders),partsupp)
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q09.materialized.k38.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `part` | `lineitem` | `[PartKey]` |
| `t1` | `nation` | `supplier` | `[NationKey]` |
| `t2` | `t1` | `t0` | `[SuppKey]` |
| `t3` | `orders` | `t2` | `[OrderKey]` |
| `t4` | `partsupp` | `t3` | `[PartKey], [SuppKey]` |

## q9_selduckdb_c

Variant `duckdb-selectivity-c`; association ID `3626b65ce2`.

```text
join(join(join(nation,supplier),join(part,partsupp)),join(lineitem,orders))
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q09.materialized.k9525.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `nation` | `supplier` | `[NationKey]` |
| `t1` | `part` | `partsupp` | `[PartKey]` |
| `t2` | `t0` | `t1` | `[SuppKey]` |
| `t3` | `orders` | `lineitem` | `[OrderKey]` |
| `t4` | `t2` | `t3` | `[PartKey], [SuppKey]` |

## q9_selduckdb_d

Variant `duckdb-selectivity-d`; association ID `001a9e75e5`.

```text
join(join(join(join(nation,supplier),partsupp),part),join(lineitem,orders))
```

[Source EXPLAIN](../../results/duckdb-tpch-selectivity/20260908T200506.888858Z-sf0p1/plans/q09.materialized.k14481.refinement/optimized/explain.json).

| Output | Unique left input | Right input | Join keys |
|---|---|---|---|
| `t0` | `nation` | `supplier` | `[NationKey]` |
| `t1` | `t0` | `partsupp` | `[SuppKey]` |
| `t2` | `part` | `t1` | `[PartKey]` |
| `t3` | `orders` | `lineitem` | `[OrderKey]` |
| `t4` | `t2` | `t3` | `[PartKey], [SuppKey]` |

