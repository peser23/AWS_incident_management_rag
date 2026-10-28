import sys
import types
from pathlib import Path

# expose scripts/try_synthesis.py as `scripts_path` for the live tests
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import try_synthesis  # noqa: E402

sys.modules["scripts_path"] = types.SimpleNamespace(run=try_synthesis.run)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_guard_llm(request, monkeypatch):
    """Offline tests must never reach Bedrock through the guard classifier; an unmocked call fails open."""
    if request.node.get_closest_marker("integration"):
        return
    from daimo_retrieval import clients

    def refuse():
        raise RuntimeError("guard classifier called without a mock")
    monkeypatch.setattr(clients, "guard_bedrock_runtime", refuse)
