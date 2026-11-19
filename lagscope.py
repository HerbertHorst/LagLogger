#!/usr/bin/env python3
"""Start lagscope directly from the source folder (the built single file `lagscope` does the same)."""
import os
import sys

if sys.version_info < (3, 8):
    msg = "Lagscope braucht Python 3.8 oder neuer (gefunden: %d.%d)." % sys.version_info[:2]
    sys.stderr.write(msg + "\n")
    try:
        import subprocess
        subprocess.call(["notify-send", "Lagscope", msg])
    except Exception:
        pass
    sys.exit(1)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lagscope.cli import main  # noqa: E402

sys.exit(main())
