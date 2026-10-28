import json
from pathlib import Path

import pytest

from daimo_retrieval.schemas import TriageResponse

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name", ["answered", "answered_redacted", "no_match", "answer_withheld", "greeting", "out_of_scope", "blocked"])
def test_fixture_matches_schema(name):
    r = TriageResponse.model_validate(json.loads((FIX / f"{name}.json").read_text()))
    assert r.status == name.removesuffix("_redacted")
