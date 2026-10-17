"""Make the server's top-level modules importable from either test directory.

server/ goes on sys.path so `import protocol`, `import session` work; the repo root
also goes on it so tests using `from server.board import ...` keep working.
"""

import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
for _p in (_SERVER.parent, _SERVER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
