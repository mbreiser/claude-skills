"""Make `bin/kicad-extract.py` importable as a module from tests.

The extractor lives at bin/kicad-extract.py — note the dash, which prevents
direct `import kicad-extract`. This conftest registers it under the name
`kicad_extract` (underscore) by loading the source file via importlib.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_BIN_PATH = _THIS_DIR.parent / "bin" / "kicad-extract.py"


def _load_extractor():
    spec = importlib.util.spec_from_file_location("kicad_extract", _BIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["kicad_extract"] = module
    spec.loader.exec_module(module)
    return module


# Eagerly load so test modules can `import kicad_extract`.
_load_extractor()
