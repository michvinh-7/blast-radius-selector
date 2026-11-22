"""Build time: every identifier mentioned anywhere in the pinned sources (NAME tokens, and every
identifier-like word inside any string, f-string or comment), so the selector can tell whether a
name a patch adds is mentioned by ANY pinned code or test.
  python names_build.py <app dir> <out names.txt>"""
import io, pickle, re, sys, tokenize, zlib
from pathlib import Path

app, out = Path(sys.argv[1]), Path(sys.argv[2])
src = pickle.loads((app / "src.bin").read_bytes())
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
names = set()
for path, blob in src.items():
    text = zlib.decompress(blob).decode("utf-8", "replace")
    names.update(WORD.findall(text))  # every word anywhere in any pinned file: strings, comments, code
out.write_text("\n".join(sorted(names)))
print("names:", len(names), "from", len(src), "files")
