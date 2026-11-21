# Blast Radius entry

`selector.py` picks, for each sqlglot patch, the tests that can catch it and splits them over workers.
Nothing is run at decision time; it reads two precomputed files built from the pinned sqlglot commit
(eb11aec4be8cb2122afe713a4816662c3f0fb2fa):

* `map.bin` – per-test line coverage (each test run alone in its own process, including spawned pool
  workers), which fixture files each test reads, which sqlglot classes each test instantiates, source structure.
* `src.bin` – snapshot of the pinned `sqlglot/` and `tests/` python files (used to apply a patch and
  compare token streams; no network is needed).

Rules: comment/whitespace-only changes run nothing; fixture edits run the tests that read the fixture;
code edits run tests that executed the changed statement; class-level constants run tests that instantiated
the class (and read the attribute); syntax errors and unknown cases fall back to wider sets.

Rebuild: see `tools/` (`iso_plugin.py` runs each test in a forked child with coverage, `classhook.py`
records instantiated classes, `build_map.py` assembles `map.bin` and `src.bin`).
