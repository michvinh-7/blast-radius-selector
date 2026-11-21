"""Blast radius selector: choose the sqlglot tests that can catch each patch.

Reads /data/tests.json and /data/commits/*.patch, answers from a precomputed map
(map.bin: per-test line coverage, fixture reads, source structure) built from the
pinned sqlglot commit, and prints one JSON line per commit.
"""

import io
import json
import os
import pickle
import re
import sys
import tokenize
import zlib
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
APP = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SQLGLOT_SRC", "/nonexistent"))
MAX_WORKERS = 8
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


# ------------------------------------------------------------------ data
tests = json.loads((DATA / "tests.json").read_text())["tests"]
num_by_id = {t["id"]: t["number"] for t in tests}
dur_ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}

M = pickle.loads(zlib.decompress((APP / "map.bin").read_bytes()))
IDS = M["ids"]
SETS = M["sets"]
LINES = M["lines"]
GL = M["gl"]
STRUCT = M["struct"]
REF = M["ref"]
INST = M["inst"]
OPENS = M["opens"]
FILE_TESTS = M["file_tests"]
FILE_OF_TEST = M["files_of_test"]

# map index -> mask bit (we work in map-index space and translate at the end)
idx_known = [i for i, tid in enumerate(IDS) if tid in num_by_id]
ALL = 0
for i in idx_known:
    ALL |= 1 << i
# tests present at run time but unknown to the map: always safe to include when we go wide
EXTRA_NUMS = [t["number"] for t in tests if t["id"] not in set(IDS)]


def mask_to_numbers(mask):
    out = []
    while mask:
        low = mask & -mask
        i = low.bit_length() - 1
        mask ^= low
        n = num_by_id.get(IDS[i])
        if n is not None:
            out.append(n)
    return out


def tests_of_file(path):
    return FILE_TESTS.get(path, 0)


# ------------------------------------------------------------------ patch parsing
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileDiff:
    def __init__(self):
        self.old = None
        self.new = None
        self.is_new = False
        self.is_deleted = False
        self.binary = False
        self.hunks = []  # list of blocks


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    old_ln = 0
    in_hunk = False
    block = None
    blocks = None

    def close_block():
        nonlocal block
        if block is not None and (block["rem"] or block["add"]):
            blocks.append(block)
        block = None

    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("diff --git "):
            if cur is not None:
                close_block()
            cur = FileDiff()
            files.append(cur)
            blocks = cur.hunks
            in_hunk = False
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            continue
        if cur is None:
            continue
        if not in_hunk:
            if line.startswith("new file mode"):
                cur.is_new = True
            elif line.startswith("deleted file mode"):
                cur.is_deleted = True
            elif line.startswith("rename from "):
                cur.old = line[len("rename from "):]
            elif line.startswith("rename to "):
                cur.new = line[len("rename to "):]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
            elif line.startswith("--- "):
                p = line[4:].strip()
                if p == "/dev/null":
                    cur.is_new = True
                elif p.startswith("a/"):
                    cur.old = p[2:]
            elif line.startswith("+++ "):
                p = line[4:].strip()
                if p == "/dev/null":
                    cur.is_deleted = True
                elif p.startswith("b/"):
                    cur.new = p[2:]
        m = HUNK.match(line)
        if m:
            close_block()
            in_hunk = True
            start = int(m.group(1))
            length = 1 if m.group(2) is None else int(m.group(2))
            old_ln = start + 1 if length == 0 else start
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            if block is None:
                block = {"pos": old_ln, "rem": [], "add": []}
            block["add"].append(line[1:])
        elif line.startswith("-"):
            if block is None:
                block = {"pos": old_ln, "rem": [], "add": []}
            block["rem"].append((old_ln, line[1:]))
            old_ln += 1
        else:
            close_block()
            old_ln += 1
    if cur is not None:
        close_block()
    return files


# ------------------------------------------------------------------ source helpers
_SRC = None


def read_old(path):
    """Original (pinned commit) text of a python file, split into lines."""
    global _SRC
    if _SRC is None:
        try:
            _SRC = pickle.loads((APP / "src.bin").read_bytes())
        except OSError:
            _SRC = {}
    blob = _SRC.get(path)
    if blob is not None:
        return zlib.decompress(blob).decode("utf-8").split("\n")
    try:
        return (SRC / path).read_text(encoding="utf-8").split("\n")
    except OSError:
        return None


def apply_blocks(old_lines, blocks):
    """Apply blocks (old-line coordinates) to old_lines, return new text."""
    out = []
    cur = 1
    for b in sorted(blocks, key=lambda b: b["pos"]):
        lo = b["pos"]
        hi = lo + len(b["rem"])
        out.extend(old_lines[cur - 1: lo - 1])
        out.extend(b["add"])
        cur = hi
    out.extend(old_lines[cur - 1:])
    return "\n".join(out)


COMMENTISH = re.compile(r"^\s*(#.*)?$")
CODING = re.compile(r"coding[:=]")


def sem_tokens(text):
    out = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.COMMENT:
                if CODING.search(t.string) and t.start[0] <= 2:
                    out.append((t.type, t.string))
                continue
            if t.type in (tokenize.NL, tokenize.ENCODING):
                continue
            out.append((t.type, t.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


# ------------------------------------------------------------------ selection primitives
def set_mask(sid):
    return SETS[sid]


def union_lines(path, lo, hi, table):
    tb = table.get(path)
    if not tb:
        return 0
    m = 0
    for ln in range(lo, hi + 1):
        sid = tb.get(ln)
        if sid is not None:
            m |= SETS[sid]
    return m


def stmt_range(S, L):
    sid = S["sid"]
    if L < len(sid) and sid[L]:
        return S["stmts"][sid[L]]
    return (L, L)


def func_containing(S, a, b):
    best = None
    for (s, e) in S["funcs"]:
        if s <= a and b <= e:
            if best is None or s >= best[0]:
                best = (s, e)
    return best


def class_containing(S, a, b):
    best = None
    for (s, e, _n) in S["classes"]:
        if s <= a and b <= e:
            if best is None or s >= best[0]:
                best = (s, e)
    return best


def class_chain(S, L):
    """Names of the classes enclosing line L, outermost first."""
    chain = [(s, n) for (s, e, n) in S["classes"] if s <= L <= e]
    chain.sort()
    return [n for _, n in chain]


def module_name(path):
    return path[:-3].replace("/", ".")


FAMILY_DIRS = {"expressions", "dialects", "parsers", "generators", "typing"}
FAMILY_FILES = {"sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py"}

ASSIGN = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)|^\s*(\w+)\s*(?::[^=]+)?=[^=]")


def added_names(texts):
    names = []
    unknown = False
    for t in texts:
        if COMMENTISH.match(t):
            continue
        m = ASSIGN.match(t)
        if m:
            names.append(m.group(1) or m.group(2))
        else:
            unknown = True
    return names, unknown


class PyChange:
    """Selection for one source/test python file."""

    def __init__(self, path, old_lines):
        self.path = path
        self.S = STRUCT[path]
        self.old = old_lines
        self.is_test = path.startswith("tests/")
        self.is_test_module = bool(re.match(r"tests/(.*/)?test_\w+\.py$", path))

    # --- helpers over this file
    def run_union(self, lo, hi):
        return union_lines(self.path, lo, hi, LINES)

    def col_union(self, lo, hi):
        return union_lines(self.path, lo, hi, GL)

    def file_union(self):
        n = len(self.S["kind"])
        return self.run_union(1, n) | self.col_union(1, n)

    def refs(self, names):
        m = 0
        for n in names:
            m |= REF.get(n, 0)
        return m

    def kind(self, L):
        k = self.S["kind"]
        return k[L] if 0 <= L < len(k) else 0

    # --- a single changed (existing) line
    def line_mask(self, L, deep=True):
        S = self.S
        k = self.kind(L)
        if k == 0:
            text = self.old[L - 1] if 0 < L <= len(self.old) else ""
            if COMMENTISH.match(text):
                return 0
            return self.structural(L)
        if k == 3:
            return OPENS.get(self.path, 0)
        lo, hi = stmt_range(S, L)
        run = self.run_union(lo, hi)
        col = self.col_union(lo, hi)
        if k == 2:
            return run | col
        # lines inside a multi-line lambda of a definition-time statement run only when called
        for (a, b) in S.get("lams", ()):
            if a <= L <= b:
                return self.run_union(a, b) | self.col_union(a, b)
        # definition-time statement
        dn = S["dnames"].get(lo)
        names = dn[1] if dn else []
        kd = dn[2] if dn else "other"
        if self.is_test:
            m = run | col
            if self.is_test_module:
                m |= tests_of_file(self.path)
            else:
                m |= self.file_union()
            return m
        lazy = col == 0
        if kd in ("def", "class") and deep:
            end = self.body_end(lo)
            m = self.run_union(lo, end) | self.col_union(lo, end)
        else:
            m = 0
        chain = class_chain(S, lo)
        if chain:
            key = module_name(self.path) + ":" + ".".join(chain)
            im = INST.get(key)
            if im is not None:
                r = self.refs(names) if names else 0
                m |= (im & r) if r else im
                return m
            if self.path.split("/")[1] in FAMILY_DIRS or self.path in FAMILY_FILES:
                # a class no test ever instantiated: only importers could notice
                m |= run
                return m
        if names:
            r = self.refs(names)
            if lazy:
                m |= (run & r) if r else run
            else:
                m |= run | r | col
        else:
            if lazy:
                m |= run
            else:
                return ALL
        return m

    def body_end(self, first):
        for (s, e) in self.S["funcs"]:
            if s == first:
                return e
        for (s, e, _n) in self.S["classes"]:
            if s == first:
                return e
        return first

    def structural(self, L):
        """else:/try:/finally:/decorator-ish lines outside statement ranges."""
        S = self.S
        f = func_containing(S, L, L)
        if f:
            return self.run_union(*f) | self.col_union(*f)
        c = class_containing(S, L, L)
        if c:
            return self.run_union(*c) | self.col_union(*c) | self.refs_class(c)
        return ALL

    def refs_class(self, c):
        return 0

    # --- insertion between old lines a and b = a + 1
    def insertion_mask(self, a, b, added):
        S = self.S
        sid = S["sid"]
        n = len(sid)
        if 1 <= a < n and b < n and sid[a] and sid[a] == sid[b]:
            return self.line_mask(a)
        f = func_containing(S, max(a, 1), b)
        if f and (self.kind(a) == 2 or self.kind(b) == 2):
            return self.run_union(*f) | self.col_union(*f)
        # definition level
        names, unknown = added_names(added)
        m = 0
        for ln in (a, b):
            if 1 <= ln < n and self.kind(ln) in (1, 2):
                lo, hi = stmt_range(S, ln)
                m |= self.run_union(lo, hi) | self.col_union(lo, hi)
        m |= self.refs(names)
        if self.is_test:
            if self.is_test_module:
                m |= tests_of_file(self.path)
            else:
                m |= self.file_union()
        else:
            # a new definition can shadow or be found dynamically (e.g. *_sql dispatch)
            c = class_containing(S, max(a, 1), b)
            if unknown:
                m |= self.file_union()
            if any(nm.endswith("_sql") or nm.startswith("_parse") or nm.isupper() for nm in names):
                m |= self.file_union() if c else 0
        return m


# ------------------------------------------------------------------ Fix A: function-signature edits
# A def line runs at import, so coverage credits it to every importer and a signature edit used to
# select everything. A changed default/parameter only matters (1) at import time, if the new header
# cannot be evaluated, and (2) inside calls to that function, which run its body. So: select the
# tests that ran the body, and verify at runtime (impcheck.py on a /tmp copy) that every sqlglot
# module still imports exactly as on the pinned tree; if not, select everything.
_HEADERS = {}
NEEDS_IMPORT_CHECK = set()  # paths whose selection relied on the header rule (per commit, reset in main)


def def_headers(path, text):
    """[(def_line, last_header_line, body_start, end, name)] for every function in text."""
    import ast

    key = (path, hash(text))
    if key not in _HEADERS:
        out = []
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body[0].lineno > n.lineno:
                out.append((n.lineno, n.body[0].lineno - 1, n.body[0].lineno, n.end_lineno, n.name))
        _HEADERS[key] = out
    return _HEADERS[key]


def defined_names(text):
    """Multiset of names bound at module level and in class bodies (recursively), with their scope.
    If this changes, some importer may reference a name that is gone (e.g. a deleted enum member
    used in another module's class body), which only an import of every module can rule out."""
    import ast
    from collections import Counter

    out = Counter()

    def walk(body, scope):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[(scope, n.name)] += 1
            elif isinstance(n, ast.ClassDef):
                out[(scope, n.name)] += 1
                walk(n.body, scope + "." + n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            out[(scope, x.id)] += 1
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    out[(scope, a.asname or a.name)] += 1
            elif isinstance(n, (ast.If, ast.Try, ast.With)) or type(n).__name__ == "TryStar":
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(n, field, []) or [], scope)
                for h in getattr(n, "handlers", []) or []:
                    walk(h.body, scope)

    walk(ast.parse(text).body, "")
    return out


def header_block_mask(ch, path, old_text, new_text, block):
    """Mask for a block that lies entirely inside one function's signature, else None."""
    lines = [ln for ln, _ in block["rem"]] or [block["pos"] - 1, block["pos"]]
    hs = [h for h in def_headers(path, old_text) if all(h[0] <= L <= h[1] for L in lines)]
    if not hs:
        return None
    # the edit must not add, remove or rename any function anywhere in the file
    if sorted(h[4] for h in def_headers(path, old_text)) != sorted(h[4] for h in def_headers(path + "#new", new_text)):
        return None
    h = min(hs, key=lambda h: h[1] - h[0])
    return ch.run_union(h[2], h[3]) | ch.col_union(h[2], h[3])


def select_py(fd, path, blocks):
    old = read_old(path)
    if old is None:
        return ALL
    ch = PyChange(path, old)
    # whole-file semantic equivalence
    new_text = apply_blocks(old, blocks)
    try:
        import ast

        ast.parse(new_text)
    except SyntaxError:
        if ch.is_test_module:
            return tests_of_file(path)
        return ALL
    old_tokens = None
    new_tokens = sem_tokens(new_text)
    old_tokens = sem_tokens("\n".join(old))
    if old_tokens is not None and new_tokens is not None and old_tokens == new_tokens:
        return 0
    # comment-only blocks
    cblocks = [
        b
        for b in blocks
        if all(COMMENTISH.match(t) for _, t in b["rem"]) and all(COMMENTISH.match(t) for t in b["add"])
    ]
    if cblocks:
        tc = sem_tokens(apply_blocks(old, cblocks))
        if tc is not None and tc == old_tokens:
            blocks = [b for b in blocks if b not in cblocks]
    m = 0
    old_text = "\n".join(old)
    if not ch.is_test and defined_names(old_text) != defined_names(new_text):
        NEEDS_IMPORT_CHECK.add(path)  # a module/class-level name appeared, vanished or was renamed
    for b in blocks:
        rem = b["rem"]
        add = b["add"]
        if not ch.is_test:
            hm = header_block_mask(ch, path, old_text, new_text, b)
            if hm is not None:
                NEEDS_IMPORT_CHECK.add(path)
                m |= hm
                continue
        for (ln, _) in rem:
            m |= ch.line_mask(ln)
        if not rem:
            a = b["pos"] - 1
            m |= ch.insertion_mask(a, a + 1, add)
        elif len(add) > len(rem):
            a = rem[-1][0]
            m |= ch.insertion_mask(a, a + 1, add[len(rem):])
        if m == ALL:
            return ALL
    return m


def dirname(p):
    return os.path.dirname(p)


def select_other(fd, path):
    m = OPENS.get(path, 0)
    if fd.is_new:
        m |= OPENS.get(dirname(path), 0)
    base = os.path.basename(path)
    if path.startswith("sqlglotc/"):
        m |= tests_of_file("tests/test_sqlglotc.py")
    if base in ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py", "requirements.lock"):
        m |= ALL if base in ("pytest.ini", "conftest.py", "tox.ini") else tests_of_file("tests/test_sqlglotc.py")
    return m


def select_file(fd):
    path = fd.old or fd.new
    if fd.binary:
        return OPENS.get(path, 0)
    if path.endswith(".py") and path in STRUCT and not fd.is_new:
        if fd.is_deleted:
            return ALL if not path.startswith("tests/") else tests_of_file(path)
        return select_py(fd, path, fd.hunks)
    if path.endswith(".py") and (fd.is_new or path not in STRUCT):
        if path.startswith("tests/"):
            return 0
        return 0
    return select_other(fd, path)


# ------------------------------------------------------------------ worker assignment
def assign(numbers):
    """Pick the worker count that minimises busiest load + 2 s per worker."""
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: -dur_ms[n])
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        loads = [0] * k
        groups = [[] for _ in range(k)]
        for n in items:
            j = loads.index(min(loads))
            groups[j].append(n)
            loads[j] += dur_ms[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, groups)
    return [g for g in best[1] if g]


def cost_ms(numbers):
    groups = assign(numbers)
    return max(sum(dur_ms[n] for n in g) for g in groups) + WORKER_MS * len(groups) if groups else 0


# ------------------------------------------------------------------ Fix B: TPC-DS pipeline check
# test_execute_tpcds (~21.5 s) runs every TPC-DS query through parse -> pickle -> optimize -> plan
# -> generated Python -> execute, and compares with duckdb on transpile(sql, "duckdb"). If a patch
# leaves everything before execution byte-identical for all queries, and touches no execution-time
# code (sqlglot/executor/) and nothing outside sqlglot/, the test sees identical input on identical
# executor code, so it cannot change outcome. fixb.py checks this on a /tmp copy of the patched
# sources; any error, difference or timeout keeps the test.
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_NUM = num_by_id.get(TPCDS_ID)
FIXB_PINNED_SRC = Path(os.environ.get("FIXB_PINNED_SRC", APP / "pinned"))  # pristine sqlglot/ tree
FIXB_PINNED_FP = Path(os.environ.get("FIXB_PINNED_FP", APP / "pinned_tpcds.json"))
FIXB_DATA = APP / "fixb_tpcds.json"
FIXB_MIN_SAVING_MS = 6000  # only check when dropping the test saves clearly more than the check costs
FIXB_TIMEOUT_S = 40
FIXB_BUDGET_S = 100  # stop starting new checks this far into the run (limit: 180 s practice, 270 s final)
T0 = None


def fixb_eligible(diffs):
    paths = set()
    for fd in diffs:
        for p in (fd.old, fd.new):
            if p:
                paths.add(p)
    return bool(paths) and all(p.startswith("sqlglot/") and not p.startswith("sqlglot/executor/") for p in paths)


def fixb_apply(root, diffs):
    for fd in diffs:
        if fd.binary:
            raise ValueError("binary")
        old_p = root / fd.old if fd.old else None
        new_p = root / fd.new if fd.new else None
        if fd.is_deleted:
            old_p.unlink()
            continue
        old_lines = [] if fd.is_new else old_p.read_text(encoding="utf-8").split("\n")
        text = apply_blocks(old_lines, fd.hunks)
        new_p.parent.mkdir(parents=True, exist_ok=True)
        new_p.write_text(text, encoding="utf-8")
        if fd.old and fd.new and fd.old != fd.new:
            old_p.unlink()


def fixb_same(cid, diffs):
    """True only if fixb.py proves the TPC-DS pipeline output is unchanged."""
    import shutil
    import subprocess
    import tempfile
    import time

    if time.time() - T0 > FIXB_BUDGET_S:
        log("fixb", cid, "skipped: time budget")
        return False
    td = Path(tempfile.mkdtemp(prefix="fixb_", dir="/tmp" if Path("/tmp").is_dir() else None))
    try:
        shutil.copytree(FIXB_PINNED_SRC / "sqlglot", td / "sqlglot")
        fixb_apply(td, diffs)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, "-S", str(APP / "fixb.py"), str(td), str(FIXB_DATA), "-", str(FIXB_PINNED_FP)],
                           env=env, capture_output=True, timeout=FIXB_TIMEOUT_S)
        log("fixb", cid, "SAME" if p.returncode == 0 else f"keep (exit {p.returncode})")
        return p.returncode == 0
    except Exception as e:  # anything unexpected: keep the test
        log("fixb", cid, "keep:", repr(e)[:200])
        return False
    finally:
        shutil.rmtree(td, ignore_errors=True)


FIXB_PINNED_IMPORTS = Path(os.environ.get("FIXB_PINNED_IMPORTS", APP / "pinned_imports.json"))


def imports_ok(cid, diffs):
    """True only if every sqlglot module imports on the patched tree exactly as on the pinned one."""
    import shutil
    import subprocess
    import tempfile

    if not (FIXB_PINNED_IMPORTS.exists() and (FIXB_PINNED_SRC / "sqlglot").is_dir()):
        return False
    td = Path(tempfile.mkdtemp(prefix="imp_", dir="/tmp" if Path("/tmp").is_dir() else None))
    try:
        shutil.copytree(FIXB_PINNED_SRC / "sqlglot", td / "sqlglot")
        # only sqlglot/ files can change whether sqlglot modules import
        fixb_apply(td, [fd for fd in diffs if all(p.startswith("sqlglot/") for p in (fd.old, fd.new) if p)])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, "-S", str(APP / "impcheck.py"), str(td), str(FIXB_PINNED_IMPORTS)],
                           env=env, capture_output=True, timeout=FIXB_TIMEOUT_S)
        log("impcheck", cid, "ok" if p.returncode == 0 else f"FAILED (exit {p.returncode}) -> all tests")
        return p.returncode == 0
    except Exception as e:
        log("impcheck", cid, "error -> all tests:", repr(e)[:200])
        return False
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main():
    global T0
    import time
    from concurrent.futures import ThreadPoolExecutor

    T0 = time.time()
    fixb_ready = TPCDS_NUM is not None and FIXB_PINNED_FP.exists() and (FIXB_PINNED_SRC / "sqlglot").is_dir()
    if not fixb_ready:
        log("fixb disabled: pinned data missing")
    rows = []  # (commit, numbers, diffs or None)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        diffs = None
        NEEDS_IMPORT_CHECK.clear()
        try:
            diffs = parse_patch(text)
            mask = 0
            for fd in diffs:
                mask |= select_file(fd)
                if mask == ALL:
                    break
            if mask != ALL and NEEDS_IMPORT_CHECK and not imports_ok(patch.stem, diffs):
                mask = ALL
        except Exception as e:  # never crash: fall back to everything
            log("fallback for", patch.stem, repr(e))
            mask = ALL
            diffs = None
        numbers = mask_to_numbers(mask)
        if mask == ALL:
            numbers = sorted(set(numbers) | set(EXTRA_NUMS))
        rows.append((patch.stem, numbers, diffs))

    checks = []
    if fixb_ready:
        for i, (cid, numbers, diffs) in enumerate(rows):
            if diffs is None or TPCDS_NUM not in numbers or not fixb_eligible(diffs):
                continue
            without = [n for n in numbers if n != TPCDS_NUM]
            if cost_ms(numbers) - cost_ms(without) > FIXB_MIN_SAVING_MS:
                checks.append(i)
    if checks:
        with ThreadPoolExecutor(2) as ex:
            same = list(ex.map(lambda i: fixb_same(rows[i][0], rows[i][2]), checks))
        for i, ok in zip(checks, same):
            if ok:
                cid, numbers, diffs = rows[i]
                rows[i] = (cid, [n for n in numbers if n != TPCDS_NUM], diffs)

    out = []
    for cid, numbers, _ in rows:
        numbers = [n for n in numbers if n != TPCDS_NUM]  # PROBE P3 (practice only, never the final)
        out.append(json.dumps({"commit": cid, "workers": assign(numbers)}))
        log(cid, len(numbers))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
