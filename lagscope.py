#!/usr/bin/env python3
"""Start lagscope directly from the source folder (the built single file `lagscope` does the same)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lagscope.cli import main  # noqa: E402

sys.exit(main())
