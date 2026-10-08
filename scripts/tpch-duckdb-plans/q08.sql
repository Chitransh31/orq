SELECT
    o_year,
    sum(CASE WHEN nation = 'BRAZIL' THEN volume ELSE 0 END) / sum(volume) AS mkt_share
FROM (
    SELECT
        extract(year FROM o.o_orderdate) AS o_year,
        l.l_extendedprice * (1 - l.l_discount) AS volume,
        n2.n_name AS nation
    FROM part AS p
    JOIN lineitem AS l ON p.p_partkey = l.l_partkey
    JOIN orders AS o ON l.l_orderkey = o.o_orderkey
    JOIN customer AS c ON o.o_custkey = c.c_custkey
    JOIN nation AS n1 ON c.c_nationkey = n1.n_nationkey
    JOIN region AS r ON n1.n_regionkey = r.r_regionkey
    JOIN supplier AS s ON l.l_suppkey = s.s_suppkey
    JOIN nation AS n2 ON s.s_nationkey = n2.n_nationkey
    WHERE r.r_name = 'AMERICA'
      AND o.o_orderdate BETWEEN DATE '1995-01-01' AND DATE '1996-12-31'
      AND p.p_type = 'ECONOMY ANODIZED STEEL'
) AS all_nations
GROUP BY o_year
ORDER BY o_year;
