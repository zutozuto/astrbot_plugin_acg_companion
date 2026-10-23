"""Load this checkout under a stable name, without machine-specific paths."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

if app := os.environ.get("ASTRBOT_APP"):
    sys.path.insert(0, app)

PLUGIN = Path(__file__).resolve().parents[1]
if "acg_companion" not in sys.modules:
    spec = importlib.util.spec_from_file_location("acg_companion", PLUGIN / "__init__.py",
                                                submodule_search_locations=[str(PLUGIN)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

# Never write test databases into the live AstrBot data directory.
TEST_TEMP = Path(tempfile.gettempdir())
