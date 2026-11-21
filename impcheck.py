"""Import every module of the sqlglot package at ROOT; print the sorted list of modules that fail.
  python -S impcheck.py ROOT [PINNED.json]
With PINNED.json (the failures on the pinned tree): exit 0 if the failures are identical, else 3."""
import importlib
import json
import pkgutil
import sys

root = sys.argv[1]
sys.path.insert(0, root)
failed = []
try:
    import sqlglot
    mods = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.") if not m.name.endswith("__main__"))
except Exception:
    print(json.dumps(["sqlglot"]))
    sys.exit(3)
for name in mods:
    try:
        importlib.import_module(name)
    except BaseException:  # noqa: BLE001 - any failure to import counts
        failed.append(name)
if len(sys.argv) > 2:
    sys.exit(0 if failed == json.load(open(sys.argv[2])) else 3)
print(json.dumps(failed))
