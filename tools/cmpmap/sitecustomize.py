"""Build-time instrumentation (never shipped): record what every single-operator comparison in
sqlglot/ sees while the pinned test suite runs. Loaded via PYTHONPATH so xdist workers, spawn
pool workers and `python -c` subprocesses all record too. Each process dumps on exit to
$CMPMAP_DIR/<pid>.pkl.

Per site "relpath:lineno:col" we keep:
  n     evaluations seen
  P     set of typed primitive operand pairs ((tag, value), (tag, value))   (None/bool/int/float/str/tuples)
  E     set of result vectors over OPS for pairs involving Enum members      (dunders not from sqlglot)
  O     True if some evaluation had two plain objects (object.__eq__/__ne__): is <-> == equivalent
  X     True if some evaluation could not be classified (custom __eq__, non-bool result, ...)
  over  True if P or E exceeded CAP distinct entries
"""

import ast
import atexit
import enum
import importlib.abc
import importlib.machinery
import math
import operator
import os
import pickle
import sys

ROOT = os.environ.get("CMPMAP_ROOT", "")  # absolute path of the sqlglot checkout
OUT = os.environ.get("CMPMAP_DIR", "/tmp/cmpmap")
CAP = 64
OPS = ("is", "is not", "==", "!=", "<", "<=", ">", ">=")
OPFN = {
    "is": operator.is_, "is not": operator.is_not, "==": operator.eq, "!=": operator.ne,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "in": lambda a, b: a in b, "not in": lambda a, b: a not in b,
}
AST_OP = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
          ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in"}
PRIM = (type(None), bool, int, float, str)
DATA = {}


def prim(v, depth=0):
    """Typed, hashable encoding of a primitive value, or None if not primitive."""
    t = type(v)
    if t in PRIM:
        if t is float and math.isnan(v):
            return ("float", "nan")
        return (t.__name__, v)
    if t is tuple and depth < 3 and len(v) <= 8:
        parts = tuple(prim(x, depth + 1) for x in v)
        if all(p is not None for p in parts):
            return ("tuple", parts)
    return None


def plain_obj(v):
    """Equality falls back to identity: object's default dunders, or None (its C-level __eq__ is identity)."""
    t = type(v)
    if t is type(None):
        return True
    return getattr(t, "__eq__", None) is object.__eq__ and getattr(t, "__ne__", None) is object.__ne__


def safe_dunders(v):
    """Enum member (or primitive) whose comparison dunders come from builtins/enum, not sqlglot."""
    for name in ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__"):
        for k in type(v).__mro__:
            if name in k.__dict__:
                if (k.__module__ or "").startswith("sqlglot"):
                    return False
                break
    return True


def vector(a, b):
    out = []
    for op in OPS:
        try:
            r = OPFN[op](a, b)
            out.append(r if type(r) is bool else "nonbool")
        except Exception as e:  # noqa: BLE001
            out.append("exc:" + type(e).__name__)
    return tuple(out)


def site(key):
    d = DATA.get(key)
    if d is None:
        d = DATA[key] = {"n": 0, "P": set(), "E": set(), "O": False, "X": False, "over": False}
    return d


def __brcmp__(key, op, a, b):
    try:
        r = OPFN[op](a, b)
    except BaseException:
        # the original comparison raised: record as unclassifiable and re-raise unchanged
        site(key)["X"] = True
        raise
    try:
        d = site(key)
        d["n"] += 1
        if d["X"] or d["over"]:
            return r
        pa, pb = prim(a), prim(b)
        if pa is not None and pb is not None:
            d["P"].add((pa, pb))
            if len(d["P"]) > CAP:
                d["over"] = True
        elif op in OPS and (isinstance(a, enum.Enum) or isinstance(b, enum.Enum)) and (pa is not None or isinstance(a, enum.Enum)) \
                and (pb is not None or isinstance(b, enum.Enum)) and safe_dunders(a) and safe_dunders(b):
            d["E"].add(vector(a, b))
            if len(d["E"]) > CAP:
                d["over"] = True
        elif op in ("is", "is not", "==", "!=") and plain_obj(a) and plain_obj(b):
            d["O"] = True
        else:
            d["X"] = True
    except Exception:  # noqa: BLE001
        DATA.setdefault(key, {})["X"] = True
    return r


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in AST_OP:
            return node
        key = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(func=ast.Name("__brcmp__", ast.Load()),
                        args=[ast.Constant(key), ast.Constant(AST_OP[type(node.ops[0])]), node.left, node.comparators[0]],
                        keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(path, ROOT)
        tree = ast.parse(data, filename=path)
        tree = ast.fix_missing_locations(_Rewriter(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def exec_module(self, module):
        module.__dict__["__brcmp__"] = __brcmp__
        super().exec_module(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not os.path.abspath(spec.origin).startswith(os.path.join(ROOT, "sqlglot") + os.sep):
            return spec
        spec.loader = _Loader(name, spec.origin)
        return spec


def _dump():
    if not DATA:
        return
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{os.getpid()}.pkl"), "wb") as f:
        pickle.dump(DATA, f)


if ROOT:
    sys.meta_path.insert(0, _Finder())
    sys.dont_write_bytecode = True  # never cache instrumented bytecode
    atexit.register(_dump)
