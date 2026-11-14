"""Put every tool folder on sys.path so tests can `import gsc_scraper`.

Each tool lives in its own folder under lib/ (lib/gsc/, lib/tgv/, ...). A test
imports its own tool and, for the cinema contract, the other chain's scraper too.
"""

from __future__ import annotations

import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent
for folder in sorted(LIB.iterdir()):
    if folder.is_dir() and not folder.name.startswith((".", "_")) and any(folder.glob("*.py")):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
