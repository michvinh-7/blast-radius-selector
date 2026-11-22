"""Fix B: does a patch change what tests/test_executor.py::test_execute_tpcds builds?

The test parses each TPC-DS query, pickles it to a worker, optimizes it against the schema,
plans it, compiles the plan's expressions to Python and executes them, then compares with
duckdb running transpile(sql, write="duckdb"). This script rebuilds everything up to (not
including) execution and hashes it per query:

    pickle round-trip AST, optimized AST, plan steps (+ generated Python code), duckdb SQL

Run against sqlglot sources at ROOT. Execution-time code (sqlglot/executor/) is excluded by
the caller, which only uses this when the patch touches nothing outside sqlglot/ or inside
sqlglot/executor/.

  python fixb.py ROOT DATA.json OUT.json             # write per-query fingerprints
  python fixb.py ROOT DATA.json - PINNED.json        # exit 0 if identical, 3 at first diff
Any other exit (exception, crash, timeout) means "cannot vouch": the caller keeps the test.
"""

import gc; gc.set_threshold(100000, 50, 100)  # short-lived process: collect cyclic garbage rarely (CPU is scored)
import hashlib
import json
import os
import pickle
import re
import sys

root, data_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
pinned = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else None
sys.path.insert(0, root)

from sqlglot import exp, parse_one, transpile  # noqa: E402
from sqlglot.executor.python import Python  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.planner import Plan  # noqa: E402


def _fastexit(code):
    # skip interpreter teardown (a full GC + finalizers): CPU is scored
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)




data = json.load(open(data_path))
SCHEMA = data["schema"]
EXPR = exp.Expr if hasattr(exp, "Expr") else exp.Expression
gen = Python().generator()
ADDR = re.compile(r" \(\d{6,}\)|0x[0-9a-fA-F]{6,}")  # plan step names embed id()


def step_code(step):
    parts = [type(step).__name__, repr(sorted(step.__dict__.keys()))]
    for k, v in sorted(step.__dict__.items()):
        if k in ("dependencies", "dependents"):
            parts.append(f"{k}={sorted(d.id if hasattr(d, 'id') else d.name for d in v)}")
            continue
        vals = v if isinstance(v, (list, tuple)) else (list(v.values()) if isinstance(v, dict) else [v])
        for x in vals:
            if isinstance(x, EXPR):
                parts.append(f"{k}:{x!r}")
                parts.append(f"{k}#py:{gen.generate(x)}")
            else:
                parts.append(f"{k}={x!r}"[:2000])
    return ADDR.sub("", "\n".join(parts))


res = []
for i, sql in enumerate(data["queries"]):
    h = hashlib.sha1()
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    h.update(repr(e).encode())
    o = optimize(e, SCHEMA, leave_tables_isolated=True)
    h.update(repr(o).encode())
    for code in sorted(step_code(s) for s in Plan(o).dag):  # same-named steps: order by full text
        h.update(code.encode())
    h.update(transpile(sql, write="duckdb")[0].encode())
    res.append(h.hexdigest())
    if pinned is not None and res[-1] != pinned[i]:
        _fastexit(3)

if pinned is not None:
    _fastexit(0 if res == pinned else 3)
with open(out, "w") as _f:
    json.dump(res, _f)
_fastexit(0)
