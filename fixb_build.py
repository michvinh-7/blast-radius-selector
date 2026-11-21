"""Docker build step for Fix B (build time is not scored).

Extracts the pinned sqlglot/ sources from src.bin into OUT_DIR/sqlglot and computes the pinned
TPC-DS pipeline fingerprints with this image's own Python, so runtime comparisons are exact.

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
