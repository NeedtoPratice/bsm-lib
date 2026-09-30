import hashlib, sys
from pathlib import Path
root = Path(sys.argv[1])
for p in sorted(root.rglob("*")):
    if p.is_file() and p.suffix != ".md5":
        print(hashlib.md5(p.read_bytes()).hexdigest(), p.relative_to(root))
