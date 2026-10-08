/**
 * @file q9.cpp
 * @brief TPCH Query 9
 *
 * Equivalent SQL:
 * select
 *     nation,
 *     o_year,
 *     sum(amount) as sum_profit
 * from (
 *     select
 *         n_name as nation,
 *         extract(year from o_orderdate) as o_year,
 *         l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity as amount
 *     from
 *         part,
 *         supplier,
 *         lineitem,
 *         partsupp,
 *         orders,
 *         nation
 *     where
 *         s_suppkey = l_suppkey
 *         and ps_suppkey = l_suppkey
 *         and ps_partkey = l_partkey
 *         and p_partkey = l_partkey
 *         and o_orderkey = l_orderkey
 *         and s_nationkey = n_nationkey
 *         and p_name like '%[COLOR]%'
 *     ) as profit
 * group by
 *     nation,
 *     o_year
 * order by
 *     nation,
 *     o_year desc;
 *
 */

#include <sys/time.h>

#include "orq.h"
#include "profiling/stopwatch.h"
#include "tpch_dbgen.h"

// #define PRINT_TABLES

// #define QUERY_PROFILE

#ifndef PRINT_TABLES
#define print_table(...)
#endif

using namespace orq::debug;
using namespace orq::service;
using namespace orq::operators;
using namespace std::chrono;
using namespace orq::aggregators;
using namespace orq::benchmarking;

using namespace COMPILED_MPC_PROTOCOL_NAMESPACE;

using T = int64_t;

using sec = duration<float, seconds::period>;

#if TPCH_SELECTIVITY_PLAN == 1
constexpr auto TPCH_PLAN_VARIANT = "duckdb-selectivity-a";
constexpr auto TPCH_PLAN_ASSOCIATION = "join(join(join(join(join(lineitem,part),supplier),nation),orders),partsupp)";
#elif TPCH_SELECTIVITY_PLAN == 2
constexpr auto TPCH_PLAN_VARIANT = "duckdb-selectivity-b";
constexpr auto TPCH_PLAN_ASSOCIATION = "join(join(join(join(lineitem,part),join(nation,supplier)),orders),partsupp)";
#elif TPCH_SELECTIVITY_PLAN == 3
constexpr auto TPCH_PLAN_VARIANT = "duckdb-selectivity-c";
constexpr auto TPCH_PLAN_ASSOCIATION = "join(join(join(nation,supplier),join(part,partsupp)),join(lineitem,orders))";
#elif TPCH_SELECTIVITY_PLAN == 4
constexpr auto TPCH_PLAN_VARIANT = "duckdb-selectivity-d";
constexpr auto TPCH_PLAN_ASSOCIATION = "join(join(join(join(nation,supplier),partsupp),part),join(lineitem,orders))";
#else
#ifdef TPCH_DUCKDB_CANONICAL_PLAN
constexpr auto TPCH_PLAN_VARIANT = "duckdb-canonical";
constexpr auto TPCH_PLAN_ASSOCIATION =
    "join(join(join(lineitem,part),orders),join(join(nation,supplier),partsupp))";
#else
constexpr auto TPCH_PLAN_VARIANT = "original-orq";
constexpr auto TPCH_PLAN_ASSOCIATION =
    "join(join(join(join(join(nation,supplier),lineitem),part),orders),partsupp)";
#endif

#endif

int main(int argc, char** argv) {
    orq_init(argc, argv);
    auto pid = runTime->getPartyID();

    auto sf = runTime->getArg<float>("test-size", "r", 0.1);
    // TPCH Q9 query parameters
    const int P_NAME_COLOR = 1;  // Substitution for p_name like '%[COLOR]%'

    ////////////////////////////////////////////////////////////////
    // Database Initialization

    // Setup SQLite DB for output validation
    sqlite3* sqlite_db = nullptr;
#ifndef QUERY_PROFILE
    if (pid == 0) {
        int err = sqlite3_open(NULL, &sqlite_db);  // NULL -> Create in-memory database
        if (err) {
            throw std::runtime_error(sqlite3_errmsg(sqlite_db));
        } else {
            single_cout("SQLite DB created");
        }
    }
#endif

    // Query DB setup
    orq::benchmarking::tpch_experiment::Scope experiment("q9", TPCH_PLAN_ASSOCIATION);
    auto db = TPCDatabase<T>(sf, sqlite_db);

    using A = ASharedVector<T>;
    using B = BSharedVector<T>;

    single_cout("Q9 SF " << db.scaleFactor);
    single_cout("[QUERY_PLAN] query=q9 variant=" << TPCH_PLAN_VARIANT
                                                  << " association=" << TPCH_PLAN_ASSOCIATION);

    ////////////////////////////////////////////////////////////////
    // Query

    auto Part = db.getPartTable();
    auto Supplier = db.getSupplierTable();
    auto LineItem = db.getLineitemTable();
    auto PartSupp = db.getPartSuppTable();
    auto Orders = db.getOrdersTable();
    auto Nation = db.getNationTable();

    Part.project({"[Name]", "[PartKey]"});
    Supplier.project({"[NationKey]", "[SuppKey]"});
    LineItem.project(
        {"[SuppKey]", "[PartKey]", "[OrderKey]", "ExtendedPrice", "Discount", "Quantity"});
    PartSupp.project({"[PartKey]", "[SuppKey]", "SupplyCost"});
    Orders.project({"[OrderKey]", "[OrderDate]"});
    Nation.project({"[NationKey]", "[Name]"});

#ifdef PRINT_TABLES
    single_cout("Part size: " << db.partSize());
    print_table(Part.open_with_schema(), pid);

    single_cout("Supplier size: " << db.supplierSize());
    print_table(Supplier.open_with_schema(), pid);

    single_cout("LineItem size: " << db.lineitemsSize());
    print_table(LineItem.open_with_schema(), pid);

    single_cout("PartSupp size: " << db.partSuppSize());
    print_table(PartSupp.open_with_schema(), pid);

    single_cout("Orders size: " << db.ordersSize());
    print_table(Orders.open_with_schema(), pid);

    single_cout("Nation size: " << db.nationSize());
    print_table(Nation.open_with_schema(), pid);
#endif

    experiment.begin_query();
    stopwatch::timepoint("Start");
    stopwatch::profile_init();

    Part.filter(Part["[Name]"] == P_NAME_COLOR);
#if TPCH_SELECTIVITY_PLAN
    Supplier.addColumns({"[NationName]"});
    LineItem.addColumns({"[NationName]"});
#endif

    stopwatch::timepoint("Part Filter");

#if TPCH_SELECTIVITY_PLAN
#if TPCH_SELECTIVITY_PLAN == 1 || TPCH_SELECTIVITY_PLAN == 2
    auto PL = join(Part, LineItem, {"[PartKey]"}, {});
#if TPCH_SELECTIVITY_PLAN == 1
    auto SL = join(Supplier, PL, {"[SuppKey]"}, {{"[NationKey]", "[NationKey]", copy<B>}});
    auto NL = join(Nation, SL, {"[NationKey]"}, {{"[Name]", "[NationName]", copy<B>}});
    SL.deleteTable();
#else
    auto NS = join(Nation, Supplier, {"[NationKey]"}, {{"[Name]", "[NationName]", copy<B>}});
    auto NL = join(NS, PL, {"[SuppKey]"}, {{"[NationName]", "[NationName]", copy<B>}});
    NS.deleteTable();
#endif
    auto OL = join(Orders, NL, {"[OrderKey]"}, {{"[OrderDate]", "[OrderDate]", copy<B>}});
    auto FinalJoin = join(PartSupp, OL, {"[PartKey]", "[SuppKey]"},
        {{"SupplyCost", "SupplyCost", copy<A>}});
    PL.deleteTable(); NL.deleteTable(); OL.deleteTable();
#else
    auto NS = join(Nation, Supplier, {"[NationKey]"}, {{"[Name]", "[NationName]", copy<B>}});
#if TPCH_SELECTIVITY_PLAN == 3
    auto PPS = join(Part, PartSupp, {"[PartKey]"}, {});
    auto Dimension = join(NS, PPS, {"[SuppKey]"}, {{"[NationName]", "[NationName]", copy<B>}});
    PPS.deleteTable();
#else
    auto NPS = join(NS, PartSupp, {"[SuppKey]"}, {{"[NationName]", "[NationName]", copy<B>}});
    auto Dimension = join(Part, NPS, {"[PartKey]"}, {});
    NPS.deleteTable();
#endif
    auto OL = join(Orders, LineItem, {"[OrderKey]"}, {{"[OrderDate]", "[OrderDate]", copy<B>}});
    auto FinalJoin = join(Dimension, OL, {"[PartKey]", "[SuppKey]"},
        {{"SupplyCost", "SupplyCost", copy<A>}, {"[NationName]", "[NationName]", copy<B>}});
    NS.deleteTable(); Dimension.deleteTable(); OL.deleteTable();
#endif
    Part.deleteTable(); Supplier.deleteTable(); LineItem.deleteTable(); PartSupp.deleteTable(); Orders.deleteTable();
#elif defined(TPCH_DUCKDB_CANONICAL_PLAN)
    // Left branch of DuckDB's canonical association: (Lineitem join Part) join Orders.
    auto PartLineItemJoin = join(Part, LineItem, {"[PartKey]"}, {});
    Part.deleteTable();
    LineItem.deleteTable();

    stopwatch::timepoint("PartKey Join");

    auto LineItemOrderKeyJoin = join(Orders, PartLineItemJoin, {"[OrderKey]"}, {{"[OrderDate]", "[OrderDate]", copy<B>}});
    Orders.deleteTable();
    PartLineItemJoin.deleteTable();

    stopwatch::timepoint("OrderKey Join");

    // Right branch: (Nation join Supplier) join PartSupp.
    Supplier.addColumns({"[NationName]"});
    auto SuppliersJoin =
        join(Nation, Supplier, {"[NationKey]"}, {{"[Name]", "[NationName]", copy<B>}});
    Supplier.deleteTable();

    stopwatch::timepoint("NationKey Join");

    auto SupplierPartSuppJoin = join(SuppliersJoin, PartSupp, {"[SuppKey]"}, {{"[NationName]", "[NationName]", copy<B>}});
    SuppliersJoin.deleteTable();
    PartSupp.deleteTable();

    stopwatch::timepoint("SuppKey Join");

    // The composite PartSupp key is the primary side for the final ORQ join.
    auto FinalJoin = join(SupplierPartSuppJoin, LineItemOrderKeyJoin, {"[PartKey]", "[SuppKey]"},
        {{"SupplyCost", "SupplyCost", copy<A>},
         {"[NationName]", "[NationName]", copy<B>}});
    SupplierPartSuppJoin.deleteTable();
    LineItemOrderKeyJoin.deleteTable();

    stopwatch::timepoint("PartSupp Join");
#else
    Supplier.addColumns({"[NationName]"});
    auto SuppliersJoin =
        join(Nation, Supplier, {"[NationKey]"}, {{"[Name]", "[NationName]", copy<B>}});

    Supplier.deleteTable();

    stopwatch::timepoint("NationKey Join");

    auto LineItemSuppKeyJoin = join(SuppliersJoin, LineItem, {"[SuppKey]"}, {{"[NationName]", "[NationName]", copy<B>}});

    SuppliersJoin.deleteTable();
    LineItem.deleteTable();

    stopwatch::timepoint("SuppKey Join");

    auto LineItemPartKeyJoin = join(Part, LineItemSuppKeyJoin, {"[PartKey]"}, {});

    Part.deleteTable();
    LineItemSuppKeyJoin.deleteTable();
    stopwatch::timepoint("PartKey Join");

    auto LineItemOrderKeyJoin = join(Orders, LineItemPartKeyJoin, {"[OrderKey]"},
                                                  {{"[OrderDate]", "[OrderDate]", copy<B>}});

    Orders.deleteTable();
    LineItemPartKeyJoin.deleteTable();
    stopwatch::timepoint("OrderKey Join");

    auto FinalJoin = join(PartSupp, LineItemOrderKeyJoin, {"[PartKey]", "[SuppKey]"},
                                         {{"SupplyCost", "SupplyCost", copy<A>}});

    PartSupp.deleteTable();
    LineItemOrderKeyJoin.deleteTable();

    stopwatch::timepoint("PartSupp Join");
#endif

    FinalJoin.project(
        {"[NationName]", "[OrderDate]", "ExtendedPrice", "Discount", "Quantity", "SupplyCost"});

    FinalJoin.addColumns({"Amount", "SumProfit"});

    // Using a version without the PartSupp table
    FinalJoin["Amount"] = (FinalJoin["ExtendedPrice"] * (-FinalJoin["Discount"] + 100) / 100) -
                          FinalJoin["SupplyCost"] * FinalJoin["Quantity"];

    stopwatch::timepoint("Calculate amount");

    FinalJoin.aggregate({"[NationName]", "[OrderDate]"}, {{"Amount", "SumProfit", sum<A>}});

    stopwatch::timepoint("Group by + Sum");

    FinalJoin.sort({std::make_pair("[NationName]", ASC), std::make_pair("[OrderDate]", DESC)});

    stopwatch::timepoint("Order by");

#ifdef QUERY_PROFILE
    // Include the final mask and shuffle in benchmarking time
    FinalJoin.finalize();
    stopwatch::timepoint("Finalize");
#endif

    experiment.finish(FinalJoin);
    stopwatch::done();          // print wall clock time
    stopwatch::profile_done();  // print profiling data
    experiment.emit();

    runTime->print_statistics();
    runTime->print_communicator_statistics();

    ////////////////////////////////////////////////////////////////
    // Correctness Test

#ifndef QUERY_PROFILE

    auto result = FinalJoin.open_with_schema();
    auto nation = FinalJoin.get_column(result, "[NationName]");
    auto o_year = FinalJoin.get_column(result, "[OrderDate]");
    auto sum_profit = FinalJoin.get_column(result, "SumProfit");
    orq::benchmarking::tpch_experiment::result<T>({"NationName","OrderDate","Profit"}, {nation,o_year,sum_profit});


    if (pid == 0) {
        // Fetch Q9 SQL result to validate
        // Note: The "extract" operation is unnecessary here,
        // given the integer representation of OrderDate
        int ret;
        const char* query = R"sql(
            select
                nation,
                o_year,
                sum(amount) as sum_profit
            from (
                select
                    n.Name as nation,
                    o.OrderDate as o_year,
                    (l.ExtendedPrice * (100 - l.Discount) / 100) - ps.supplycost * l.quantity as amount
                from
                    part as p,
                    supplier as s,
                    lineitem as l,
                    partsupp as ps,
                    orders as o,
                    nation as n
                where
                    s.SuppKey = l.SuppKey
                    and ps.SuppKey = l.SuppKey
                    and ps.PartKey = l.PartKey
                    and p.PartKey = l.PartKey
                    and o.OrderKey = l.OrderKey
                    and s.NationKey = n.NationKey
                    and p.Name = ?
                ) as profit
            group by
                nation,
                o_year
            order by
                nation,
                o_year desc;
        )sql";

        sqlite3_stmt* stmt;
        ret = sqlite3_prepare_v2(sqlite_db, query, -1, &stmt, NULL);

        // Fill in query placeholders
        sqlite3_bind_int(stmt, 1, P_NAME_COLOR);

        int i = 0;
        while ((ret = sqlite3_step(stmt)) == SQLITE_ROW) {
            int64_t sqlNation = sqlite3_column_int64(stmt, 0);
            int64_t sqlYear = sqlite3_column_int64(stmt, 1);
            int64_t sqlProfit = sqlite3_column_int64(stmt, 2);

            // std::cout << "nation: " << sqlNation << " | " << "o_year: " << sqlYear << " | " <<
            // "sum_profit: " << sqlProfit << std::endl; std::cout << "nation: " << nation[i] << " |
            // " << "o_year: " << o_year[i] << " | " << "sum_profit: " << sum_profit[i] <<
            // std::endl; std::cout << std::endl;

            assert(i < nation.size());

            ASSERT_SAME(nation[i], sqlNation);
            ASSERT_SAME(o_year[i], sqlYear);
            ASSERT_SAME(sum_profit[i], sqlProfit);
            i++;
        }
        ASSERT_SAME(i, nation.size());
        if (i == 0) {
            single_cout("Empty result");
        }

        if (ret != SQLITE_DONE) {
            throw std::runtime_error(sqlite3_errmsg(sqlite_db));
        }
    }

#endif
    // Close SQLite DB
    sqlite3_close(sqlite_db);

#ifndef QUERY_PROFILE
    if (orq::benchmarking::tpch_experiment::enabled && pid == 0)
        std::cout << "[TPCH_CORRECTNESS] sqlite=passed" << std::endl;
#endif
    return 0;
}
