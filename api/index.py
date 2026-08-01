"""Vercel serverless entrypoint.

Vercel routes every file under `api/` to a function, and serves an ASGI app
assigned to the module-level name `app`. That is the whole contract; the real
application is the FastAPI instance in `api.py` at the project root.

Loading it is fiddlier than `from api import app` deserves to be. This file
lives in a directory called `api/`, and the application lives in a module called
`api.py` -- so a plain import is ambiguous to a reader even where it is not
ambiguous to the interpreter (a regular module wins over a namespace package,
so it happens to resolve correctly). Loading it explicitly by path removes the
question, and under a name that cannot collide.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `src.*` imports inside the application resolve against the project root, which
# is not on the path when the entrypoint is the one being executed.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("spendlens_service", ROOT / "api.py")
if _spec is None or _spec.loader is None:  # pragma: no cover - packaging error
    raise RuntimeError(f"could not load the application from {ROOT / 'api.py'}")

_module = importlib.util.module_from_spec(_spec)
# Registered before execution so that anything importing it mid-import (or a
# reload on a warm invocation) gets this instance rather than a second copy.
sys.modules["spendlens_service"] = _module
_spec.loader.exec_module(_module)

app = _module.app
