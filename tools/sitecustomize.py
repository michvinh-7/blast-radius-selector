import os, sys

_ctx = os.environ.get("COV_CTX")
if _ctx:
    try:
        import coverage
        if os.environ.get("COV_HOOK"):
            import classhook
            classhook.install_import_hook()

        _out = os.environ.get("COV_OUT", "/home/claude/work")
        _cov = coverage.Coverage(
            data_file=f"{_out}/sub/{os.environ.get('COV_IDX', 'x')}.{os.getpid()}.db",
            source=["sqlglot", "tests"],
            config_file=False,
            context=_ctx,
        )
        if not os.environ.get("COV_NOCOV"):
            _cov.start()
        _saved = []

        def _save(*a):
            if not _saved:
                _saved.append(1)
                try:
                    if os.environ.get("COV_HOOK"):
                        classhook.dump(f"{_out}/inst/{os.environ.get('COV_IDX', 'x')}.{os.getpid()}.json")
                    if not os.environ.get("COV_NOCOV"):
                        _cov.stop()
                        _cov.save()
                except Exception:
                    pass

        import atexit

        atexit.register(_save)
        try:
            import multiprocessing.util as _mu

            _mu.Finalize(None, _save, exitpriority=100)
        except Exception:
            pass
    except Exception as e:  # never break the interpreter
        sys.stderr.write(f"sitecustomize coverage failed: {e!r}\n")
