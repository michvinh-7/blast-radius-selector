"""Record which sqlglot classes a test instantiates (with their MROs)."""
import json
import os
import sys

TARGETS = {
    "sqlglot.expressions.core": ["Expr"],
    "sqlglot.tokens": ["Tokenizer"],
    "sqlglot.parser": ["Parser"],
    "sqlglot.generator": ["Generator"],
    "sqlglot.dialects.dialect": ["Dialect"],
}
SEEN = set()
_patched = set()


def _wrap(cls):
    key = (cls.__module__, cls.__qualname__)
    if key in _patched:
        return
    _patched.add(key)
    orig = cls.__init__

    def __init__(self, *a, **kw):
        SEEN.add(type(self))
        orig(self, *a, **kw)

    cls.__init__ = __init__


def patch_module(name, mod):
    for cname in TARGETS.get(name, ()):
        cls = getattr(mod, cname, None)
        if cls is not None:
            _wrap(cls)


def patch_loaded():
    for name in TARGETS:
        mod = sys.modules.get(name)
        if mod is not None:
            patch_module(name, mod)


def install_import_hook():
    """For fresh interpreters: patch the targets right after they are imported."""
    import importlib.abc
    import importlib.machinery

    class Finder(importlib.abc.MetaPathFinder):
        busy = False

        def find_spec(self, name, path, target=None):
            if name not in TARGETS or Finder.busy:
                return None
            Finder.busy = True
            try:
                spec = importlib.machinery.PathFinder.find_spec(name, path)
            finally:
                Finder.busy = False
            if spec is None or spec.loader is None:
                return None
            loader = spec.loader
            orig = loader.exec_module

            def exec_module(mod, _orig=orig, _name=name):
                _orig(mod)
                patch_module(_name, mod)

            loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, Finder())


def dump(path):
    out = set()
    for cls in SEEN:
        for k in cls.__mro__:
            mod = getattr(k, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                out.add(f"{mod}:{k.__qualname__}")
    with open(path, "w") as f:
        json.dump(sorted(out), f)
