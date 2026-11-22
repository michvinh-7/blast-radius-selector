"""Merge per-process comparison dumps into entry/cmpmap.bin (zlib-compressed pickle).

  python3 -I tools/cmpmap/merge.py work/cmpmap/seed0 work/cmpmap/seed1 -o entry/cmpmap.bin

Site value: (n, P, E, O, X, over) with P/E frozensets. A site is kept only if it was executed;
a site with X or over is kept as unsafe (so the runtime knows it was seen but cannot reason).
"""

import argparse
import glob
import os
import pickle
import zlib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    agg = {}
    files = 0
    for d in a.dirs:
        for f in glob.glob(os.path.join(d, "*.pkl")):
            files += 1
            for k, v in pickle.load(open(f, "rb")).items():
                s = agg.setdefault(k, {"n": 0, "P": set(), "E": set(), "O": False, "X": False, "over": False})
                s["n"] += v.get("n", 0)
                s["P"] |= v.get("P", set())
                s["E"] |= v.get("E", set())
                s["O"] |= v.get("O", False)
                s["X"] |= v.get("X", False)
                s["over"] |= v.get("over", False)
    out = {}
    for k, s in agg.items():
        if s["n"] == 0 and not s["X"]:
            continue
        over = s["over"] or len(s["P"]) > 64 or len(s["E"]) > 64
        unsafe = s["X"] or over
        out[k] = (s["n"], frozenset() if unsafe else frozenset(s["P"]), frozenset() if unsafe else frozenset(s["E"]),
                  s["O"], s["X"], over)
    blob = zlib.compress(pickle.dumps(out, protocol=4), 9)
    open(a.out, "wb").write(blob)
    safe = sum(1 for v in out.values() if not v[4] and not v[5])
    print(f"{files} dump files -> {len(out)} sites ({safe} analyzable), {len(blob) // 1024} KB -> {a.out}")


if __name__ == "__main__":
    main()
