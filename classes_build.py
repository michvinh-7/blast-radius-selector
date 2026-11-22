"""Build time: sqlglot classes whose creation cannot observe a new plain attribute: metaclass is
type, abc.ABCMeta or sqlglot's _Dialect (audited: reads specific names only), and the class is not an
Enum (new constant = new member), dataclass / NamedTuple / TypedDict (new annotation = new field)
or Protocol. Rule (2) of narrow_insertion applies at class level only to these.
  python classes_build.py <pinned root> <out classes.txt>"""
import abc, dataclasses, enum, importlib, pkgutil, sys, typing

root, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
import sqlglot  # noqa: E402
from sqlglot.dialects.dialect import _Dialect  # noqa: E402

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not m.name.endswith("__main__"):
        importlib.import_module(m.name)
ok = set()
seen = set()


def visit(cls, modname):
    if id(cls) in seen:
        return
    seen.add(id(cls))
    plain = (type(cls) in (type, abc.ABCMeta, _Dialect) and not issubclass(cls, enum.Enum)
             and not dataclasses.is_dataclass(cls) and not typing.is_typeddict(cls)
             and not (issubclass(cls, tuple) and hasattr(cls, "_fields")) and not getattr(cls, "_is_protocol", False))
    if plain:
        ok.add(f"{modname}:{cls.__qualname__}")
    for v in list(vars(cls).values()):
        if isinstance(v, type) and v.__module__ == modname and v.__qualname__.startswith(cls.__qualname__ + "."):
            visit(v, modname)


for name, mod in list(sys.modules.items()):
    if mod is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
        continue
    for v in list(vars(mod).values()):
        if isinstance(v, type) and v.__module__ == name:
            visit(v, name)
open(out, "w").write("\n".join(sorted(ok)))
print("plain classes:", len(ok), "of", len(seen))
