#pragma once

// Public, benchmark-only input selection. This never seeds protocol randomness.
#include <algorithm>
#include <array>
#include <boost/uuid/detail/md5.hpp>
#include <cstdint>
#include <iomanip>
#include <limits>
#include <map>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <sodium.h>

namespace orq::benchmarking::selectivity {
inline uint64_t unsigned_integer(const std::string& value) {
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("expected unsigned decimal integer: " + value);
    size_t parsed = 0;
    auto result = std::stoull(value, &parsed);
    if (parsed != value.size()) throw std::invalid_argument("invalid integer");
    return result;
}
struct Percent {
    uint64_t numerator = 100, denominator = 1;
    static Percent parse(const std::string& value) {
        auto slash = value.find('/');
        uint64_t n, d = 1;
        if (slash != std::string::npos) {
            n = unsigned_integer(value.substr(0, slash));
            d = unsigned_integer(value.substr(slash + 1));
        } else {
            auto dot = value.find('.');
            auto digits = value;
            if (dot != std::string::npos) {
                auto decimals = value.size() - dot - 1;
                if (!dot || !decimals || decimals > 18)
                    throw std::invalid_argument("percentage needs 1..18 decimal places");
                digits.erase(dot, 1);
                for (size_t i = 0; i < decimals; ++i) d *= 10;
            }
            n = unsigned_integer(digits);
        }
        if (!d || static_cast<__uint128_t>(n) > static_cast<__uint128_t>(100) * d)
            throw std::invalid_argument("percentage outside [0,100]");
        auto g = std::gcd(n, d);
        return {n / g, d / g};
    }
    size_t count(size_t total) const {
        __uint128_t product = static_cast<__uint128_t>(numerator) * total;
        __uint128_t divisor = static_cast<__uint128_t>(denominator) * 100;
        auto quotient = product / divisor, remainder = product % divisor;
        return static_cast<size_t>(quotient + (remainder >= divisor - remainder));
    }
    std::string str() const { return std::to_string(numerator) + "/" + std::to_string(denominator); }
};
inline std::map<std::string, Percent> parse(const std::string& value) {
    std::map<std::string, Percent> result;
    if (value.empty()) return result;
    size_t start = 0;
    do {
        auto end = value.find(',', start);
        auto item = value.substr(start, end == std::string::npos ? end : end - start);
        auto equal = item.find('=');
        if (equal == std::string::npos) throw std::invalid_argument("selectivity requires relation=percent");
        auto rel = item.substr(0, equal);
        if (rel != "lineitem" && rel != "orders" && rel != "part")
            throw std::invalid_argument("unsupported selectivity relation: " + rel);
        if (!result.emplace(rel, Percent::parse(item.substr(equal + 1))).second)
            throw std::invalid_argument("duplicate selectivity relation: " + rel);
        if (end == std::string::npos) break;
        start = end + 1;
    } while (true);
    return result;
}
inline std::string md5(const std::string& value) {
    boost::uuids::detail::md5 hash;
    hash.process_bytes(value.data(), value.size());
    boost::uuids::detail::md5::digest_type words;
    hash.get_digest(words);
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    // Boost's digest words are intended for UUID formatting, not a native-byte dump.
    for (auto word : words) out << std::setw(sizeof(word) == 1 ? 2 : 8) << static_cast<uint32_t>(word);
    return out.str();
}
inline std::string hex(const unsigned char* bytes, size_t n) {
    static constexpr char digits[] = "0123456789abcdef";
    std::string result;
    result.reserve(2 * n);
    for (size_t i = 0; i < n; ++i) { result += digits[bytes[i] >> 4]; result += digits[bytes[i] & 15]; }
    return result;
}
struct Hash {
    crypto_hash_sha256_state state;
    Hash() { crypto_hash_sha256_init(&state); }
    void integer(uint64_t value) {
        unsigned char data[8];
        for (int i = 0; i < 8; ++i) data[i] = static_cast<unsigned char>(value >> (8 * i));
        crypto_hash_sha256_update(&state, data, 8);
    }
    void string(const std::string& value) {
        integer(value.size());
        crypto_hash_sha256_update(&state, reinterpret_cast<const unsigned char*>(value.data()), value.size());
    }
    std::string finish() {
        unsigned char digest[crypto_hash_sha256_BYTES];
        crypto_hash_sha256_final(&state, digest);
        return hex(digest, sizeof(digest));
    }
};
// Numeric composite keys resolve MD5 collisions. Return retained indices in original order.
inline std::vector<size_t> indices(const std::vector<std::vector<int64_t>>& keys,
                                   size_t k, uint64_t seed) {
    if (k > keys.size()) throw std::invalid_argument("selected count exceeds input");
    std::vector<std::pair<std::string, size_t>> ranked;
    ranked.reserve(keys.size());
    for (size_t i = 0; i < keys.size(); ++i) {
        std::string identity = std::to_string(seed);
        for (auto key : keys[i]) identity += ":" + std::to_string(key);
        ranked.emplace_back(md5(identity), i);
    }
    std::sort(ranked.begin(), ranked.end(), [&](const auto& a, const auto& b) {
        if (a.first != b.first) return a.first < b.first;
        return keys[a.second] < keys[b.second];
    });
    for (size_t i = 1; i < ranked.size(); ++i)
        if (keys[ranked[i-1].second] == keys[ranked[i].second])
            throw std::invalid_argument("duplicate primary key in selectivity input");
    std::vector<size_t> selected;
    for (size_t i = 0; i < k; ++i) selected.push_back(ranked[i].second);
    std::sort(selected.begin(), selected.end());
    return selected;
}
} // namespace orq::benchmarking::selectivity
