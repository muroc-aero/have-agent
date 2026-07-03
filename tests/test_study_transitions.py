"""Exhaustive coverage of the §3.1 study state machine (same style as jobs)."""

import json

import pytest

from have_agent import study_transition
from have_agent.substrate import (
    _STUDY_HUMAN_REQUIRED,
    STUDY_STATUSES,
    STUDY_TRANSITIONS,
    ActorNotAllowed,
    IllegalTransition,
    StudyNotFound,
)
from tests.conftest import events_for, force_study, study_row

HUMAN = "human:alex"
AGENT = "agent:have"
SYSTEM = "system:scheduler"

ALL_PAIRS = sorted((a, b) for a in STUDY_STATUSES for b in STUDY_STATUSES if a != b)
LEGAL_PAIRS = sorted(STUDY_TRANSITIONS)
ILLEGAL_PAIRS = sorted(set(ALL_PAIRS) - set(LEGAL_PAIRS))


def actor_for(pair):
    return HUMAN if pair in _STUDY_HUMAN_REQUIRED else AGENT


@pytest.mark.parametrize("pair", LEGAL_PAIRS, ids=lambda p: f"{p[0]}->{p[1]}")
def test_legal_transition(conn, pair):
    frm, to = pair
    sid = force_study(conn, status=frm)
    study_transition(conn, sid, to, actor_for(pair))
    assert study_row(conn, sid)["status"] == to
    events = events_for(conn, sid)
    assert len(events) == 1
    assert events[0]["verb"] == STUDY_TRANSITIONS[pair]
    payload = json.loads(events[0]["payload_json"])
    assert (payload["from"], payload["to"]) == pair


@pytest.mark.parametrize("pair", ILLEGAL_PAIRS, ids=lambda p: f"{p[0]}->{p[1]}")
def test_illegal_transition(conn, pair):
    frm, to = pair
    sid = force_study(conn, status=frm)
    with pytest.raises(IllegalTransition):
        study_transition(conn, sid, to, HUMAN)
    assert study_row(conn, sid)["status"] == frm
    assert events_for(conn, sid) == []


@pytest.mark.parametrize("pair", sorted(_STUDY_HUMAN_REQUIRED), ids=lambda p: f"{p[0]}->{p[1]}")
def test_human_required_rejects_machines(conn, pair):
    sid = force_study(conn, status=pair[0])
    with pytest.raises(ActorNotAllowed):
        study_transition(conn, sid, pair[1], SYSTEM)
    assert study_row(conn, sid)["status"] == pair[0]


@pytest.mark.parametrize(
    "pair", sorted(set(LEGAL_PAIRS) - _STUDY_HUMAN_REQUIRED), ids=lambda p: f"{p[0]}->{p[1]}"
)
def test_machine_only_rejects_humans(conn, pair):
    sid = force_study(conn, status=pair[0])
    with pytest.raises(ActorNotAllowed):
        study_transition(conn, sid, pair[1], HUMAN)


def test_plan_proposal_attached_at_proposed(conn):
    sid = force_study(conn, status="draft")
    study_transition(conn, sid, "proposed", AGENT, plan_proposal={"cases": 48})
    assert json.loads(study_row(conn, sid)["plan_proposal_json"]) == {"cases": 48}


@pytest.mark.parametrize("to", ["closed", "aborted"])
def test_terminal_sets_closed_at(conn, to):
    frm = "review" if to == "closed" else "running"
    sid = force_study(conn, status=frm)
    study_transition(conn, sid, to, HUMAN)
    assert study_row(conn, sid)["closed_at"] is not None


def test_unknown_study(conn):
    with pytest.raises(StudyNotFound):
        study_transition(conn, "no-such-study", "approved", HUMAN)


def test_create_study_emits_submitted_event(conn, study_id):
    events = events_for(conn, study_id)
    assert [e["verb"] for e in events] == ["study.submitted"]
    assert study_row(conn, study_id)["status"] == "draft"
