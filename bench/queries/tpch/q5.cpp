/**
 * @file q5.cpp
 * @brief Implements TPCH Query 5
 * @date 2024-08-21
 *
 * Equivalent SQL:
 * select
 *     n_name,
 *     sum(l_extendedprice * (1 - l_discount)) as revenue
 * from
 *     customer,
 *     orders,
 *     lineitem,
 *     supplier,
 *     nation,
 *     region
 * where
 *     c_custkey = o_custkey
 *     and l_orderkey = o_orderkey
 *     and l_suppkey = s_suppkey
 *     and c_nationkey = s_nationkey
 *     and s_nationkey = n_nationkey
 *     and n_regionkey = r_regionkey
 *     and r_name = '[REGION]'
 *     and o_orderdate >= date '[DATE]'
 *     and o_orderdate < date '[DATE]' + interval '1' year
 * group by n_name
 * order by revenue desc;
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
constexpr auto TPCH_PLAN_ASSOCIATION = "join(join(join(join(customer,orders),join(nation,region)),lineitem),supplier)";
#else
#ifdef TPCH_DUCKDB_CANONICAL_PLAN
constexpr auto TPCH_PLAN_VARIANT = "duckdb-canonical";
constexpr auto TPCH_PLAN_ASSOCIATION =
    "join(join(join(join(customer,join(nation,region)),orders),lineitem),supplier)";
#else
constexpr auto TPCH_PLAN_VARIANT = "original-orq";
constexpr auto TPCH_PLAN_ASSOCIATION = "orq-original-duplicated-lineitem-branches";
#endif

#endif

int main(int argc, char** argv) {
    orq_init(argc, argv);
    auto pid = runTime->getPartyID();

    auto sf = runTime->getArg<float>("test-size", "r", 0.1);

    // TPCH Q5 query parameters
    const int DATE = 100;
    const int DATE_INTERVAL = 10;  // Arbitrary date interval to account for date format
    const int REGION = 1;

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
    orq::benchmarking::tpch_experiment::Scope experiment("q5", TPCH_PLAN_ASSOCIATION);
    auto db = TPCDatabase<T>(sf, sqlite_db);

    using A = ASharedVector<T>;
    using B = BSharedVector<T>;

    single_cout("Q5 SF " << db.scaleFactor);
    single_cout("[QUERY_PLAN] query=q5 variant=" << TPCH_PLAN_VARIANT
                                                  << " association=" << TPCH_PLAN_ASSOCIATION);

    ////////////////////////////////////////////////////////////////
    // Query

    auto Customer = db.getCustomersTable();
    auto Orders = db.getOrdersTable();
    auto Lineitem = db.getLineitemTable();
    auto Supplier = db.getSupplierTable();
    auto Nation = db.getNationTable();
    auto Region = db.getRegionTable();

    Customer.project({"[CustKey]", "[NationKey]"});
    Orders.project({"[OrderKey]", "[CustKey]", "[OrderDate]"});
    Lineitem.project({"[OrderKey]", "[LineNumber]", "[SuppKey]", "ExtendedPrice", "Discount"});
    Supplier.project({"[SuppKey]", "[NationKey]"});
    Nation.project({"[NationKey]", "[RegionKey]", "[Name]"});
    Region.project({"[RegionKey]", "[Name]"});

#ifdef PRINT_TABLES
    single_cout("CUSTOMER, size " << db.customersSize());
    print_table(Customer.open_with_schema(), pid);

    single_cout("ORDERS, size " << db.ordersSize());
    print_table(Orders.open_with_schema(), pid);

    single_cout("LINEITEM, size " << db.lineitemsSize());
    print_table(Lineitem.open_with_schema(), pid);

    single_cout("SUPPLIER, size " << db.supplierSize());
    print_table(Supplier.open_with_schema(), pid);

    single_cout("NATION, size " << db.nationSize());
    print_table(Nation.open_with_schema(), pid);
#endif

    experiment.begin_query();
    stopwatch::timepoint("Start");
    stopwatch::profile_init();

    // and o_orderdate >= date '[DATE]'
    // and o_orderdate < date '[DATE]' + interval '1' year
    Orders.filter(Orders["[OrderDate]"] >= DATE & Orders["[OrderDate]"] < (DATE + DATE_INTERVAL));
    Orders.deleteColumns({"[OrderDate]"});

    // and r_name = '[REGION]'
    Region.filter(Region["[Name]"] == REGION);

    // and n_regionkey = r_regionkey
    auto SelectedNations = join(Region, Nation, {"[RegionKey]"});
    SelectedNations.project({"[NationKey]", "[Name]"});

    stopwatch::timepoint("Filters");

    Lineitem.addColumns({"Revenue"});
    Lineitem["Revenue"] = Lineitem["ExtendedPrice"] * (-Lineitem["Discount"] + 100) / 100;
    Lineitem.deleteColumns({"ExtendedPrice", "Discount"});

    stopwatch::timepoint("Revenue");

#if defined(TPCH_DUCKDB_CANONICAL_PLAN) || TPCH_SELECTIVITY_PLAN
    // DuckDB canonical association (orientation adapted to ORQ's PK/FK join contract):
    // ((((Customer join (Nation join Region)) join Orders) join Lineitem) join Supplier)
#if TPCH_SELECTIVITY_PLAN == 1
    auto CustomerOrders = join(Customer, Orders, {"[CustKey]"},
        {{"[NationKey]", "[NationKey]", copy<B>}});
    auto SelectedOrders = join(SelectedNations, CustomerOrders, {"[NationKey]"},
        {{"[Name]", "[Name]", copy<B>}});
    CustomerOrders.deleteTable();
#else
    auto SelectedCustomers = join(SelectedNations, Customer, {"[NationKey]"}, {{"[Name]", "[Name]", copy<B>}});

    auto SelectedOrders = join(SelectedCustomers, Orders, {"[CustKey]"},
        {{"[Name]", "[Name]", copy<B>}, {"[NationKey]", "[NationKey]", copy<B>}});

#endif

    auto CustomerItems = join(SelectedOrders, Lineitem, {"[OrderKey]"},
        {{"[Name]", "[Name]", copy<B>}, {"[NationKey]", "[NationKey]", copy<B>}});

    auto FinalItems = join(Supplier, CustomerItems, {"[SuppKey]", "[NationKey]"}, {});
    FinalItems.deleteColumns({"[OrderKey]", "[LineNumber]", "[NationKey]", "[SuppKey]"});

    Customer.deleteTable();
    Orders.deleteTable();
    Lineitem.deleteTable();
    Supplier.deleteTable();
#if !TPCH_SELECTIVITY_PLAN
    SelectedCustomers.deleteTable();
#endif
    SelectedOrders.deleteTable();
    CustomerItems.deleteTable();
#else
    auto SelectedSuppliers = join(SelectedNations, Supplier, {"[NationKey]"},
        {{"[Name]", "[Name]", copy<B>}});
    auto ItemsBySupplier = join(SelectedSuppliers, Lineitem, {"[SuppKey]"},
        {{"[Name]", "[Name]", copy<B>}});
    SelectedSuppliers.deleteTable();

    ItemsBySupplier.deleteColumns({"[NationKey]", "[SuppKey]"});
    Supplier.deleteTable();

    auto NationCustomers = join(Nation, Customer, {"[NationKey]"},
        {{"[Name]", "[Name]", copy<B>}});
    auto NationOrders = join(NationCustomers, Orders, {"[CustKey]"},
        {{"[Name]", "[Name]", copy<B>}});
    auto ItemsByCustomer = join(NationOrders, Lineitem, {"[OrderKey]"},
        {{"[Name]", "[Name]", copy<B>}});
    NationCustomers.deleteTable();
    NationOrders.deleteTable();
    ItemsByCustomer.deleteColumns({"[NationKey]", "[CustKey]", "[SuppKey]"});

    // Collect garbage
    Customer.deleteTable();
    Orders.deleteTable();
    Lineitem.deleteTable();

    auto FinalItems =
        join(ItemsByCustomer, ItemsBySupplier, {"[OrderKey]", "[LineNumber]", "[Name]"}, {});
    FinalItems.deleteColumns({"[OrderKey]", "[LineNumber]"});

    ItemsByCustomer.deleteTable();
    ItemsBySupplier.deleteTable();
#endif

    stopwatch::timepoint("Joins");

    // sort on valid to move invalid columns to the bottom
    FinalItems.aggregate({"[Name]"}, {{"Revenue", "Revenue", sum<A>}});

    // Group-by over nation: at most |Nation| rows.
    FinalItems.sort({ENC_TABLE_VALID});
    FinalItems.tail(Nation.size());

    stopwatch::timepoint("Group");

    FinalItems.addColumns({"[Revenue]"});
    FinalItems.convert_a2b("Revenue", "[Revenue]");
    FinalItems.deleteColumns({"Revenue"});

#ifdef TPCH_SELECTIVITY_EXPERIMENT
    FinalItems.sort({std::make_pair("[Revenue]", DESC), std::make_pair("[Name]", ASC)});
#else
    FinalItems.sort({"[Revenue]"}, DESC);
#endif

    stopwatch::timepoint("Sort");

#ifdef QUERY_PROFILE
    // Include the final mask and shuffle in benchmarking time
    FinalItems.finalize();
    stopwatch::timepoint("Finalize");
#endif

    experiment.finish(FinalItems);
    stopwatch::done();          // print wall clock time
    stopwatch::profile_done();  // print profiling data
    experiment.emit();

    runTime->print_statistics();
    runTime->print_communicator_statistics();

    ////////////////////////////////////////////////////////////////
    // Correctness Test

#ifndef QUERY_PROFILE

    auto resultOpened = FinalItems.open_with_schema();
    auto nation_name_col = FinalItems.get_column(resultOpened, "[Name]");
    auto revenue_col = FinalItems.get_column(resultOpened, "[Revenue]");
    orq::benchmarking::tpch_experiment::result<T>({"NationName","Revenue"}, {nation_name_col,revenue_col});


    // print_table(resultOpened, pid);

    if (pid == 0) {
        // Run Q5 through SQL to verify result
        int ret;
        const char* query = R"sql(
            select
                n.Name,
                sum(l.ExtendedPrice * (100 - l.Discount) / 100) as Revenue
            from
                CUSTOMER as c,
                ORDERS as o,
                LINEITEM as l,
                SUPPLIER as s,
                NATION as n,
                REGION as r
            where
                c.CustKey = o.CustKey
                and l.OrderKey = o.OrderKey
                and l.SuppKey = s.SuppKey
                and c.NationKey = s.NationKey
                and s.NationKey = n.NationKey
                and n.RegionKey = r.RegionKey
                and r.Name = ?
                and o.OrderDate >= ?
                and o.OrderDate < ? + ?
            group by n.Name
            order by Revenue desc
        )sql"
#ifdef TPCH_SELECTIVITY_EXPERIMENT
        ", n.Name "
#endif
        ;
        sqlite3_stmt* stmt;
        ret = sqlite3_prepare_v2(sqlite_db, query, -1, &stmt, NULL);
        // Fill in query placeholders
        sqlite3_bind_int(stmt, 1, REGION);
        sqlite3_bind_int(stmt, 2, DATE);
        sqlite3_bind_int(stmt, 3, DATE);
        sqlite3_bind_int(stmt, 4, DATE_INTERVAL);

        // Assert result against SQL result
        auto res = sqlite3_step(stmt);
        auto i = 0;
        while (res == SQLITE_ROW) {
            if (i >= nation_name_col.size()) throw std::runtime_error("SQLite returned more rows than ORQ");
            int64_t sql_nation_name = sqlite3_column_int64(stmt, 0);
            int64_t sql_revenue = sqlite3_column_int64(stmt, 1);

            ASSERT_SAME(sql_nation_name, nation_name_col[i]);
            ASSERT_SAME(sql_revenue, revenue_col[i]);

            res = sqlite3_step(stmt);
            ++i;
        }
        if (res == SQLITE_ERROR) {
            throw std::runtime_error(sqlite3_errmsg(sqlite_db));
        }
        if (res != SQLITE_DONE) {
            throw std::runtime_error(sqlite3_errmsg(sqlite_db));
        }
        ASSERT_SAME(i, nation_name_col.size());
        if (i == 0) {
            single_cout("Empty result");
        }

        std::cout << i << " rows OK\n";
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
