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
outcome can't change, and the selector drops it.

A patch may also touch files outside `sqlglot/`, but only when none of them can reach the test:

* **No Python files.** Every test module shares the pytest process, so a `.py` edit can't be ruled out.
* **Not the test's own files:** its module, `tests/helpers.py`, the TPC-DS fixture data, or any configuration file.
* **Not a file the test was recorded opening.**
* **`tpc-ds.sql` edits** are re-parsed with an exact copy of the test's fixture parser. Every `(meta, sql)` input
  must stay byte-identical, because the test parses all of them, not only the executed ones.
* **`tpc-h.sql` edits** only have to parse. The test's class setup parses that file, but the test never reads it.

A trace of the pinned commit found 18 lines outside `sqlglot/executor/` that run while the queries execute but
never in the fingerprinted pipeline: `find_tables`, `Schema.table_parts`/`find`, `Plan.leaves`, one generator
branch, and the Python generator's Boolean rendering. They're listed in `fixb_exec_only.json`. A patch that
touches any of them keeps `test_execute_tpcds`, because the fingerprint can't see those edits. Any difference, exception, timeout or exhausted time budget
keeps the test. This is the "run sqlglot's code while you decide" option, and its CPU time is paid for.

## 3. Function signatures and import-time safety (`impcheck.py`)

A `def` line runs at import, so line coverage credits it to every test that imports sqlglot. Editing a
signature (a default value, a parameter) can only matter in two places:

* At import time, if the new header can't be evaluated.
* Inside calls to that function, which run its body.

So a block that lies entirely inside a signature selects the tests that ran the function's body. That holds
only when no function is added, removed or renamed anywhere in the file.

Whenever a patch relies on that rule, or adds, removes or renames any name bound at module or class level
(for example a deleted enum member that another module's class body refers to), the selector runs
`impcheck.py` on a `/tmp` copy of the patched sources. It imports every `sqlglot` module and compares the
failures with the pinned list, which is computed at Docker build time. If any module imports differently,
every test is selected.

## 4. Value profile of comparisons (`valuemap.py`, `cmpmap.bin`)

**Map built from the pinned commit.** The full test suite runs on the pinned commit with every
single-operator comparison in `sqlglot/` instrumented. That includes import and collection time, pytest-xdist
workers and spawned pool workers. It runs twice, under different hash seeds, and both runs pass all 1620 tests.

For each comparison site, `cmpmap.bin` records what the comparison was evaluated on:

* The distinct operand pairs, with their types, when both operands are plain values (None, bool, int, float,
  str, or tuples of these).
* The results under every comparison operator, for Enum operands whose comparison methods aren't defined in
  sqlglot.
* Whether two objects with default, identity-based equality were compared.
* Whether anything unclassifiable was seen, or the value set overflowed. Either makes the site unusable.

This is an execution profile of the pinned code, in the same category as line coverage. It doesn't say which
tests any edit breaks.

**Runtime.** The check only applies when every change in the patch lies inside comparison nodes of `sqlglot/`
files, where a comparison's operator changed or one of its literal operands changed. For each changed site, the
selector re-evaluates the patched comparison on every recorded input and checks that it returns exactly the
same result as the original.

If every site does, no test can behave differently, by induction:

* Execution is identical up to the first evaluation of a changed site.
* That evaluation sees operands the profile recorded.
* So it returns the original result.
* Execution stays identical from there to the next evaluation, and so on.

The selector then selects nothing. If any site was never recorded, was unsafe, or differs on even one input,
or the patch changes anything else, the normal selection applies.

## 5. `test_generator_memory` (6.7 s): only when construction can change

That test builds every dialect's generator 1,100 times and asserts the memory retained by the last 1,000 is
under 1 KiB. The selector drops it only when all of these hold:

* Every change is import-time code: module level or a class body, never inside a function.
* The changes are in sqlglot files off the construction path. Excluded files are `generator.py`,
  `dialects/dialect.py`, the tokenizer modules, `helper.py`, and the Athena/Hive/Trino/BigQuery modules whose
  `__init__` builds other dialects.
* No change is inside a `Tokenizer` class (several attributes that construction reads are derived from it).
* No change binds or mentions an attribute that `Generator.__init__` reads.

Such code runs once, at import, during the test's warm-up and before memory tracing starts. `_build_dispatch`
only copies `TRANSFORMS`. A cold process on the patched tree imports every module and builds one generator per
dialect; if anything differs from the pinned run, the test is kept.

## Validation (not shipped)

The selector was checked against synthetic mutants of the pinned commit, with ground truth from running the
full test suite on each. Those mutants and results live outside this repository and are never consulted at
runtime.

## Rebuilding the map

See `tools/`:

* `iso_plugin.py` runs each test in a forked child with coverage.
* `classhook.py` records instantiated classes.
* `build_map.py` assembles `map.bin` and `src.bin`.

## 6. Table-entry narrowing (`tabmap.bin`, `tabfp.py`, `tab_build.py`)
Patches that only edit entries of class-level table literals (TRANSFORMS, FUNCTIONS, KEYWORDS,
arg_types, ...) used to select every test of the class. `tools/tabmap/tabrec.py` re-runs the pinned
suite with every sqlglot module/class dict (and the generator dispatch tables, and process-pool
tasks) replaced by a recording dict, giving per test the (owner, table, key) entries it read and the
tables it saw whole. At decision time `tabfp.py` fingerprints every table entry and every other
module/class attribute of the patched tree and compares with the pinned fingerprint (built in the
image by `tab_build.py`, with an alias audit). A test is dropped only if it is tracked (no process,
no failure while recorded, no cross-test state) and read none of the changed entries; any non-table
change, import difference, aliased table or unknown keeps the selection unchanged.
