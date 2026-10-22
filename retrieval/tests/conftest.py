import sys
import types
from pathlib import Path

# expose scripts/try_synthesis.py as `scripts_path` for the live tests
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import try_synthesis  # noqa: E402

sys.modules["scripts_path"] = types.SimpleNamespace(run=try_synthesis.run)
