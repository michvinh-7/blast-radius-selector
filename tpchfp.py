"""Fingerprints for the two TPC-H tests in tests/test_executor.py (no query is executed by either):
  test_execute_tpch   : parse_one(sql) + pickle round-trip for each TPC-H query (no TPC-H query has
                        the `execute` flag, so the pool workers return None);
  test_optimized_tpch : transpile(sql, duckdb) and transpile(optimized, duckdb) for each pair, which is
                        all the test feeds duckdb.
  python tpchfp.py ROOT DATA.json OUT.json            # write {"parse": [...], "duck": [...]}
  python tpchfp.py ROOT DATA.json - PINNED.json       # print which tests are unchanged: "parse duck"
Any exception: prints nothing (caller keeps both tests)."""
import hashlib
import json
import pickle
import sys

root, data_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
from sqlglot import parse_one, transpile  # noqa: E402

pairs = json.load(open(data_path))["pairs"]
h = lambda s: hashlib.sha1(s.encode()).hexdigest()  # noqa: E731
res = {"parse": [h(repr(pickle.loads(pickle.dumps(parse_one(sql))))) for sql, _ in pairs],
       "duck": [h(transpile(sql, write="duckdb")[0] + "\x00" + transpile(opt, write="duckdb")[0]) for sql, opt in pairs]}
if len(sys.argv) > 4:
    pinned = json.load(open(sys.argv[4]))
    print(" ".join(k for k in ("parse", "duck") if res[k] == pinned[k]))
else:
    json.dump(res, open(out, "w"))
