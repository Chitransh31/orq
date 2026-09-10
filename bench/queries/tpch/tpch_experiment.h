#pragma once
#include "orq.h"
#include "tpch_selectivity.h"
#include <chrono>
#include <fstream>

namespace orq::benchmarking::tpch_experiment {
using namespace COMPILED_MPC_PROTOCOL_NAMESPACE;
using namespace orq::service;
#ifdef TPCH_SELECTIVITY_EXPERIMENT
inline constexpr bool enabled = true;
#else
inline constexpr bool enabled = false;
#endif
inline std::vector<std::string> events;
inline std::string query_id;
inline std::string quoted(const std::string& value) {
    std::string s = "\"";
    for (char c : value) {
        if (c == '"' || c == '\\') s += '\\';
        if (static_cast<unsigned char>(c) < 32) throw std::invalid_argument("control character in metadata");
        s += c;
    }
    return s + "\"";
}
inline std::string strings(const std::vector<std::string>& values) {
    std::string s = "[";
    for (const auto& v : values) { if (s.size() > 1) s += ','; s += quoted(v); }
    return s + "]";
}
inline std::string leaf(std::string name) {
    const std::map<std::string, std::string> names = {{"CUSTOMER","customer"},{"ORDERS","orders"},
        {"LINEITEMS","lineitem"},{"LINEITEM","lineitem"},{"PART","part"},{"PARTSUPP","partsupp"},
        {"SUPPLIER","supplier"},{"NATION","nation"},{"REGION","region"}};
    return names.count(name) ? names.at(name) : name;
}
inline std::string association(std::string a, std::string b) {
    a = leaf(a); b = leaf(b);
    if (a > b) std::swap(a,b);
    return "join(" + a + "," + b + ")";
}
template<class Signature> struct JoinSignature;
template<class R, class C, class Right, class Keys, class Spec, class Options>
struct JoinSignature<R(C::*)(Right, Keys, Spec, Options)> { using specification = Spec; };
template<class Table>
Table join(Table& left, Table& right, std::vector<std::string> keys,
           typename JoinSignature<decltype(&Table::inner_join)>::specification spec = {}) {
    if constexpr (!enabled) return left.inner_join(right, keys, spec);
    auto tree = association(left.name(), right.name());
    auto l = left.size(), r = right.size();
    auto result = left.inner_join(right, keys, spec);
    if constexpr (enabled) {
        result.tableName = tree;
        std::ostringstream out;
        out << "[TPCH_JOIN] {\"association\":" << quoted(tree)
            << ",\"left\":" << quoted(leaf(left.name())) << ",\"right\":" << quoted(leaf(right.name()))
            << ",\"left_columns\":" << strings(left.getColumnNames()) << ",\"right_columns\":" << strings(right.getColumnNames())
            << ",\"keys\":" << strings(keys) << ",\"left_rows\":" << l << ",\"right_rows\":" << r
            << ",\"output_rows\":" << result.size() << ",\"columns\":" << strings(result.getColumnNames())
            << ",\"value_bits\":64}";
        events.push_back(out.str());
    }
    return result;
}
inline uint64_t bytes() {
    uint64_t n = 0;
    for (auto& worker : runTime->workers) n += worker.getCommunicator()->getBytesSent();
    return n;
}
struct Scope {
    using Clock = std::chrono::steady_clock;
    Clock::time_point start = Clock::now(), query_start = start;
    uint64_t initial_bytes = 0, query_start_bytes = 0;
    std::string expected, scope_event;
    Scope(std::string query, std::string association) : expected(std::move(association)) {
        if constexpr (enabled) { query_id = std::move(query); events.clear(); initial_bytes = bytes(); }
    }
    void begin_query() {
        if constexpr (enabled) { query_start_bytes = bytes(); query_start = Clock::now(); }
    }
    template<class Table> void finish(Table& table) {
        if constexpr (enabled) {
            auto end = Clock::now(); auto final_bytes = bytes();
            auto observed = leaf(table.name());
            // Original Q5 is explicitly a duplicated-branch non-DuckDB baseline.
            if (expected != "orq-original-duplicated-lineitem-branches" && observed != expected)
                throw std::runtime_error("actual TPCH association differs from QUERY_PLAN: " + observed);
            std::ostringstream out;
            out << std::setprecision(17) << "[TPCH_SCOPE] {\"party\":" << runTime->getPartyID()
                << ",\"query\":" << quoted(query_id) << ",\"association\":" << quoted(observed)
                << ",\"input_setup_seconds\":" << std::chrono::duration<double>(query_start-start).count()
                << ",\"query_seconds\":" << std::chrono::duration<double>(end-query_start).count()
                << ",\"workload_seconds\":" << std::chrono::duration<double>(end-start).count()
                << ",\"initial_bytes\":" << initial_bytes << ",\"input_bytes\":" << query_start_bytes-initial_bytes
                << ",\"query_bytes\":" << final_bytes-query_start_bytes << ",\"bytes_sent_total\":" << final_bytes << "}";
            scope_event = out.str();
        }
    }
    void emit() const {
        if constexpr (enabled) {
            std::cout << scope_event << std::endl;
            if (runTime->getPartyID() == 0) for (const auto& event : events) std::cout << event << '\n';
        }
    }
};
// Export only opened logical output in correctness builds, independent of padding/shuffle order.
template<class T>
void result(const std::vector<std::string>& schema, const std::vector<orq::Vector<T>>& columns) {
    if constexpr (!enabled) return;
    if (runTime->getPartyID() != 0) return;
    if (schema.size() != columns.size()) throw std::runtime_error("result schema mismatch");
    size_t n = columns.empty() ? 0 : columns.front().size();
    std::vector<std::vector<int64_t>> rows(n);
    for (const auto& col : columns) {
        if (col.size() != n) throw std::runtime_error("result column length mismatch");
        for (size_t i = 0; i < n; ++i) rows[i].push_back(col[i]);
    }
    std::sort(rows.begin(), rows.end());
    std::string data;
    auto integer = [&](uint64_t value) { for (int i=0;i<8;++i) data += static_cast<char>(value >> (i*8)); };
    integer(schema.size());
    for (auto name : schema) { integer(name.size()); data += name; }
    integer(n);
    for (auto& row : rows) for (auto value : row) integer(static_cast<uint64_t>(value));
    unsigned char hash[crypto_hash_sha256_BYTES];
    crypto_hash_sha256(hash, reinterpret_cast<const unsigned char*>(data.data()), data.size());
    std::ofstream file("tpch-result.bin", std::ios::binary | std::ios::trunc);
    file.write(data.data(), data.size()); file.close();
    if (!file) throw std::runtime_error("cannot write tpch-result.bin");
    std::cout << "[TPCH_RESULT] {\"query\":" << quoted(query_id) << ",\"rows\":" << n
              << ",\"sha256\":" << quoted(selectivity::hex(hash,sizeof(hash))) << "}" << std::endl;
}
} // namespace orq::benchmarking::tpch_experiment
// A thin wrapper also preserves legacy query behavior when experiment instrumentation is disabled.
using orq::benchmarking::tpch_experiment::join;
