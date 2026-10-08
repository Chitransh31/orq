SELECT
    n.n_name,
    sum(l.l_extendedprice * (1 - l.l_discount)) AS revenue
FROM region AS r
JOIN nation AS n ON r.r_regionkey = n.n_regionkey
JOIN customer AS c ON n.n_nationkey = c.c_nationkey
JOIN orders AS o ON c.c_custkey = o.o_custkey
JOIN lineitem AS l ON o.o_orderkey = l.l_orderkey
JOIN supplier AS s
  ON l.l_suppkey = s.s_suppkey
 AND n.n_nationkey = s.s_nationkey
WHERE r.r_name = 'ASIA'
  AND o.o_orderdate >= DATE '1994-01-01'
  AND o.o_orderdate < DATE '1995-01-01'
GROUP BY n.n_name
ORDER BY revenue DESC;
