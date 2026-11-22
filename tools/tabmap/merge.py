"""Merge tabrec.py recordings into tabmap.bin (shipped with the selector).

  python3 -I merge.py <tests.json> <rec_dir> <out tabmap.bin>

Rules (all conservative):
- reads outside any test ("" : collection, between tests) count for every test;
- setup/teardown-phase reads ("S", nodeid) count for every test of the same file;
- a test is UNTRACKED (never narrowed) if: it, or the setup of any test in its file, started a
  process; it failed/errored while recording; its call phase never completed (crashed worker);
  or its file keeps state across tests (SHARED_FILES);
- tests missing from the recording are untracked.
Output: {"ids": [test ids], "untracked": [idx], "idx": {(owner, attr): {key: [idx...]}}}
"""
import collections, glob, json, pickle, sys, zlib

# test_optimizer: its pool tasks are recorded in the workers (tabrec _TabTask); its setUpClass state is
# setup-phase (counted for the whole file). test_executor keeps a cross-test result cache.
SHARED_FILES = {"tests/test_executor.py", "tests/test_lineage.py", "tests/test_sqlglotc.py"}


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    ids = [t["id"] for t in tests]
    pos = {t: i for i, t in enumerate(ids)}
    rec = collections.defaultdict(set)
    untracked_raw, called = set(), set()
    n = 0
    for f in glob.glob(sys.argv[2] + "/*.pkl"):
        d = pickle.load(open(f, "rb"))
        n += 1
        for k, v in d["rec"].items():
            rec[k] |= v
        untracked_raw |= d["untracked"]
        called |= d["called"]
    print(n, "recording files;", len(called), "tests completed")

    def file_of(nodeid):
        return nodeid.split("::")[0]

    glob_reads = rec.get("", set())
    file_setup = collections.defaultdict(set)
    for k, v in rec.items():
        if isinstance(k, tuple):
            file_setup[file_of(k[1])] |= v
    bad_files = set(SHARED_FILES)
    for u in untracked_raw:
        if isinstance(u, tuple):
            bad_files.add(file_of(u[1]))
        elif u == "":
            print("WARNING: a process was started outside any test")
    untracked = set()
    for t in ids:
        if t not in called or t in untracked_raw or file_of(t) in bad_files:
            untracked.add(pos[t])
    idx = collections.defaultdict(lambda: collections.defaultdict(list))
    for t in ids:
        i = pos[t]
        if i in untracked:
            continue
        for (o, a, k) in rec.get(t, set()) | file_setup.get(file_of(t), set()) | glob_reads:
            idx[(o, a)][k].append(i)
    out = {"ids": ids, "untracked": sorted(untracked),
           "idx": {oa: dict(ks) for oa, ks in idx.items()}}
    blob = zlib.compress(pickle.dumps(out, protocol=4), 9)
    open(sys.argv[3], "wb").write(blob)
    print(f"tracked {len(ids) - len(untracked)}/{len(ids)}; tables {len(idx)}; "
          f"global reads {len(glob_reads)}; bad files {sorted(bad_files)}; {len(blob) / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
