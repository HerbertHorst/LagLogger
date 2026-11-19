#!/usr/bin/env python3
"""Builds the portable single file `dist/lagscope` (a Python zipapp with all web files inside)."""
import os
import stat
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
OUT = os.path.join(DIST, "lagscope")
MAIN = "import sys\n" + r"""if sys.version_info < (3, 8):
    msg = "Lagscope braucht Python 3.8 oder neuer (gefunden: %d.%d)." % sys.version_info[:2]
    sys.stderr.write(msg + "\n")
    try:
        import subprocess
        subprocess.call(["notify-send", "Lagscope", msg])
    except Exception:
        pass
    sys.exit(1)
""" + "from lagscope.cli import main\nsys.exit(main())\n"

os.makedirs(DIST, exist_ok=True)
tmp = OUT + ".tmp"
with open(tmp, "wb") as f:
    f.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", MAIN)
        pkg = os.path.join(ROOT, "lagscope")
        for base, dirs, files in os.walk(pkg):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(base, name)
                z.write(full, os.path.relpath(full, ROOT))
os.replace(tmp, OUT)
os.chmod(OUT, os.stat(OUT).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
print(f"gebaut: {OUT} ({os.path.getsize(OUT) // 1024} KB)")
