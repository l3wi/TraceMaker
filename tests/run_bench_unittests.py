#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""CTest entry point for the benchmark harness's unittest discovery."""
from __future__ import annotations

import importlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        importlib.import_module("shapely")
    except ModuleNotFoundError as error:
        if error.name != "shapely":
            raise
        print("SKIP: benchmark geometry unit tests require Shapely (use .venv/bin/python).")
        return 77
    suite = unittest.defaultTestLoader.discover(str(ROOT / "bench"), pattern="test_*.py")
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
