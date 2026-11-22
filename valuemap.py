"""Value-profile equivalence check for single-operator comparisons.

Build time (tools/cmpmap, pinned commit eb11aec): the whole test suite runs once per hash seed with
every single-operator comparison in sqlglot/ instrumented. For each comparison site we record what
it was evaluated on: typed primitive operand pairs (P), result vectors over all comparison operators
for Enum operands (E), whether plain-identity objects were compared (O), and whether anything
unclassifiable was seen (X) or the value set overflowed. That profile is cmpmap.bin.

Runtime: if a file's only AST differences are inside comparison nodes (operator changed and/or a
literal operand changed), each changed site is re-evaluated on every recorded input. If the patched
comparison returns exactly the same result as the original on all of them, then every test run is
identical to the pinned run: execution agrees up to the first evaluation of a changed site, that
evaluation sees recorded operands and returns the same result, and so on by induction. Any unknown,
unrecorded, unsafe or differing site means "cannot prove" and the normal selection is used.
"""

import ast
import operator
import pickle
import zlib

OPFN = {
    "is": operator.is_, "is not": operator.is_not, "==": operator.eq, "!=": operator.ne,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
    "in": lambda a, b: a in b, "not in": lambda a, b: a not in b,
}
OPS = ("is", "is not", "==", "!=", "<", "<=", ">", ">=")  # order of the E result vectors
AST_OP = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
          ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in"}
PRIM = (type(None), bool, int, float, str)
IDENTITY = {"is", "is not"}

_MAP = None


def load(path):
    global _MAP
    if _MAP is None:
        try:
            _MAP = pickle.loads(zlib.decompress(path.read_bytes()))
        except OSError:
            _MAP = {}
    return _MAP


class NotProvable(Exception):
    pass


def _typed(v):
    return (type(v), repr(v))


def changed_compares(old_tree, new_tree):
    """[(old_compare_node, new_op, new_left_literal|None, new_right_literal|None)] if every AST
    difference lies in a comparison's operator or literal operand; raises NotProvable otherwise."""
    out = []

    def same(a, b):
        if isinstance(a, ast.AST):
            if type(a) is not type(b):
                raise NotProvable("node type")
            if isinstance(a, ast.Compare):
                return compare(a, b)
            for f in a._fields:
                same(getattr(a, f, None), getattr(b, f, None))
            return None
        if isinstance(a, list):
            if not isinstance(b, list) or len(a) != len(b):
                raise NotProvable("list length")
            for x, y in zip(a, b):
                same(x, y)
            return None
        if _typed(a) != _typed(b):
            raise NotProvable("value")
        return None

    def literal(a, b):
        """None if identical; the new literal value if a literal operand changed; else recurse."""
        if isinstance(a, ast.Constant) and isinstance(b, ast.Constant):
            if _typed(a.value) == _typed(b.value) and a.kind == b.kind:
                return None
            if type(b.value) not in PRIM or type(a.value) not in PRIM:
                raise NotProvable("literal type")
            return ("lit", b.value)
        same(a, b)
        return None

    def compare(a, b):
        if len(a.ops) != 1 or len(b.ops) != 1:
            if ast.dump(a) != ast.dump(b):
                raise NotProvable("chained comparison changed")
            return None
        oa, ob = type(a.ops[0]), type(b.ops[0])
        if oa not in AST_OP or ob not in AST_OP:
            raise NotProvable("operator")
        left = literal(a.left, b.left)
        right = literal(a.comparators[0], b.comparators[0])
        if oa is not ob or left or right:
            out.append((a, AST_OP[ob], left, right))
        return None

    same(old_tree, new_tree)
    return out


def _decode(e):
    tag, v = e
    if tag == "tuple":
        return tuple(_decode(x) for x in v)
    if tag == "float" and v == "nan":
        return float("nan")
    return v


def _eval(op, a, b):
    try:
        r = OPFN[op](a, b)
    except Exception as e:  # noqa: BLE001
        return ("exc", type(e).__name__)
    return r if type(r) is bool else ("nonbool",)


def site_equivalent(entry, old_op, new_op, left, right):
    n, P, E, O, X, over = entry
    if X or over or n == 0:
        return False
    if (left or right) and (E or O):
        return False  # literal changes need concrete values
    identity = old_op in IDENTITY or new_op in IDENTITY
    for pa, pb in P:
        a, b = _decode(pa), _decode(pb)
        if identity and not any(x is None or type(x) is bool for x in (a, b)):
            return False  # identity of non-singleton values is not reproducible offline
        na = left[1] if left else a
        nb = right[1] if right else b
        if _eval(old_op, a, b) != _eval(new_op, na, nb):
            return False
    if E:
        if old_op not in OPS or new_op not in OPS:
            return False
        i, j = OPS.index(old_op), OPS.index(new_op)
        if any(v[i] != v[j] for v in E):
            return False
    if O:
        fam = {old_op, new_op}
        if not (fam <= {"is", "=="} or fam <= {"is not", "!="}):
            return False
    return True


def file_equivalent(map_path, rel, old_text, new_text):
    """True only if every difference between old_text and new_text is a comparison change that is
    provably result-preserving on every input the pinned test suite fed that comparison."""
    try:
        changes = changed_compares(ast.parse(old_text), ast.parse(new_text))
    except (NotProvable, SyntaxError, RecursionError):
        return False
    if not changes:
        return False  # nothing comparison-related to prove (caller handles token-identical files)
    m = load(map_path)
    for node, new_op, left, right in changes:
        old_op = AST_OP[type(node.ops[0])]
        entry = m.get(f"{rel}:{node.lineno}:{node.col_offset}")
        if entry is None or not site_equivalent(entry, old_op, new_op, left, right):
            return False
    return True
