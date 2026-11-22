"""Build time: fingerprint the pinned sqlglot tree (tabfp.py) three times and record what is not
stable across runs, so the runtime comparison never mistakes run-to-run noise for a change.

  python tab_build.py <app dir> <pinned root> <out pinned_tabfp.pkl>
"""
import os
import pickle
import subprocess
import sys
import tempfile

app, root, out = sys.argv[1], sys.argv[2], sys.argv[3]
runs = []
env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
for i in range(3):
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "fp.pkl")
        subprocess.run([sys.executable, "-S", os.path.join(app, "tabfp.py"), root, f] + (["--audit"] if i == 0 else []),
                       check=True, env=env, stdout=subprocess.DEVNULL)
        runs.append(pickle.load(open(f, "rb")))
base = runs[0]
unstable_keys, unstable_other, unstable_weak = set(), set(), set()
for r in runs[1:]:
    assert r["errors"] == base["errors"] and set(r["tables"]) == set(base["tables"]) and set(r["other"]) == set(base["other"])
    for oa, t in base["tables"].items():
        for k in set(t) | set(r["tables"][oa]):
            if t.get(k) != r["tables"][oa].get(k):
                unstable_keys.add((oa, k))
    for oa, (s, w) in base["other"].items():
        s2, w2 = r["other"][oa]
        if s != s2:
            unstable_other.add(oa)
            if w != w2:
                unstable_weak.add(oa)
base["unstable_keys"] = unstable_keys
base["unstable_other"] = unstable_other
base["unstable_weak"] = unstable_weak
pickle.dump(base, open(out, "wb"), protocol=4)
print(f"aliased {sorted(base['aliased'])}; weak_ok {len(base['weak_ok'])}")
print(f"tabfp: {len(base['tables'])} tables, {len(base['other'])} other; unstable keys {len(unstable_keys)}, "
      f"other {sorted(unstable_other)}, weak-unstable {sorted(unstable_weak)}")
