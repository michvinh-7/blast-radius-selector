"""Build map.pkl: per-test line coverage, file-open map and source structure.

Inputs (produced by tools/iso_plugin.py and a collection-only coverage run):
  COVDIR/*.db         one coverage database per test (context = test id)
  OPENDIR/*.json      per-test files opened, plus collect_*.json for collection time
  GFILE               json: {relpath: [lines executed while collecting]}
Output: map.pkl
"""
import ast
import collections
import glob
import io
import json
import os
import pickle
import re
import sqlite3
import sys
import tokenize
import zlib

ROOT = sys.argv[1]            # sqlglot checkout
TESTS_JSON = sys.argv[2]      # tests.json
COVDIR = sys.argv[3]
OPENDIR = sys.argv[4]
GDIR = sys.argv[5]
INSTDIR = sys.argv[7]       # inst/*.json from the class-hook run
STATUS = sys.argv[8:]       # status_*.tsv files (session index -> test id)
OUT = sys.argv[6]

tests = json.load(open(TESTS_JSON))["tests"]
ids = [t["id"] for t in tests]
index = {t: i for i, t in enumerate(ids)}
file_tests = collections.defaultdict(int)
for t in tests:
    file_tests[t["file"]] |= 1 << index[t["id"]]


# ---------------------------------------------------------------- coverage
# raw[file][line] -> bitmask of tests
raw = collections.defaultdict(lambda: collections.defaultdict(int))
nstat = 0
covdbs = []
for d_ in COVDIR.split(":"):
    covdbs += sorted(glob.glob(os.path.join(d_, "*.db")))
for db in covdbs:
    con = sqlite3.connect(db)
    cur = con.cursor()
    files = {r[0]: r[1] for r in cur.execute("select id, path from file")}
    ctxs = {r[0]: r[1] for r in cur.execute("select id, context from context")}
    # line_bits: file_id, context_id, numbits
    for fid, cid, nb in cur.execute("select file_id, context_id, numbits from line_bits"):
        ctx = ctxs[cid]
        if ctx not in index:
            continue
        bit = 1 << index[ctx]
        path = files[fid]
        rel = os.path.relpath(path, ROOT) if os.path.isabs(path) else path
        d = raw[rel]
        # numbits: little-endian bytes of a bitset of line numbers
        for byte_i, byte in enumerate(nb):
            if byte:
                for b in range(8):
                    if byte >> b & 1:
                        d[byte_i * 8 + b] |= bit
    con.close()
    nstat += 1
print("coverage dbs:", nstat, "files:", len(raw), file=sys.stderr)

# collection-time coverage, one run per test file: gcov[rel][line] -> tests of the files whose
# collection executed that line
import coverage

gmask = collections.defaultdict(lambda: collections.defaultdict(int))
for tf, fm in file_tests.items():
    name = tf.replace("/", "_")
    cf = os.path.join(GDIR, name + ".cov")
    if not os.path.exists(cf):
        print("missing collection coverage for", tf, file=sys.stderr)
        continue
    cd = coverage.CoverageData(basename=cf)
    cd.read()
    for fpath in cd.measured_files():
        rel = os.path.relpath(fpath, ROOT)
        for ln in cd.lines(fpath) or []:
            gmask[rel][ln] |= fm


# ---------------------------------------------------------------- structure
def analyse(src):
    """Return per-file structure for a python source."""
    tree = ast.parse(src)
    n = src.count("\n") + 2
    kind = bytearray(n + 1)     # 0 none, 1 definition-time, 2 run-time, 3 docstring
    sid = [0] * (n + 1)         # statement-range id (0 = none)
    stmts = [None]              # (start, end)
    funcs = []                  # (start, end) of every function (incl. decorators)
    classes = []                # (start, end)
    dnames = {}                 # first line of D statement -> (end, names)
    lams = []                   # (first, last) inner lines of multi-line lambdas in D statements

    def mark(lo, hi, k):
        sidx = len(stmts)
        stmts.append((lo, hi))
        for ln in range(lo, min(hi, n) + 1):
            if kind[ln] == 0:
                kind[ln] = k
            sid[ln] = sidx
        return sidx

    def targets(t, out):
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, ast.Attribute):
            out.append(t.attr)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                targets(e, out)
        elif isinstance(t, ast.Subscript):
            targets(t.value, out)
        elif isinstance(t, ast.Starred):
            targets(t.value, out)

    def kind_of(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return "def"
        if isinstance(node, ast.ClassDef):
            return "class"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "import"
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            return "assign"
        return "other"

    def names_of(node):
        out = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                targets(t, out)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets(node.target, out)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.append((a.asname or a.name).split(".")[0])
        return out

    def is_doc(stmt):
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )

    def visit_body(body, in_func, scope_first=True):
        for i, st in enumerate(body):
            visit(st, in_func, doc_ok=(i == 0))

    def visit(st, in_func, doc_ok=False):
        k = 2 if in_func else 1
        if doc_ok and is_doc(st):
            mark(st.lineno, st.end_lineno, 3)
            return
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([st.lineno] + [d.lineno for d in st.decorator_list])
            hdr_end = st.body[0].lineno - 1
            # header ends where the body's first statement starts (or the line holding ':' )
            hdr_end = max(hdr_end, st.lineno)
            if st.body and st.body[0].lineno == st.lineno:
                hdr_end = st.lineno
            mark(first, hdr_end, k)
            if not in_func:
                dnames[first] = (hdr_end, names_of(st), kind_of(st))
            if isinstance(st, ast.ClassDef):
                classes.append((first, st.end_lineno, st.name))
                visit_body(st.body, in_func)
            else:
                funcs.append((first, st.end_lineno))
                visit_body(st.body, True)
            return
        if isinstance(st, (ast.If, ast.While)):
            hdr_end = st.body[0].lineno - 1
            mark(st.lineno, max(st.lineno, hdr_end), k)
            visit_body(st.body, in_func)
            if st.orelse:
                visit_body(st.orelse, in_func)
            return
        if isinstance(st, (ast.For, ast.AsyncFor)):
            hdr_end = st.body[0].lineno - 1
            mark(st.lineno, max(st.lineno, hdr_end), k)
            visit_body(st.body, in_func)
            if st.orelse:
                visit_body(st.orelse, in_func)
            return
        if isinstance(st, (ast.With, ast.AsyncWith)):
            hdr_end = st.body[0].lineno - 1
            mark(st.lineno, max(st.lineno, hdr_end), k)
            visit_body(st.body, in_func)
            return
        if isinstance(st, ast.Try) or st.__class__.__name__ == "TryStar":
            visit_body(st.body, in_func)
            for h in st.handlers:
                hdr_end = h.body[0].lineno - 1
                mark(h.lineno, max(h.lineno, hdr_end), k)
                visit_body(h.body, in_func)
            visit_body(st.orelse, in_func)
            visit_body(st.finalbody, in_func)
            return
        if isinstance(st, ast.Match):
            hdr_end = st.cases[0].pattern.lineno - 1 if st.cases else st.lineno
            mark(st.lineno, max(st.lineno, hdr_end), k)
            for c in st.cases:
                visit_body(c.body, in_func)
            return
        # simple statement
        mark(st.lineno, st.end_lineno, k)
        if not in_func:
            dnames[st.lineno] = (st.end_lineno, names_of(st), kind_of(st))
            for node in ast.walk(st):
                if isinstance(node, ast.Lambda) and node.end_lineno > node.lineno:
                    lams.append((node.lineno + 1, node.end_lineno))

    visit_body(tree.body, False)
    return {
        "kind": bytes(kind),
        "sid": sid,
        "stmts": stmts,
        "funcs": sorted(funcs),
        "classes": sorted(classes),
        "dnames": dnames,
        "lams": sorted(set(lams)),
    }


# ---------------------------------------------------------------- build
struct = {}
for root, _, fs in os.walk(os.path.join(ROOT, "sqlglot")):
    for f in fs:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            rel = os.path.relpath(p, ROOT)
            try:
                struct[rel] = analyse(open(p, encoding="utf-8").read())
            except SyntaxError:
                print("syntax error?", rel, file=sys.stderr)
for root, _, fs in os.walk(os.path.join(ROOT, "tests")):
    for f in fs:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            rel = os.path.relpath(p, ROOT)
            struct[rel] = analyse(open(p, encoding="utf-8").read())

# line -> set id, dedup bitmasks
sets = {}
setlist = []


def setid(mask):
    s = sets.get(mask)
    if s is None:
        s = len(setlist)
        sets[mask] = s
        setlist.append(mask)
    return s


lines = {}
for rel, d in raw.items():
    if rel in struct:
        lines[rel] = {ln: setid(m) for ln, m in d.items() if m}

# ---------------------------------------------------------------- ident refs
# For identifiers bound by definition-time statements: union of tests that executed
# any statement mentioning the identifier (outside of its own defining statement).
need = set()
for rel, s in struct.items():
    for first, (end, names, kd) in s["dnames"].items():
        need.update(names)
ref = collections.defaultdict(int)
ident_lines = collections.defaultdict(lambda: collections.defaultdict(set))
for rel, s in struct.items():
    if not rel.startswith("sqlglot/") and not rel.startswith("tests/"):
        continue
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        continue
    lm = lines.get(rel, {})
    gm = gmask.get(rel, {})
    stm = s["stmts"]
    sidv = s["sid"]
    stmt_mask = {}
    for ln, sd in lm.items():
        if ln < len(sidv) and sidv[ln]:
            stmt_mask[sidv[ln]] = stmt_mask.get(sidv[ln], 0) | setlist[sd]
    for ln, mk in gm.items():
        if ln < len(sidv) and sidv[ln]:
            stmt_mask[sidv[ln]] = stmt_mask.get(sidv[ln], 0) | mk
    bind_lines = {fl: (e, kd) for fl, (e, nm, kd) in s["dnames"].items() if nm}
    seen_op = {}
    prev = None
    for tk in toks:
        word = None
        ln0 = tk.start[0]
        if ln0 in bind_lines:
            e, kd = bind_lines[ln0]
            if kd == "import":
                prev = tk
                continue
            if kd == "assign" and not seen_op.get(ln0):
                if tk.type == tokenize.OP and (tk.string == "=" or (tk.string.endswith("=") and tk.string not in ("==", "!=", "<=", ">=")) or tk.string == ":"):
                    seen_op[ln0] = True
                prev = tk
                continue
            if kd in ("def", "class") and prev is not None and prev.type == tokenize.NAME and prev.string in ("def", "class") and prev.start[0] == ln0:
                prev = tk
                continue
        prev = tk
        if tk.type == tokenize.NAME and tk.string in need:
            word = tk.string
        elif tk.type == tokenize.STRING and tk.string.strip("'\"") in need:
            word = tk.string.strip("'\"")
        if word:
            ln = tk.start[0]
            if ln >= len(sidv):
                continue
            m = stmt_mask.get(sidv[ln], 0) | (setlist[lm[ln]] if ln in lm else 0) | gm.get(ln, 0)
            if m:
                ref[word] |= m
ref = dict(ref)

# ---------------------------------------------------------------- class usage
sess_id = {}
for fn in STATUS:
    for ln in open(fn):
        parts = ln.rstrip("\n").split("\t")
        if len(parts) >= 2 and parts[0].isdigit():
            sess_id[int(parts[0])] = parts[1]
inst = collections.defaultdict(int)
for fn in glob.glob(os.path.join(INSTDIR, "*.json")):
    idx = int(os.path.basename(fn).split(".")[0])
    tid = sess_id.get(idx)
    if tid not in index:
        continue
    bit = 1 << index[tid]
    for key in json.load(open(fn)):
        inst[key] |= bit
inst = dict(inst)
print("class keys with usage:", len(inst), file=sys.stderr)

# ---------------------------------------------------------------- opens
opens = collections.defaultdict(int)
for fn in glob.glob(os.path.join(OPENDIR, "*.json")):
    base = os.path.basename(fn)
    d = json.load(open(fn))
    for rel, owners in d.items():
        for o in owners:
            if o.startswith("collect:"):
                fpath = o[len("collect:"):].split("::")[0]
                m = file_tests.get(fpath, 0)
            elif o in index:
                m = 1 << index[o]
            else:
                m = 0
            if m:
                opens[rel] |= m
opens = {k: v for k, v in opens.items() if not k.endswith(".pyc")}

out = {
    "ids": ids,
    "files_of_test": [t["file"] for t in tests],
    "sets": setlist,
    "lines": lines,
    "gl": {rel: {ln: setid(m) for ln, m in d.items()} for rel, d in gmask.items() if rel in struct},
    "struct": struct,
    "ref": ref,
    "inst": inst,
    "opens": opens,
    "file_tests": dict(file_tests),
}
blob = zlib.compress(pickle.dumps(out, protocol=4), 9)
open(OUT, "wb").write(blob)
print("wrote", OUT, len(blob), "bytes; sets", len(setlist), "ref", len(ref), "opens", len(opens), file=sys.stderr)

# ---------------------------------------------------------------- source snapshot
src = {}
for rel in struct:
    src[rel] = zlib.compress(open(os.path.join(ROOT, rel), "rb").read(), 9)
srcblob = pickle.dumps(src, protocol=4)
open(os.path.join(os.path.dirname(OUT) or ".", "src.bin"), "wb").write(srcblob)
print("wrote src.bin", len(srcblob), file=sys.stderr)
