"""Test bootstrap.

NEWSBRIEF_DATA_DIR must be set BEFORE `brief` is imported anywhere: the module
binds DATA_DIR (and every path constant derived from it) at import time, and
the default is the container volume path /app/logs.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("NEWSBRIEF_DATA_DIR", tempfile.mkdtemp(prefix="newsbrief-test-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DB-backed tests skip when DATABASE_URL is unset (see tests/test_db.py), and say
# how to get one. They must never skip in CI: the workflow sets DATABASE_URL, so
# a skip there means the services block is broken, not that the test is optional.
