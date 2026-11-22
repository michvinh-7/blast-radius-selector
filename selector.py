"""Blast radius selector: choose the sqlglot tests that can catch each patch.

Reads /data/tests.json and /data/commits/*.patch, answers from a precomputed map
(map.bin: per-test line coverage, fixture reads, source structure) built from the
pinned sqlglot commit, and prints one JSON line per commit.
"""

import gc; gc.set_threshold(100000, 50, 100)  # short-lived process: collect cyclic garbage rarely (CPU is scored)
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
gc.freeze()  # long-lived map: keep it out of every later collection
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


def lazy_hook_ranges(text):
    """Line ranges of module-level __getattr__/__dir__ (PEP 562). These lazy-attribute hooks cache
    their results in globals(), so after the first access no later test executes them: per-test
    coverage cannot tell who depends on them. Any edit inside one selects everything."""
    import ast

    return [(n.lineno, n.end_lineno) for n in ast.parse(text).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ("__getattr__", "__dir__")]


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


_CMP_TOKEN = re.compile(r"==|!=|<=|>=|<|>|\bis\b|\bin\b")


def _may_touch_comparison(blocks):
    """Cheap pre-filter for valuemap: a comparison-only change must touch a line with a comparison."""
    return any(_CMP_TOKEN.search(t) for b in blocks for t in [x for _, x in b["rem"]] + b["add"])


def select_py(fd, path, blocks):
    old = read_old(path)
    if old is None:
        return ALL
    ch = PyChange(path, old)
    # whole-file semantic equivalence
    new_text = apply_blocks(old, blocks)
    all_blocks = blocks  # (comment-only blocks are filtered below; offsets need them all)
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
    if VM["enabled"] and not ch.is_test and path.startswith("sqlglot/") and _may_touch_comparison(blocks):
        import valuemap

        if valuemap.file_equivalent(VALUEMAP_PATH, path, "\n".join(old), new_text):
            VM["used"] = True
            log("valuemap", path, "comparison change proven result-preserving on all recorded inputs")
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
    if not ch.is_test:
        hooks = lazy_hook_ranges(old_text)
        for b in blocks:
            touched = [ln for ln, _ in b["rem"]] or [b["pos"] - 1, b["pos"]]
            if any(lo <= L <= hi for L in touched for lo, hi in hooks):
                return ALL
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
            nm = narrow_insertion(ch, path, old, new_text, all_blocks, b)
            if nm is not None:
                m |= nm
                continue
            a = b["pos"] - 1
            m |= ch.insertion_mask(a, a + 1, add)
        elif len(add) > len(rem):
            a = rem[-1][0]
            m |= ch.insertion_mask(a, a + 1, add[len(rem):])
        if m == ALL:
            return ALL
    return m


# ------------------------------------------------------------------ pure insertions
# A pure insertion (added lines, nothing removed) used to select every test that ran the enclosing
# function, or for a new definition every user of its neighbours / file. Two sharper, sound cases:
# (1) INSIDE A FUNCTION: inserted complete statements can only run when execution reaches the
#     insertion point, i.e. after the previous sibling statement or before the next one ran. So
#     select the tests that ran either sibling - provided the insertion cannot change the function
#     elsewhere: it binds no name that was not already local (a new local name would make earlier
#     reads of that name local -> UnboundLocalError), has no yield/await/global/nonlocal, and is not
#     at the start of the function body (docstring).
# (2) AT MODULE OR CLASS LEVEL: new plain functions (no decorators, constant defaults) and constant
#     assignments whose names are mentioned NOWHERE in the pinned sources (no code, string or test
#     mentions them; sqlglot's only name-built lookups are *_sql / _parse* and classes, which are
#     excluded) cannot be reached by any pinned code: they select nothing. The import check still
#     runs (bound names changed).
_NAMES = None
_NEWAST = {}


def _pinned_names():
    global _NAMES
    if _NAMES is None:
        try:
            _NAMES = frozenset((APP / "pinned_names.txt").read_text().split("\n"))
        except OSError:
            _NAMES = frozenset()
    return _NAMES


_PLAIN = None


def _plain_classes():
    global _PLAIN
    if _PLAIN is None:
        try:
            _PLAIN = frozenset((APP / "pinned_classes.txt").read_text().split("\n"))
        except OSError:
            _PLAIN = frozenset()
    return _PLAIN


def _source_readers(path):
    """Tests that read this file's source text (e.g. test_typing parses sqlglot/typing/*.py and
    compares code objects): an insertion changes what they read, whatever runs."""
    m = OPENS.get(path, 0)
    if path.startswith("sqlglot/typing/"):
        m |= tests_of_file("tests/test_typing.py")
    return m


def _new_to_old(old_n, blocks):
    """new line number -> old line number (None for added lines), 1-based, index 0 unused."""
    m = [None]
    cur = 1
    for b in sorted(blocks, key=lambda b: b["pos"]):
        lo = b["pos"]
        for L in range(cur, lo):
            m.append(L)
        m.extend([None] * len(b["add"]))
        cur = lo + len(b["rem"])
    for L in range(cur, old_n + 1):
        m.append(L)
    return m


def _stmt_lists(tree):
    import ast

    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
                yield node, field, lst
        for h in getattr(node, "handlers", None) or []:
            yield h, "body", h.body
        for c in getattr(node, "cases", None) or []:
            yield c, "body", c.body


def _overlap_walk(tree, n0, n1):
    """ast.walk order (breadth-first), restricted to nodes whose line span (decorators included)
    overlaps [n0, n1] or that carry no line info, plus a parent map for every enqueued node. A
    statement list outside the region cannot contain statements inside [n0, n1], so the lists this
    yields are exactly those of the full walk that can matter, in the same relative order."""
    from collections import deque

    def overlaps(n):
        lo = getattr(n, "lineno", None)
        hi = getattr(n, "end_lineno", None)
        if lo is None or hi is None:
            return True
        for d in getattr(n, "decorator_list", None) or []:
            lo = min(lo, d.lineno)
        return not (hi < n0 or lo > n1)

    import ast

    order, parents = [], {}
    todo = deque([tree])
    while todo:
        n = todo.popleft()
        order.append(n)
        for c in ast.iter_child_nodes(n):
            if overlaps(c):
                parents[id(c)] = n
                todo.append(c)
    return order, parents


def _lists_of(nodes):
    import ast

    for node in nodes:
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
                yield node, field, lst
        for h in getattr(node, "handlers", None) or []:
            yield h, "body", h.body
        for c in getattr(node, "cases", None) or []:
            yield c, "body", c.body


def _start(st):
    return min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])


def _bound_names(nodes):
    """Names bound in the scope these statements belong to (nested def/class/lambda bodies excluded,
    their own names included; walrus inside comprehensions binds in the enclosing scope)."""
    import ast

    out = set()

    def rec(n, top):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            for d in n.decorator_list:
                rec(d, False)
            if not isinstance(n, ast.ClassDef):
                for d in n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
                    rec(d, False)
            return
        if isinstance(n, ast.Lambda):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.NamedExpr):
            out.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif type(n).__name__ in ("MatchAs", "MatchStar") and getattr(n, "name", None):
            out.add(n.name)
        elif type(n).__name__ == "MatchMapping" and getattr(n, "rest", None):
            out.add(n.rest)
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # comprehension targets are local to the comprehension; only walrus escapes
            for x in ast.walk(n):
                if isinstance(x, ast.NamedExpr):
                    out.add(x.target.id)
            return
        for c in ast.iter_child_nodes(n):
            rec(c, False)

    for n in nodes:
        rec(n, True)
    return out


def narrow_insertion(ch, path, old, new_text, blocks, b):
    """Mask for a pure-insertion block under rules (1)/(2) above, or None for the general rule."""
    import ast
    import builtins

    if ch.is_test or not path.startswith("sqlglot/") or b["rem"] or not b["add"]:
        return None
    key = (path, new_text)
    if key not in _NEWAST:
        _NEWAST.clear()
        try:
            _NEWAST[key] = ast.parse(new_text)
        except SyntaxError:
            _NEWAST[key] = None
    tree = _NEWAST[key]
    if tree is None:
        return None
    off = sum(len(x["add"]) - len(x["rem"]) for x in blocks if x["pos"] < b["pos"])
    n0 = b["pos"] + off
    n1 = n0 + len(b["add"]) - 1
    code = [n0 + i for i, t in enumerate(b["add"]) if not COMMENTISH.match(t)]
    if not code:
        return None
    # the inserted code lines must be exactly a run of whole statements of one statement list
    found = None
    order, parents = _overlap_walk(tree, n0, n1)
    for owner, field, lst in _lists_of(order):
        idx = [i for i, st in enumerate(lst) if n0 <= _start(st) and st.end_lineno <= n1]
        if not idx:
            continue
        if idx != list(range(idx[0], idx[-1] + 1)):
            return None
        covered = {L for i in idx for L in range(_start(lst[i]), lst[i].end_lineno + 1)}
        if not all(L in covered for L in code):
            continue
        if any(i not in idx and not (lst[i].end_lineno < n0 or _start(lst[i]) > n1) for i in range(len(lst))):
            return None
        found = (owner, field, lst, idx[0], idx[-1])
        break
    if found is None:
        return None
    owner, field, lst, i, j = found
    ins = lst[i:j + 1]
    # nearest enclosing scope of the statement list (its ancestors all overlap the insertion)
    scope = owner
    while not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module, ast.Lambda)):
        scope = parents.get(id(scope))
        if scope is None:
            return None
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # rule (1)
        for st in ins:
            for x in ast.walk(st):
                if isinstance(x, (ast.Yield, ast.YieldFrom, ast.Await, ast.Global, ast.Nonlocal)):
                    return None
        if owner is scope and field == "body" and i == 0:
            return None  # function docstring position
        args = scope.args
        local = {x.arg for x in args.posonlyargs + args.args + args.kwonlyargs}
        local |= {x.arg for x in (args.vararg, args.kwarg) if x is not None}
        saved = lst[i:j + 1]
        del lst[i:j + 1]
        try:
            local |= _bound_names(scope.body)  # names local to the function without the insertion
            # every identifier the rest of the function mentions (nested scopes included)
            mentioned = {x.id for x in ast.walk(scope) if isinstance(x, ast.Name)} | \
                {nm for x in ast.walk(scope) if isinstance(x, (ast.Global, ast.Nonlocal)) for nm in x.names} | \
                {x.arg for x in ast.walk(scope) if isinstance(x, ast.arg)}
        finally:
            lst[i:i] = saved
        # a name the insertion binds must already be local, or appear nowhere else in the function
        # (otherwise making it local could change an earlier read or a closure's read of it)
        for nm in _bound_names(ins) - local:
            if nm in mentioned:
                return None
        n2o = _new_to_old(len(old), blocks)
        m = 0
        sib = []
        if i > 0:
            sib.append(lst[i - 1])
        if j + 1 < len(lst):
            sib.append(lst[j + 1])
        if not sib:
            return None
        for st in sib:
            lo, hi = _start(st), st.end_lineno
            if hi >= len(n2o) or any(n2o[L] is None for L in range(lo, hi + 1)):
                return None  # the sibling was edited too: use the general rule
            m |= ch.run_union(n2o[lo], n2o[hi]) | ch.col_union(n2o[lo], n2o[hi])
        log("insertion", path, f"inside a function: tests that reach new lines {n0}-{n1}")
        return m | _source_readers(path)
    if isinstance(scope, (ast.Module, ast.ClassDef)) and owner is scope and field == "body":
        # rule (2)
        if isinstance(scope, ast.ClassDef):
            # only plain classes (no Enum / dataclass / NamedTuple / TypedDict / Protocol / unknown
            # metaclass: there a new attribute is a new member or field), and no class decorators
            q, n = [], scope
            while n is not None and not isinstance(n, ast.Module):
                if isinstance(n, ast.ClassDef):
                    if n.decorator_list:
                        return None
                    q.append(n.name)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return None  # class created inside a function: not in the build-time list
                n = parents.get(id(n))
            modname = path[:-3].replace("/", ".").removesuffix(".__init__")
            if f"{modname}:{'.'.join(reversed(q))}" not in _plain_classes():
                return None
        future = any(isinstance(st, ast.ImportFrom) and st.module == "__future__" and any(a.name == "annotations" for a in st.names)
                     for st in tree.body)
        names = set()
        for st in ins:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if st.decorator_list:
                    return None
                a = st.args
                if not all(isinstance(d, ast.Constant) for d in a.defaults + [x for x in a.kw_defaults if x is not None]):
                    return None
                if not future and (st.returns is not None or any(x.annotation is not None for x in
                                   a.posonlyargs + a.args + a.kwonlyargs + [y for y in (a.vararg, a.kwarg) if y])):
                    return None
                names.add(st.name)
            elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) \
                    and isinstance(st.value, ast.Constant):
                names.add(st.targets[0].id)
            elif isinstance(st, ast.AnnAssign) and future and isinstance(st.target, ast.Name) and st.simple \
                    and (st.value is None or isinstance(st.value, ast.Constant)):
                names.add(st.target.id)
            else:
                return None
        pinned = _pinned_names()
        if not pinned:
            return None
        for nm in names:
            if nm in pinned or hasattr(builtins, nm) or nm.startswith("__") or nm.endswith("_sql") or nm.startswith("_parse"):
                return None
        NEEDS_IMPORT_CHECK.add(path)
        log("insertion", path, "new definitions no pinned code or test mentions:", sorted(names))
        return _source_readers(path)
    return None


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
        # pytest reads setup.cfg / pyproject.toml / tox.ini / pytest.ini / conftest.py at startup: an edit
        # can make the whole run error (e.g. a duplicate key in setup.cfg fails every test)
        startup = ("pytest.ini", "conftest.py", "tox.ini", "setup.cfg", "pyproject.toml")
        m |= ALL if base in startup else tests_of_file("tests/test_sqlglotc.py")
    return m


def select_file(fd):
    path = fd.old or fd.new
    if fd.old and fd.new and fd.old != fd.new:
        # a rename deletes the old path: importers/openers of the old name break
        if fd.old.endswith(".py"):
            if fd.old.startswith("tests/"):
                return tests_of_file(fd.old)
            return ALL
        m = OPENS.get(fd.old, 0) | OPENS.get(dirname(fd.new), 0)
        return m | select_other(fd, fd.new)
    if fd.binary:
        return OPENS.get(path, 0)
    if path.endswith(".py") and path in STRUCT and not fd.is_new:
        if fd.is_deleted:
            return ALL if not path.startswith("tests/") else tests_of_file(path)
        return select_py(fd, path, fd.hunks)
    if path.endswith(".py") and (fd.is_new or path not in STRUCT):
        # files Python or pytest pick up implicitly: conftest plugins, site customization, package
        # markers in the test tree, and repo-root modules (the root is on sys.path and can shadow imports)
        base = os.path.basename(path)
        if (base in ("conftest.py", "sitecustomize.py", "usercustomize.py")
                or (base == "__init__.py" and path.startswith("tests/")) or "/" not in path):
            return ALL
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
# Value-profile check (valuemap.py): a patch whose only changes are comparisons that return the same
# result on every input the pinned suite fed them cannot change any test. Valid only if EVERY changed
# file is proven this way (otherwise other changes could feed the comparisons new values).
VALUEMAP_PATH = APP / "cmpmap.bin"
VM = {"enabled": True, "used": False}

TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_NUM = num_by_id.get(TPCDS_ID)
FIXB_PINNED_SRC = Path(os.environ.get("FIXB_PINNED_SRC", APP / "pinned"))  # pristine sqlglot/ tree
FIXB_PINNED_FP = Path(os.environ.get("FIXB_PINNED_FP", APP / "pinned_tpcds.json"))
FIXB_DATA = APP / "fixb_tpcds.json"
FIXB_MIN_SAVING_MS = 10000  # a full check costs ~6.5 s CPU on the scorer (board: +7 s CPU at 6 s threshold)
FIXB_TIMEOUT_S = 40
FIXB_BUDGET_S = 100  # stop starting new checks this far into the run (limit: 180 s practice, 270 s final)
T0 = None


# Files test_execute_tpcds can depend on outside sqlglot/: its own module, the helpers it imports, its
# fixtures, and anything that configures pytest or the package. Any other non-sqlglot file is safe to
# ignore for this test if the coverage map shows the test never opened it.
TPCDS_DEPS = ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py")
TPCDS_DIRS = ("tests/fixtures/optimizer/tpc-ds/",)
CONFIG_NAMES = ("conftest.py", "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "requirements.lock")
_TPCDS_BIT = None


# The two fixture files TestExecutor.setUpClass parses. test_execute_tpcds parses every TPC-DS
# (meta, sql) input and executes the flagged ones; TPC-H only has to parse. Parser replicated from
# tests/helpers.py at eb11aec.
TPCDS_SQL, TPCH_SQL = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
_FIXTEXT = None


def _helpers_pairs(text):
    """tests/helpers.py load_sql_fixture_pairs at eb11aec, on a string."""
    statements = "\n".join([line for line in text.splitlines() if line and not line.startswith("--")]).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql = statements[i].strip()
            meta, lines, j = {}, sql.split("\n"), 0
            while lines[j].startswith("#"):
                key, val = lines[j].split(":", maxsplit=1)
                meta[key.lstrip("#").strip()] = val.strip()
                j += 1
            out.append((meta, "\n".join(lines[j:]), statements[i + 1].strip()))
    return out


def fixture_inputs_unchanged(fd):
    """True if editing this setUpClass fixture leaves test_execute_tpcds's inputs byte-identical."""
    global _FIXTEXT
    if fd.is_new or fd.is_deleted or fd.binary or fd.old != fd.new or fd.old not in (TPCDS_SQL, TPCH_SQL):
        return False
    if _FIXTEXT is None:
        import gzip

        _FIXTEXT = json.loads(gzip.decompress((APP / "fixb_fixtures.json.gz").read_bytes()))
    old = _FIXTEXT[fd.old[len("tests/fixtures/optimizer/"):]]
    new = apply_blocks(old.split("\n"), fd.hunks)
    try:
        old_pairs, new_pairs = _helpers_pairs(old), _helpers_pairs(new)
    except Exception:  # setUpClass would raise: the test errors
        return False
    if fd.old == TPCH_SQL:
        return True  # parses fine; test_execute_tpcds never reads its content
    # test_execute_tpcds parses EVERY query (parse_one before submitting), executed or not, so every
    # (meta, sql) input must be byte-identical; only the expected-output halves may differ
    inputs = lambda pairs: [(m, q) for m, q, _ in pairs]  # noqa: E731
    return inputs(old_pairs) == inputs(new_pairs)


def tpcds_independent(fd, path):
    """True if this file change (outside sqlglot/) cannot influence test_execute_tpcds."""
    global _TPCDS_BIT
    if _TPCDS_BIT is None:
        _TPCDS_BIT = 1 << IDS.index(TPCDS_ID) if TPCDS_ID in IDS else 0
    if not _TPCDS_BIT or path in TPCDS_DEPS:
        return False
    if path in (TPCDS_SQL, TPCH_SQL):
        return fixture_inputs_unchanged(fd)
    if path.startswith(TPCDS_DIRS) or os.path.basename(path) in CONFIG_NAMES:
        return False
    if path.endswith(".py"):
        # any Python file is imported into the same pytest process (collection, shared module state):
        # an edit to it cannot be proven irrelevant to test_execute_tpcds from file-open data alone
        return False
    if OPENS.get(path, 0) & _TPCDS_BIT:
        return False
    # a directory listing only matters to files that did not exist before (collection scans dirs)
    if fd.is_new and OPENS.get(dirname(path), 0) & _TPCDS_BIT:
        return False
    return path.startswith(("tests/", "docs/")) or path.endswith((".md", ".rst", ".txt"))


# sqlglot lines outside sqlglot/executor/ that run while the TPC-DS queries EXECUTE but never in the
# fingerprinted pipeline (traced on the pinned commit): an edit there is invisible to the fingerprint.
_EXEC_ONLY = None


def touches_exec_only(diffs):
    global _EXEC_ONLY
    if _EXEC_ONLY is None:
        try:
            _EXEC_ONLY = {k: set(v) for k, v in json.loads((APP / "fixb_exec_only.json").read_text()).items()}
        except OSError:
            _EXEC_ONLY = None
            return True  # cannot tell: be conservative
    for fd in diffs:
        lines = _EXEC_ONLY.get(fd.old or "")
        if not lines:
            continue
        for b in fd.hunks:
            touched = [ln for ln, _ in b["rem"]] or [b["pos"] - 1, b["pos"]]
            if any(L in lines or L - 1 in lines or L + 1 in lines for L in touched):
                return True
    return False


def fixb_eligible(diffs):
    if touches_exec_only(diffs):
        return False
    paths = set()
    for fd in diffs:
        if fd.binary:
            return False
        for p in (fd.old, fd.new):
            if p:
                paths.add(p)
    code = [p for p in paths if p.startswith("sqlglot/")]
    if any(p.startswith("sqlglot/executor/") for p in code):
        return False
    others = [fd for fd in diffs if not all(p.startswith("sqlglot/") for p in (fd.old, fd.new) if p)]
    if not code and not others:
        return False
    return all(tpcds_independent(fd, p) for fd in others for p in (fd.old, fd.new) if p)


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


_FIXB_LINES = None


def _unreached_by_tpcds(diffs):
    """True only if every changed sqlglot line sits inside a function body (not import-time code) and
    is never executed by the test's pipeline or query execution on the pinned commit (traced under the
    scorer's Python). Then the patched code paths it runs are the pinned ones: identical fingerprint."""
    global _FIXB_LINES
    if _FIXB_LINES is None:
        try:
            d = json.loads((APP / "fixb_lines.json").read_text())
            _FIXB_LINES = {f: set(d["pipe"].get(f, [])) | set(d["exec"].get(f, [])) for f in set(d["pipe"]) | set(d["exec"])}
        except (OSError, ValueError, KeyError):
            _FIXB_LINES = {}
            return False
    if not _FIXB_LINES:
        return False
    for fd in diffs:
        p = fd.old or fd.new
        if not p or not p.startswith("sqlglot/"):
            continue
        if fd.is_new or fd.is_deleted or fd.binary or fd.old != fd.new:
            return False
        old = read_old(p)
        if old is None:
            return False
        bodies = [(h[2], h[3]) for h in def_headers(p, "\n".join(old))]
        run = _FIXB_LINES.get(p, set())
        for b in fd.hunks:
            touched = [ln for ln, _ in b["rem"]] or [b["pos"] - 1, b["pos"]]
            for L in touched:
                if not any(lo <= L <= hi for lo, hi in bodies):
                    return False  # import-time / class-level code
                if L in run or L - 1 in run or L + 1 in run:
                    return False
    return True


def fixb_same(cid, diffs):
    """True only if fixb.py proves the TPC-DS pipeline output is unchanged."""
    import shutil
    import subprocess
    import tempfile
    import time

    if not any(p.startswith("sqlglot/") for fd in diffs for p in (fd.old, fd.new) if p):
        log("fixb", cid, "SAME (no sqlglot code changed; other files vetted)")
        return True
    if _unreached_by_tpcds(diffs):
        log("fixb", cid, "SAME (changed lines never run by the TPC-DS pipeline or execution)")
        return True
    if time.time() - T0 > FIXB_BUDGET_S:
        log("fixb", cid, "skipped: time budget")
        return False
    td = Path(tempfile.mkdtemp(prefix="fixb_", dir="/tmp" if Path("/tmp").is_dir() else None))
    try:
        shutil.copytree(FIXB_PINNED_SRC / "sqlglot", td / "sqlglot")
        # only the sqlglot/ part can change the pipeline; the rest was vetted by tpcds_independent()
        fixb_apply(td, [fd for fd in diffs if all(p.startswith("sqlglot/") for p in (fd.old, fd.new) if p)])
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
        log("!!! impcheck disabled: pinned data missing -> all tests !!!")
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


# test_generator_memory builds every dialect's generator 1,100 times and asserts the traced memory
# growth over the last 1,000 is < 1 KiB. Generator.__init__ reads only Dialect/Tokenizer-derived
# attributes; a Generator class's own constants (TRANSFORMS, TYPE_MAPPING, flags, ...) are read only by
# _build_dispatch -- which copies TRANSFORMS and never calls its values, runs once per class during
# the warm-up, and is cached -- or by generate(), which this test never calls. So a patch whose every
# change is a constant assignment inside a Generator class body, on lines the test never runs, cannot
# change the measured loop. Evaluating the changed constants happens at import, so a cold process
# imports everything and builds one generator per dialect on the patched tree; any difference keeps it.
GENMEM_ID = "tests/test_generator.py::TestGenerator::test_generator_memory"
GENMEM_NUM = num_by_id.get(GENMEM_ID)
FIXB_PINNED_IMPORTS_GEN = Path(os.environ.get("FIXB_PINNED_IMPORTS_GEN", APP / "pinned_imports_gen.json"))


# test_generator_memory's measured window (between tracemalloc.start and stop) executes only the lines in
# genmem_window.json (traced on the pinned commit under the scorer's Python): Generator.__init__, the
# dispatch-cache lookup, name_sequence, Dialect.get_or_raise, and the Athena/BigQuery generator __init__s.
# Everything else this test touches runs once, at import/warm-up, before memory is traced: a change there
# can only make the test fail by raising, which the cold check (fresh process: import everything, build one
# generator per dialect) detects. Athena's generator builds Hive and Trino dialects inside the window, so
# those modules (and the tokenizer machinery, lazy-import packages) always keep the test.
GENMEM_PATH_FILES = {
    "sqlglot/tokens.py", "sqlglot/tokenizer_core.py", "sqlglot/dialects/__init__.py", "sqlglot/__init__.py",
    "sqlglot/generators/__init__.py", "sqlglot/generators/athena.py", "sqlglot/dialects/athena.py",
    "sqlglot/dialects/hive.py", "sqlglot/dialects/trino.py", "sqlglot/generators/hive.py",
    "sqlglot/generators/trino.py", "sqlglot/generators/presto.py", "sqlglot/dialects/presto.py",
}
GENMEM_DENY = {"NORMALIZE_FUNCTIONS", "QUOTE_START", "QUOTE_END", "IDENTIFIER_START", "IDENTIFIER_END",
               "BYTE_START", "BYTE_END", "IDENTIFIER_ESCAPED_SEQUENCES", "STRING_ESCAPES", "QUOTES", "IDENTIFIERS",
               "BYTE_STRINGS", "tokenizer_class", "generator_class", "parser_class", "Tokenizer", "Generator", "Parser",
               "_DISPATCH_CACHE", "_build_dispatch", "name_sequence", "get_or_raise", "__init__", "__new__"}
_GENMEM_WINDOW = None


def genmem_droppable(diffs):
    """True if no change can alter test_generator_memory's measured window (see above)."""
    import ast

    global _GENMEM_WINDOW
    if _GENMEM_WINDOW is None:
        try:
            _GENMEM_WINDOW = {k: set(v) for k, v in json.loads((APP / "genmem_window.json").read_text()).items()}
        except (OSError, ValueError):
            _GENMEM_WINDOW = {}
            return False
    if not _GENMEM_WINDOW:
        return False
    any_change = False
    for fd in diffs:
        p = fd.old or fd.new
        if not p or not p.startswith("sqlglot/") or not p.endswith(".py") or p in GENMEM_PATH_FILES:
            return False
        if fd.is_new or fd.is_deleted or fd.binary or fd.old != fd.new:
            return False
        old = read_old(p)
        if old is None:
            return False
        win = _GENMEM_WINDOW.get(p, set())
        tree = ast.parse("\n".join(old))
        hot_funcs, tokenizers = [], []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                lo, hi = node.lineno, node.end_lineno
                if any(lo <= L <= hi for L in win):
                    hot_funcs.append((lo, hi))  # a function that runs inside the window: any edit counts
            elif isinstance(node, ast.ClassDef) and node.name == "Tokenizer":
                tokenizers.append((node.lineno, node.end_lineno))
        for b in fd.hunks:
            any_change = True
            touched = [ln for ln, _ in b["rem"]] or [b["pos"] - 1, b["pos"]]
            for L in touched:
                if L in win or L - 1 in win or L + 1 in win:
                    return False
                if any(lo <= L <= hi for lo, hi in hot_funcs + tokenizers):
                    return False
            if any(GENMEM_DENY_RE.search(t) for t in [x for _, x in b["rem"]] + b["add"]):
                return False
    return any_change


GENMEM_DENY_RE = re.compile(r"\b(" + "|".join(sorted(GENMEM_DENY)) + r")\b")


def genmem_cold_ok(cid, diffs):
    import shutil
    import subprocess
    import tempfile

    if not (FIXB_PINNED_IMPORTS_GEN.exists() and (FIXB_PINNED_SRC / "sqlglot").is_dir()):
        log("!!! genmem check disabled: pinned data missing !!!")
        return False
    td = Path(tempfile.mkdtemp(prefix="gm_", dir="/tmp" if Path("/tmp").is_dir() else None))
    try:
        shutil.copytree(FIXB_PINNED_SRC / "sqlglot", td / "sqlglot")
        fixb_apply(td, diffs)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, "-S", str(APP / "impcheck.py"), str(td), "--generators",
                            str(FIXB_PINNED_IMPORTS_GEN)], env=env, capture_output=True, timeout=FIXB_TIMEOUT_S)
        log("genmem", cid, "cold check ok -> drop" if p.returncode == 0 else f"cold check differs (exit {p.returncode}) -> keep")
        return p.returncode == 0
    except Exception as e:
        log("genmem", cid, "keep:", repr(e)[:200])
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
        # never crash in the scorer (a crash rejects the run), but make it impossible to miss in gates
        log("!!! fixb disabled: pinned data missing at", FIXB_PINNED_FP, "/", FIXB_PINNED_SRC, "!!!")
    rows = []  # (commit, numbers, diffs or None)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        diffs = None
        try:
            diffs = parse_patch(text)
            for attempt in (1, 2):
                VM["enabled"], VM["used"] = attempt == 1, False
                NEEDS_IMPORT_CHECK.clear()
                mask = 0
                for fd in diffs:
                    mask |= select_file(fd)
                    if mask == ALL:
                        break
                only_proven_py = all(
                    (fd.old or "").startswith("sqlglot/") and fd.old == fd.new and fd.old.endswith(".py")
                    and not fd.is_new and not fd.is_deleted and not fd.binary for fd in diffs)
                if not VM["used"] or (mask == 0 and only_proven_py and not NEEDS_IMPORT_CHECK):
                    break
                log("valuemap", patch.stem, "not applicable to the whole patch; recomputing without it")
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

    if GENMEM_NUM is not None:
        gchecks = [i for i, (cid, numbers, diffs) in enumerate(rows)
                   if diffs is not None and GENMEM_NUM in numbers and len(numbers) < len(dur_ms) - 10
                   and genmem_droppable(diffs)
                   and cost_ms(numbers) - cost_ms([n for n in numbers if n != GENMEM_NUM]) > 700]  # check ~0.45 s CPU with .pyc
        if gchecks:
            with ThreadPoolExecutor(2) as ex:
                oks = list(ex.map(lambda i: genmem_cold_ok(rows[i][0], rows[i][2]), gchecks))
            for i, ok in zip(gchecks, oks):
                if ok:
                    cid, numbers, diffs = rows[i]
                    rows[i] = (cid, [n for n in numbers if n != GENMEM_NUM], diffs)

    out = []
    for cid, numbers, _ in rows:
        out.append(json.dumps({"commit": cid, "workers": assign(numbers)}))
        log(cid, len(numbers))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # skip interpreter teardown (CPU is scored)
