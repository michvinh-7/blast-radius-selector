"""Build-time instrumentation (never shipped): per test, which keys of which sqlglot tables it reads.

pytest plugin (`-p tabrec`, PYTHONPATH=<this dir>). After collection (all sqlglot modules
imported, all classes created) every plain dict that is a sqlglot module global or a class
attribute of a sqlglot class is replaced by RecDict, a dict subclass that records, per running
test, (owner, attr, key) on every keyed read and (owner, attr, "*") on anything that sees the
whole table (iteration, len, copy, equality, views, writes). The generator's per-class dispatch
table (built lazily from TRANSFORMS + *_sql methods and cached across tests) is wrapped too, as
owner "<module>.<Generator qualname>", attr "<dispatch>".

Owner names: "<module>" for module globals, "<module>.<class qualname>" for class attributes
(the class that DEFINES the attribute). Keys are canonicalized by canon(); a key with no stable
canonical form records "*".

Output: $TABREC_DIR/<pid>.pkl = {nodeid: set((owner, attr, key))}. Reads outside a test (setup of
the session, collection) go to nodeid "" and must be treated as read by every test.
"""

import enum
import os
import pickle
import sys

CUR = [""]
REC = {}
_PAUSE = [0]


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


def _rec(tag, key):
    if _PAUSE[0]:
        return
    s = REC.get(CUR[0])
    if s is None:
        s = REC[CUR[0]] = set()
    s.add((tag[0], tag[1], key))


class RecDict(dict):
    __slots__ = ("_tag",)

    def __init__(self, tag, data):
        dict.__init__(self, data)
        self._tag = tag

    # keyed reads
    def __getitem__(self, k):
        _rec(self._tag, canon(k))
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _rec(self._tag, canon(k))
        return dict.get(self, k, default)

    def __contains__(self, k):
        _rec(self._tag, canon(k))
        return dict.__contains__(self, k)

    # whole-table reads
    def _all(self):
        _rec(self._tag, "*")

    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def __len__(self):
        self._all()
        return dict.__len__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def copy(self):
        self._all()
        return dict(dict.items(self))

    def __eq__(self, o):
        self._all()
        return dict.__eq__(self, o)

    def __ne__(self, o):
        self._all()
        return dict.__ne__(self, o)

    def __or__(self, o):
        self._all()
        return dict(dict.items(self)) | o

    def __ror__(self, o):
        self._all()
        return o | dict(dict.items(self))

    def __repr__(self):
        self._all()
        return dict.__repr__(self)

    def __reversed__(self):
        self._all()
        return dict.__reversed__(self)

    def __reduce_ex__(self, proto):
        self._all()
        return (dict, (dict(dict.items(self)),))

    # writes: any test that mutates a table depends on all of it
    def __setitem__(self, k, v):
        self._all()
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        self._all()
        dict.__delitem__(self, k)

    def setdefault(self, k, d=None):
        self._all()
        return dict.setdefault(self, k, d)

    def pop(self, *a):
        self._all()
        return dict.pop(self, *a)

    def popitem(self):
        self._all()
        return dict.popitem(self)

    def update(self, *a, **kw):
        self._all()
        dict.update(self, *a, **kw)

    def clear(self):
        self._all()
        dict.clear(self)

    __hash__ = None


def _wrap_class(cls, owner, seen):
    if id(cls) in seen:
        return
    seen.add(id(cls))
    for attr, v in list(vars(cls).items()):
        if type(v) is dict:
            try:
                type.__setattr__(cls, attr, RecDict((owner, attr), v))
            except (TypeError, AttributeError):
                pass
        elif isinstance(v, type) and v.__module__ == cls.__module__ and v.__qualname__.startswith(cls.__qualname__ + "."):
            _wrap_class(v, f"{v.__module__}.{v.__qualname__}", seen)


def install():
    seen = set()
    for name, mod in list(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        for g, v in list(vars(mod).items()):
            if type(v) is dict and not (g.startswith("__") and g.endswith("__")):
                setattr(mod, g, RecDict((name, g), v))
            elif isinstance(v, type) and v.__module__ == name:
                _wrap_class(v, f"{name}.{v.__qualname__}", seen)
    gen = sys.modules.get("sqlglot.generator")
    if gen is not None and hasattr(gen, "_build_dispatch"):
        orig = gen._build_dispatch

        def _build_dispatch(cls):
            _PAUSE[0] += 1  # the build reads all of TRANSFORMS once per class; attribute per-key reads instead
            try:
                d = orig(cls)
            finally:
                _PAUSE[0] -= 1
            return RecDict((f"{cls.__module__}.{cls.__qualname__}", "<dispatch>"), d)

        gen._build_dispatch = _build_dispatch
        cache = getattr(gen, "_DISPATCH_CACHE", None)
        if cache is not None:
            dict.clear(cache)


# ---- process pools: run each submitted task under the submitting test's id in the worker, which
# records with its own RecDicts and dumps on exit. Pool-worker starts are then not "invisible".
_POOL_SPAWN = [0]
_CHILD_READY = [False]


class _TabTask:
    def __init__(self, nodeid, fn):
        self.nodeid, self.fn = nodeid, fn

    def __call__(self, *a, **kw):
        if not _CHILD_READY[0]:
            _CHILD_READY[0] = True
            _child_init()
        prev = CUR[0]
        CUR[0] = self.nodeid
        try:
            return self.fn(*a, **kw)
        finally:
            CUR[0] = prev
            if _CHILD_DUMP[0] is not None:
                _CHILD_DUMP[0]()


def _child_init():
    import atexit
    import importlib
    import pkgutil

    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception:  # noqa: BLE001
                pass
    install()

    def dump():
        out = os.environ.get("TABREC_DIR")
        if out:
            with open(os.path.join(out, f"child-{os.getpid()}.pkl"), "wb") as f:
                pickle.dump({"rec": REC, "untracked": set(), "called": set()}, f)

    atexit.register(dump)
    # pool workers exit through os._exit-free paths in 3.13, but be safe: also dump after every task
    _CHILD_DUMP[0] = dump


_CHILD_DUMP = [None]


def _wrap_pools():
    import concurrent.futures.process as cfp

    orig_submit = cfp.ProcessPoolExecutor.submit

    def submit(self, fn, /, *a, **kw):
        return orig_submit(self, _TabTask(CUR[0] if isinstance(CUR[0], str) else CUR[0][1], fn), *a, **kw)

    cfp.ProcessPoolExecutor.submit = submit
    orig_adjust = cfp.ProcessPoolExecutor._adjust_process_count

    def adjust(self):
        _POOL_SPAWN[0] += 1
        try:
            return orig_adjust(self)
        finally:
            _POOL_SPAWN[0] -= 1

    cfp.ProcessPoolExecutor._adjust_process_count = adjust


UNTRACKED = set()  # tests that started a process (reads there are invisible) -> never narrowed


def _mark_spawns():
    import subprocess
    import multiprocessing.process as mpp
    import os as _os

    orig_popen_init = subprocess.Popen.__init__

    def popen_init(self, *a, **kw):
        UNTRACKED.add(CUR[0])
        return orig_popen_init(self, *a, **kw)

    subprocess.Popen.__init__ = popen_init
    orig_start = mpp.BaseProcess.start

    def start(self):
        if not _POOL_SPAWN[0]:
            UNTRACKED.add(CUR[0])
        return orig_start(self)

    mpp.BaseProcess.start = start
    for name in ("fork", "posix_spawn", "spawnv", "spawnve", "system", "execv", "execve"):
        f = getattr(_os, name, None)
        if f is not None:
            def wrap(*a, _f=f, **kw):
                UNTRACKED.add(CUR[0])
                return _f(*a, **kw)
            setattr(_os, name, wrap)


CALLED = set()  # tests whose call phase completed while recording


def pytest_configure(config):
    # import every sqlglot module, then wrap: reads made while test modules are collected are
    # recorded under "" and count for every test
    import importlib
    import pkgutil

    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try:
                importlib.import_module(m.name)
            except Exception:  # noqa: BLE001
                pass
    install()
    _mark_spawns()
    _wrap_pools()


def pytest_runtest_logreport(report):
    if report.failed:
        UNTRACKED.add(report.nodeid)  # a failing test may have stopped before reading what it needs
    elif report.when == "call":
        CALLED.add(report.nodeid)


# setup/teardown phases (setUpClass, fixtures) can compute state shared with later tests: they are
# recorded under ("S", nodeid); the merge attributes them to every test of the same file.
def pytest_runtest_setup(item):
    CUR[0] = ("S", item.nodeid)


def pytest_runtest_call(item):
    CUR[0] = item.nodeid


def pytest_runtest_logfinish(nodeid, location):
    CUR[0] = ""


def pytest_runtest_teardown(item, nextitem):
    CUR[0] = ("S", item.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("TABREC_DIR")
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, f"{os.getpid()}.pkl"), "wb") as f:
            pickle.dump({"rec": REC, "untracked": UNTRACKED, "called": CALLED}, f)
