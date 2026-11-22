"""Docker build step for Fix B (build time is not scored).

Extracts the pinned sqlglot/ sources from src.bin into OUT_DIR/sqlglot and computes the pinned
TPC-DS pipeline fingerprints and the pinned import-failure list with this image's own Python,
so runtime comparisons are exact.

  python fixb_build.py APP_DIR OUT_DIR FP_JSON
"""

import pickle
import subprocess
import sys
import zlib
from pathlib import Path

app, out_dir, fp = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
src = pickle.loads((app / "src.bin").read_bytes())
n = 0
for rel, blob in src.items():
    if rel.startswith("sqlglot/"):
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zlib.decompress(blob))
        n += 1
print(f"extracted {n} sqlglot files", file=sys.stderr)
subprocess.run([sys.executable, "-S", str(app / "fixb.py"), str(out_dir), str(app / "fixb_tpcds.json"), str(fp)], check=True)
# fingerprinting twice must agree, or runtime comparisons would be meaningless
again = fp.with_suffix(".check.json")
subprocess.run([sys.executable, "-S", str(app / "fixb.py"), str(out_dir), str(app / "fixb_tpcds.json"), str(again)], check=True)
if again.read_bytes() != fp.read_bytes():
    sys.exit("fixb fingerprints are not deterministic")
again.unlink()
print(f"pinned fingerprints: {fp}", file=sys.stderr)
# Fix A: which sqlglot modules fail to import on the pinned tree (expected: none)
imp = fp.parent / "pinned_imports.json"
r = subprocess.run([sys.executable, "-S", str(app / "impcheck.py"), str(out_dir)], check=True, capture_output=True, text=True)
imp.write_text(r.stdout.strip())
print(f"pinned import failures: {r.stdout.strip()}", file=sys.stderr)
# TPC-H tests: pinned parse / duckdb-SQL fingerprints, checked twice for determinism
tp = fp.parent / "pinned_tpch.json"
for target in (tp, tp.with_suffix(".check.json")):
    subprocess.run([sys.executable, "-S", str(app / "tpchfp.py"), str(out_dir), str(app / "fixb_tpch.json"), str(target)], check=True)
if tp.read_bytes() != tp.with_suffix(".check.json").read_bytes():
    sys.exit("tpch fingerprints are not deterministic")
tp.with_suffix(".check.json").unlink()
print(f"pinned tpch fingerprints: {tp}", file=sys.stderr)
