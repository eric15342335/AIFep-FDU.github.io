"""The sandbox helper module `agent/tools.py`, loaded from its file (it is not part of a package)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_tools", ROOT / "agent" / "tools.py")
assert spec and spec.loader
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)
