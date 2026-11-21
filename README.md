# Blast Radius entry

`selector.py` picks, for each sqlglot patch, the tests that can catch it, and splits them over workers.
Everything it knows comes from the pinned sqlglot commit (eb11aec4be8cb2122afe713a4816662c3f0fb2fa).
Nothing is looked up by patch or by kind of edit: no table maps an edit to the tests it breaks.

## 1. Static selection from a coverage map

Two files are precomputed from the pinned commit:

* `map.bin` holds the following, all from the pinned commit:
  * Per-test line coverage. Each test runs alone in its own process, including spawned pool workers.
  * Which fixture files each test reads.
  * Which sqlglot classes each test instantiates.
  * The source structure: statements, functions and classes.
* `src.bin` is a snapshot of the pinned `sqlglot/` and `tests/` Python files. It's used to apply a patch
  and compare token streams, so no network is needed.

The rules:

* Comment- or whitespace-only changes, with an identical token stream, run nothing.
* Fixture edits run the tests that read the fixture.
* Code edits run the tests that executed the changed statement.
* Class-level constants run the tests that instantiated the class and read the attribute.
* Syntax errors and unknown cases fall back to wider sets.

## 2. Runtime check for `test_execute_tpcds` (`fixb.py`)

`test_execute_tpcds` takes about 21.5 s, and its coverage reaches most of the parser, optimizer and planner.
That makes it the most expensive test to select needlessly. The test:

1. Parses each TPC-DS query and pickles it to a pool worker.
2. Optimizes it against the schema.
3. Plans it, compiles the plan's expressions to Python and executes them.
4. Compares the result with duckdb running `transpile(sql, write="duckdb")`.

When a patch touches only `sqlglot/` and not `sqlglot/executor/`, and dropping the test would save more than
6 s, the selector checks whether it can drop the test:

* It writes the patched sources to `/tmp`.
* `fixb.py` rebuilds everything before execution for all 57 executed queries: the pickle round-trip AST, the
  optimized AST, the plan steps with their generated Python, and the duckdb SQL.
* It hashes each query's result and compares it with the pinned fingerprints. Those are computed by
  `fixb_build.py` during the Docker build, with the same Python.

If every fingerprint matches, the test receives identical input and runs on unchanged executor code, so its
outcome can't change, and the selector drops it. Any difference, exception, timeout or exhausted time budget
keeps the test. This is the "run sqlglot's code while you decide" option, and its CPU time is paid for.

## Validation (not shipped)

The selector was checked against synthetic mutants of the pinned commit, with ground truth from running the
full test suite on each. Those mutants and results live outside this repository and are never consulted at
runtime.

## Rebuilding the map

See `tools/`:

* `iso_plugin.py` runs each test in a forked child with coverage.
* `classhook.py` records instantiated classes.
* `build_map.py` assembles `map.bin` and `src.bin`.
