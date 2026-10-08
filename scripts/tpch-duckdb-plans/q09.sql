SELECT
    nation,
    o_year,
    sum(amount) AS sum_profit
FROM (
    SELECT
        n.n_name AS nation,
        extract(year FROM o.o_orderdate) AS o_year,
        l.l_extendedprice * (1 - l.l_discount)
            - ps.ps_supplycost * l.l_quantity AS amount
    FROM nation AS n
    JOIN supplier AS s ON n.n_nationkey = s.s_nationkey
    JOIN lineitem AS l ON s.s_suppkey = l.l_suppkey
    JOIN part AS p ON l.l_partkey = p.p_partkey
    JOIN orders AS o ON l.l_orderkey = o.o_orderkey
    JOIN partsupp AS ps
      ON ps.ps_partkey = l.l_partkey
     AND ps.ps_suppkey = l.l_suppkey
    WHERE p.p_name LIKE '%green%'
) AS profit
GROUP BY nation, o_year
ORDER BY nation, o_year DESC;
