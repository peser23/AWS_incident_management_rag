from datetime import date

import pytest
from daimo_retrieval.schemas import Options

from daimo_web.state import (Turn, default_filters, describe_filters, filters_to_options, init_state,
                             new_conversation, validate_query)


def test_defaults_send_only_top_k():
    assert filters_to_options(default_filters()) == {"top_k": 5}


def test_full_filters_map_to_service_options():
    f = {"priority_max": 2, "category": ["Software"], "affected_service": ["VistA", "CPRS"],
         "assignment_group": ["Member Profile"], "closed_after": date(2024, 1, 31), "top_k": 7}
    opts = filters_to_options(f)
    assert opts == {"top_k": 7, "filters": {"priority_level_max": 2, "category": ["Software"],
                                             "affected_service": ["VistA", "CPRS"],
                                             "assignment_group": ["Member Profile"], "closed_after": "2024-01-31"}}
    Options.model_validate(opts)  # the contract accepts it


@pytest.mark.parametrize("top_k,expected", [(50, 10), (0, None), (1, 1)])
def test_top_k_clamped(top_k, expected):
    assert filters_to_options({**default_filters(), "top_k": top_k}).get("top_k") == expected


def test_every_option_combination_is_valid():
    for pr in (None, 1, 5):
        for closed in (None, date(2025, 6, 1)):
            f = {**default_filters(), "priority_max": pr, "closed_after": closed, "category": ["X"]}
            Options.model_validate(filters_to_options(f))


def test_init_state_is_idempotent():
    ss = {}
    init_state(ss, top_k_default=4)
    sid = ss["session_id"]
    ss["turns"].append(Turn("q1", {}))
    ss["filters"]["category"] = ["Software"]
    init_state(ss)  # a rerun
    assert ss["session_id"] == sid and len(ss["turns"]) == 1 and ss["filters"]["category"] == ["Software"]
    assert ss["filters"]["top_k"] == 4


def test_new_conversation_keeps_filters():
    ss = {}
    init_state(ss)
    sid = ss["session_id"]
    ss["turns"].append(Turn("q1", {}))
    ss["filters"]["priority_max"] = 2
    new_conversation(ss)
    assert ss["turns"] == [] and ss["session_id"] != sid and ss["filters"]["priority_max"] == 2


def test_turn_snapshot_is_independent():
    live = default_filters()
    t = Turn("q", dict(live))
    live["top_k"] = 9
    assert t.filters["top_k"] == 5


def test_turn_status_and_request_id(fixture_response):
    assert Turn("q", {}, response=fixture_response("no_match")).status == "no_match"
    t = Turn("q", {}, error={"kind": "busy", "message": "x", "request_id": "r1"})
    assert t.status == "error" and t.request_id == "r1"


@pytest.mark.parametrize("text,ok", [("", False), ("   ", False), ("hi", True), ("  ab  ", True), ("x" * 1001, False)])
def test_validate_query(text, ok):
    assert (validate_query(text) is None) == ok


def test_describe_filters():
    f = {**default_filters(), "priority_max": 2, "affected_service": ["VistA"], "closed_after": date(2024, 1, 1)}
    assert describe_filters(f) == ["Priority ≤ 2", "Service: VistA", "Closed after 2024-01-01", "Top 5"]
