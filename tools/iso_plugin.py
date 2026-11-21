"""Run every collected test in its own forked child, with per-test coverage and open() tracing."""
import json, os, sys

ROOT = os.path.realpath(os.environ.get("TRACE_ROOT", os.getcwd()))
OUT = os.environ.get("ISO_OUT", "/home/claude/work")
SHARD = int(os.environ.get("ISO_SHARD", "0"))
NSHARDS = int(os.environ.get("ISO_NSHARDS", "1"))
CUR = [None]
OPENS = {}


def _rec(path):
    try:
        if isinstance(path, int):
            return
        p = os.fsdecode(os.fspath(path))
        ap = os.path.realpath(p if os.path.isabs(p) else os.path.join(os.getcwd(), p))
    except Exception:
        return
    if not ap.startswith(ROOT + os.sep):
        return
    rel = os.path.relpath(ap, ROOT)
    if ".pyc" in rel or rel.startswith((".git" + os.sep, ".coverage", ".pytest_cache")):
        return
    OPENS.setdefault(rel, set()).add(CUR[0] or "")


def _hook(event, args):
    if event == "open":
        _rec(args[0])
    elif event in ("os.listdir", "os.scandir") and args and args[0] is not None:
        _rec(args[0])


sys.addaudithook(_hook)


def pytest_collectstart(collector):
    CUR[0] = "collect:" + str(getattr(collector, "nodeid", ""))


def pytest_runtestloop(session):
    # record what collection opened (parent)
    with open(f"{OUT}/opens/collect_{SHARD}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in OPENS.items()}, f)
    OPENS.clear()
    import coverage
    results = {}
    for idx, item in enumerate(session.items):
        if idx % NSHARDS != SHARD:
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            status = "?"
            try:
                CUR[0] = item.nodeid
                os.environ["COV_CTX"] = item.nodeid
                os.environ["COV_IDX"] = str(idx)
                os.environ["COV_OUT"] = OUT
                if os.environ.get("ISO_HOOK"):
                    os.environ["COV_HOOK"] = "1"
                if os.environ.get("ISO_NOCOV"):
                    os.environ["COV_NOCOV"] = "1"
                cov = None
                if not os.environ.get("ISO_NOCOV"):
                    cov = coverage.Coverage(
                        data_file=f"{OUT}/cov/{idx}.db", source=["sqlglot", "tests"], config_file=False
                    )
                    cov.start()
                    cov.switch_context(item.nodeid)
                if os.environ.get("ISO_HOOK"):
                    import classhook
                    classhook.patch_loaded()
                reports = []
                orig = item.config.hook.pytest_runtest_logreport

                class Cap:
                    def pytest_runtest_logreport(self, report):
                        reports.append((report.when, report.outcome))

                item.config.pluginmanager.register(Cap(), "cap")
                item.config.hook.pytest_runtest_protocol(item=item, nextitem=None)
                if cov is not None:
                    cov.stop()
                    cov.save()
                if os.environ.get("ISO_HOOK"):
                    classhook.dump(f"{OUT}/inst/{idx}.main.json")
                status = json.dumps(reports)
                with open(f"{OUT}/opens/{idx}.json", "w") as f:
                    json.dump({k: sorted(v) for k, v in OPENS.items()}, f)
            except BaseException as e:  # noqa
                status = "ERR " + repr(e)
            os.write(w, f"{idx}\t{item.nodeid}\t{status}\n".encode())
            os._exit(0)
        os.close(w)
        out = b""
        while True:
            chunk = os.read(r, 65536)
            if not chunk:
                break
            out += chunk
        os.close(r)
        os.waitpid(pid, 0)
        with open(f"{OUT}/status_{SHARD}.tsv", "ab") as f:
            f.write(out)
    return True
