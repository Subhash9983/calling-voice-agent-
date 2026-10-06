"""WP11 session evidence and operational report carry the composed first-audible breakdown."""

from __future__ import annotations

import json

from tests.support.first_audible import turn_latency_events
from tests.support.persistence_builders import make_config, make_session, new_id, now_ms

from voice_agent.domain.turn import ConversationTurn
from voice_agent.events_and_latency.evidence_report import aggregate_dict, session_dict
from voice_agent.events_and_latency.evidence_types import SessionEvidenceInput
from voice_agent.events_and_latency.first_audible import FirstAudibleMethod
from voice_agent.events_and_latency.latency import FIRST_AUDIBLE_RESPONSE
from voice_agent.events_and_latency.session_evidence import build_session_evidence


def _input() -> SessionEvidenceInput:
    session = make_session(make_config())
    start = now_ms()
    turns = tuple(
        ConversationTurn(turn_id=new_id(), session_id=session.session_id, sequence_number=n + 1)
        for n in range(3)
    )
    events = [
        *turn_latency_events(
            session,
            turns[0].turn_id,
            start=start,
            worker_span_ms=1_500,
            browser_playout_ms=90,
            network_one_way_ms=30,
        ),
        *turn_latency_events(
            session, turns[1].turn_id, start=start, worker_span_ms=1_700, browser_playout_ms=60
        ),
        *turn_latency_events(session, turns[2].turn_id, start=start, worker_span_ms=1_200),
    ]
    return SessionEvidenceInput(session=session, turns=turns, events=tuple(events))


def test_session_evidence_keeps_every_structured_sample_but_only_composed_in_the_summary() -> None:
    evidence = build_session_evidence(_input(), card=None)

    methods = [sample.method for sample in evidence.first_audible_samples]
    assert methods == [
        FirstAudibleMethod.COMPOSED,
        FirstAudibleMethod.COMPOSED_NETWORK_ASSUMED,
        FirstAudibleMethod.WORKER_ONLY,
    ]
    # The authoritative docs/02 metric: composed samples only (worker-only excluded).
    assert evidence.latency_samples[FIRST_AUDIBLE_RESPONSE] == (1_620, 1_860)
    assert evidence.latency[FIRST_AUDIBLE_RESPONSE].sample_count == 2


def test_report_views_split_composed_and_worker_only_samples() -> None:
    evidence = build_session_evidence(_input(), card=None)

    single = session_dict(evidence)
    pooled = aggregate_dict([evidence, evidence])
    json.dumps([single, pooled])

    breakdown = single["first_audible_response"]
    assert breakdown["sample_counts"] == {
        "composed": 1,
        "composed_network_assumed": 1,
        "worker_only": 1,
    }
    assert breakdown["worker_only_diagnostic"]["p50_ms"] == 1_200
    assert breakdown["meets_composed_method"] is False
    assert pooled["first_audible_response"]["composed_all"]["sample_count"] == 4
    assert pooled["first_audible_response"]["worker_only_diagnostic"]["sample_count"] == 2
