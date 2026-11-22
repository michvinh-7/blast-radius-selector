"""Fingerprint every sqlglot table entry and every other module/class attribute of a sqlglot tree.

  python -S tabfp.py <root containing sqlglot/> <out.pkl>

Owners/keys are named exactly like tabrec.py records them:
  tables[(owner, attr)] = {canon(key): fp(value)}      plain dicts (module globals, class attributes)
  tables[(gen_owner, "<dispatch>")] = {...}             Generator dispatch tables (_build_dispatch)
  other[(owner, attr)] = fp(value)                      everything else
A table with a key that has no canonical form is fingerprinted whole under other[].
fp() ignores line numbers. Values it cannot fingerprint deterministically become "UNK:<type>"
and the caller must treat such an attribute as changed if it is compared at all.
"""
import enum
import functools
import hashlib
import importlib
import pickle
import pkgutil
import re
import sys
import types


def canon(k):
    if k is None or isinstance(k, (bool, int, float, str, bytes)):
        return repr(k)
    if isinstance(k, enum.Enum):
        return f"E:{type(k).__module__}.{type(k).__qualname__}.{k.name}"
    if isinstance(k, type):
        return f"T:{k.__module__}.{k.__qualname__}"
    if isinstance(k, tuple):
        parts = [canon(x) for x in k]
        return "*" if "*" in parts else "(" + ",".join(parts) + ")"
    if isinstance(k, frozenset):
        parts = sorted(canon(x) for x in k)
        return "*" if "*" in parts else "{" + ",".join(parts) + "}"
    return "*"


def code_fp(c, st):
    consts = tuple(code_fp(x, st) if isinstance(x, types.CodeType) else fp(x, st) for x in c.co_consts)
    return ("C", c.co_name, c.co_qualname, c.co_code, c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars,
            c.co_argcount, c.co_posonlyargcount, c.co_kwonlyargcount, c.co_flags, consts,
            c.co_exceptiontable)


def fp(v, st=None):
    if st is None:
        st = set()
    if v is None or isinstance(v, (bool, int, float, complex, str, bytes)):
        return (type(v).__name__, repr(v))
    if isinstance(v, enum.Enum):
        return ("E", canon(v))
    if isinstance(v, type):
        return ("T", v.__module__, v.__qualname__)
    if isinstance(v, types.ModuleType):
        return ("M", v.__name__)
    i = id(v)
    m = _MEMO.get(i)
    if m is not None and m[0] is v:
        return m[1]
    if i in st:
        return ("CYCLE",)
    st.add(i)
    try:
        r = _fp(v, st)
    finally:
        st.discard(i)
    if r[0] != "CYCLE" and "CYCLE" not in repr(r)[:0]:
        _MEMO[i] = (v, r)
    return r


_MEMO = {}


def _fp(v, st):
    if True:
        if isinstance(v, types.FunctionType):
            cells = tuple(fp(c.cell_contents, st) if _has(c) else ("EMPTY",) for c in (v.__closure__ or ()))
            return ("F", v.__module__, v.__qualname__, code_fp(v.__code__, st), fp(v.__defaults__, st),
                    fp(v.__kwdefaults__, st), cells)
        if isinstance(v, types.BuiltinFunctionType):
            return ("B", getattr(v, "__module__", None), v.__qualname__, fp(getattr(v, "__self__", None), st)
                    if isinstance(getattr(v, "__self__", None), (type, types.ModuleType)) else None)
        if isinstance(v, types.MethodType):
            return ("BM", fp(v.__func__, st), type(v.__self__).__qualname__)
        if isinstance(v, (staticmethod, classmethod)):
            return (type(v).__name__, fp(v.__func__, st))
        if isinstance(v, property):
            return ("PR", fp(v.fget, st), fp(v.fset, st), fp(v.fdel, st))
        if isinstance(v, functools.partial):
            return ("P", fp(v.func, st), fp(v.args, st), fp(v.keywords, st))
        if isinstance(v, re.Pattern):
            return ("RE", v.pattern, v.flags)
        if isinstance(v, dict):
            # order-insensitive: sqlglot builds some dicts from sets of classes (address-dependent order);
            # the selector refuses patches that reorder existing literal keys instead
            return ("D", type(v).__qualname__, tuple(sorted((repr(fp(k, st)), fp(x, st)) for k, x in v.items())))
        if isinstance(v, (list, tuple)):
            return (type(v).__qualname__, tuple(fp(x, st) for x in v))
        if isinstance(v, (set, frozenset)):
            return (type(v).__qualname__, tuple(sorted(repr(fp(x, st)) for x in v)))
        if isinstance(v, (types.MappingProxyType,)):
            return ("MP", fp(dict(v), st))
        if type(v).__module__ in ("typing", "types", "collections.abc") or isinstance(v, types.GenericAlias):
            r = repr(v)
            if " at 0x" not in r:
                return ("TY", type(v).__qualname__, r)
        d = getattr(v, "__dict__", None)
        slots = [s for c in type(v).__mro__ for s in getattr(c, "__slots__", ()) if isinstance(s, str)]
        if d is not None or slots:
            state = {}
            if d is not None:
                state.update(d)
            for s in slots:
                if hasattr(v, s):
                    state[s] = getattr(v, s)
            return ("O", type(v).__module__, type(v).__qualname__, fp(state, st))
        r = repr(v)
        if " at 0x" in r:
            return ("UNK", type(v).__qualname__)
        return ("R", type(v).__qualname__, r)


def _has(cell):
    try:
        cell.cell_contents
        return True
    except ValueError:
        return False


def h(x):
    return hashlib.sha1(repr(x).encode()).hexdigest()[:20]


def walk_owners():
    """Yield (owner, attr, value) exactly as tabrec.install() names them."""
    seen = set()

    def cls_attrs(cls, owner):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        for attr, v in list(vars(cls).items()):
            if attr in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__firstlineno__",
                        "__static_attributes__"):
                continue
            yield owner, attr, v
            if isinstance(v, type) and v.__module__ == cls.__module__ and v.__qualname__.startswith(cls.__qualname__ + "."):
                yield from cls_attrs(v, f"{v.__module__}.{v.__qualname__}")

    for name in sorted(sys.modules):
        mod = sys.modules[name]
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        for g, v in list(vars(mod).items()):
            if g.startswith("__") and g.endswith("__"):
                continue
            yield name, g, v
            if isinstance(v, type) and v.__module__ == name:
                yield from cls_attrs(v, f"{name}.{v.__qualname__}")


def audit(tables_keys, other, root):
    """Build-time only. (1) aliased: tables whose dict object is referenced from anywhere other than a
    sqlglot module namespace or a sqlglot class (instances, containers, closures, defaults...). Reads
    through such references are invisible to the recorder, so a change there keeps everything.
    (2) weak_ok: list/tuple attributes whose elements are all classes (order follows addresses, so it
    differs per process) and whose every use in sqlglot is `isinstance(x, NAME)` or `in NAME`."""
    import gc
    import os

    mod_dicts = {id(vars(m)) for n, m in sys.modules.items() if m is not None and (n == "sqlglot" or n.startswith("sqlglot."))}
    classes = set()
    objs = {}
    for owner, attr, v in walk_owners():
        if isinstance(v, type) and v.__module__.startswith("sqlglot"):
            classes.add(id(v))
            for r in gc.get_referents(vars(v)):  # the namespace dict behind the mappingproxy
                if isinstance(r, dict):
                    classes.add(id(r))
        if (owner, attr) in tables_keys:
            objs[(owner, attr)] = v
    aliased = set()
    me = {id(objs)}
    keys = list(objs)
    for oa in keys:
        v = objs[oa]
        rs = gc.get_referrers(v)
        for r in rs:
            if id(r) in mod_dicts or id(r) in classes or id(r) in me or isinstance(r, types.FrameType) or r is rs:
                continue
            aliased.add(oa)
            break
        del rs, v
    # source text of sqlglot, for the usage check
    src = []
    for dp, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if f.endswith(".py"):
                src.append(open(os.path.join(dp, f), encoding="utf-8").read())
    text = "\n".join(src)
    weak_ok = set()
    for owner, attr, v in walk_owners():
        if (owner, attr) not in other or not isinstance(v, (list, tuple)) or not v or not all(isinstance(x, type) for x in v):
            continue
        uses = [m for m in re.finditer(r"\b" + re.escape(attr) + r"\b", text)]
        ok = True
        for m in uses:
            line = text[text.rfind("\n", 0, m.start()) + 1: text.find("\n", m.end())]
            if re.match(r"\s*" + re.escape(attr) + r"\s*(:[^=]*)?=", line):
                continue  # the definition
            if re.search(r"isinstance\([^()]*,\s*" + re.escape(attr) + r"\s*\)", line) or re.search(r"\bin\s+" + re.escape(attr) + r"\b", line):
                continue
            ok = False
            break
        if ok:
            weak_ok.add((owner, attr))
    return aliased, weak_ok


def main():
    root, out = sys.argv[1], sys.argv[2]
    sys.path.insert(0, root)
    import sqlglot  # noqa: F401

    errors = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001
            errors.append((m.name, type(e).__name__))
    tables, other, unk = {}, {}, []
    gen = sys.modules["sqlglot.generator"]
    gens = {}
    for owner, attr, v in walk_owners():
        if isinstance(v, type) and issubclass(v, gen.Generator):
            gens[id(v)] = v
        if type(v) is dict:
            ks = {canon(k): k for k in v}
            if "*" not in ks and len(ks) == len(v):
                t = {}
                for ck, k in ks.items():
                    f = fp(v[k])
                    if "'UNK'" in repr(f):
                        unk.append((owner, attr, ck))
                    t[ck] = h(f)
                tables[(owner, attr)] = t
                continue
        f = fp(v)
        if "'UNK'" in repr(f):
            unk.append((owner, attr, None))
        weak = h(sorted(repr(fp(x)) for x in v)) if isinstance(v, (list, tuple)) else h(f)
        other[(owner, attr)] = (h(f), weak)  # weak: order-insensitive, used for attrs unstable across pinned runs
    for v in gens.values():
        d = gen._build_dispatch(v)
        t = {canon(k): h(fp(x)) for k, x in d.items()}
        tables[(f"{v.__module__}.{v.__qualname__}", "<dispatch>")] = t
    res = {"tables": tables, "other": other, "errors": sorted(errors), "unk": unk}
    if "--audit" in sys.argv:
        owner = attr = v = f = d = t = None  # drop references held by this frame's loop variables
        gens = None
        res["aliased"], res["weak_ok"] = audit(set(tables), set(other), root)
        print("aliased tables:", len(res["aliased"]), sorted(res["aliased"])[:15])
        print("weak_ok:", sorted(res["weak_ok"]))
    with open(out, "wb") as f:
        pickle.dump(res, f)
    print(len(tables), "tables", len(other), "other", len(errors), "import errors", len(unk), "unk", unk[:10])


if __name__ == "__main__":
    main()
