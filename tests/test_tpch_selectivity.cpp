#include "../bench/queries/tpch/tpch_experiment.h"
#include <cassert>
#include <iostream>
using namespace orq::benchmarking;
using namespace COMPILED_MPC_PROTOCOL_NAMESPACE;
using namespace orq::service;

void pure_helpers() {
    using namespace selectivity;
    assert(md5("") == "d41d8cd98f00b204e9800998ecf8427e");
    assert(md5("abc") == "900150983cd24fb0d6963f7d28e17f72");
    assert(Percent::parse("0.5").count(100) == 1);
    assert(Percent::parse("1/2").count(100) == 1);
    assert(Percent::parse("1073500/150143").count(598660) == 42803);
    assert(Percent::parse("1073525/150143").count(598660) == 42804);
    assert(Percent::parse("0").count(10) == 0);
    assert(Percent::parse("100").count(10) == 10);
    for (auto value : {"NaN", "-1", "101", "1/0", "1/2/3", "1.", ".5", "18446744073709551616"}) {
        bool failed=false;try { Percent::parse(value); } catch (const std::exception&) { failed=true; }
        assert(failed);
    }
    for (auto value : {"part=1,part=2", "customer=1", "part=1,", "part"}) {
        bool failed=false;try { parse(value); } catch (const std::exception&) { failed=true; }
        assert(failed);
    }
    std::vector<std::vector<int64_t>> keys={{10,0},{10,1},{11,0},{12,0}};
    auto one=indices(keys,1,20260908), two=indices(keys,2,20260908), all=indices(keys,4,20260908);
    assert(one == std::vector<size_t>{3});
    assert(two == (std::vector<size_t>{1,3}));
    assert(all == (std::vector<size_t>{0,1,2,3}));
    assert(indices(keys,0,20260908).empty());
    assert(indices(keys,2,20260908)==two);
    bool duplicate=false;try {indices({{1},{1}},1,1);} catch (const std::invalid_argument&) {duplicate=true;}
    assert(duplicate);
    // Gathering must resize ORQ's fixed-assignment Vector explicitly.
    orq::Vector<int64_t> values={100,101,102,103};
    orq::Vector<int64_t> reduced(2);
    for(size_t i=0;i<2;++i)reduced[i]=values[two[i]];
    values.resize(2);values=reduced;
    assert(values.same_as({101,103}));
}
void compound_join() {
    using T=int64_t;using A=ASharedVector<T>;using B=BSharedVector<T>;
    EncodedTable<T> pk=secret_share<T>({orq::Vector<T>{1,1,2},orq::Vector<T>{1,2,1},orq::Vector<T>{10,20,30}},
                           {"[K1]","[K2]","Payload"});
    EncodedTable<T> fk=secret_share<T>({orq::Vector<T>{1,1,2,7},orq::Vector<T>{1,1,1,1},orq::Vector<T>{0,1,2,3}},
                           {"[K1]","[K2]","[Row]"});
    auto joined=pk.inner_join(fk,{"[K1]","[K2]"},{{"Payload","Payload",orq::aggregators::copy<A>}});
    joined.sort({"[Row]"});
    auto opened=joined.open_with_schema();
    auto payload=joined.get_column(opened,"Payload");
    if(runTime->getPartyID()==0)assert(payload.same_as({10,10,30}));
}
int main(int argc,char** argv) {
    pure_helpers();
    orq_init(argc,argv);
    compound_join();
    if(runTime->getPartyID()==0)std::cout << "selectivity helpers and compound PK/FK join passed\n";
}
