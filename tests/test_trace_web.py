"""The agent trace on the officer pages (app.agent_trace, templates/_trace.html)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from app.agent_trace import build_trace, report_traces
from app.assessment import AssessmentService
from app.db import make_recorder
from app.incidents import IncidentService
from app.intake import IntakeService
from app.main import create_app, duration, usd
from app.meta.base import CallRecord
from app.models import (
    AgentDecision,
    Assessment,
    ModelCall,
    OfficerAction,
    PhotoAssessment,
    Report,
    Reporter,
)
from app.trace import Step
from tests.test_assessment import FakeLLM, facts, jpeg
from tests.test_incidents import DIFFERENT, FakeJudge
from tests.test_trace import RecordingSam

STEP_LABELS_IN_ORDER = [
    "Read the facts",
    "Judge photo damage",
    "Find roofs (SAM)",
    "Find floodwater (SAM)",
    "Find the barangay",
    "Check trust signals",
    "Group into an incident",
]


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        yield client


async def assessed(settings, engine, user=111, caption="Baha po sa Brgy. Malaban", judge=None):
    recorder = make_recorder(engine)
    report = await IntakeService(settings, engine).ingest_photo(user, jpeg(), caption=caption)
    llm = FakeLLM(facts(flood_depth="knee", houses_flooded=23), recorder=recorder)
    grouping = IncidentService(settings, engine, judge or FakeJudge(engine=engine))
    await AssessmentService(settings, engine, llm, RecordingSam(recorder), grouping).assess(
        report.id
    )
    return report.id


def trace_html(html: str, report_id: int) -> str:
    start = html.index(f'data-details-key="trace-{report_id}"')
    return html[start : html.index("</details>", start)]


async def test_the_report_trace_lists_the_steps_in_order_with_model_latency_cache_and_cost(
    settings, client
):
    report_id = await assessed(settings, client.app.state.engine)

    trace = trace_html(client.get("/reports").text, report_id)

    positions = [trace.index(label) for label in STEP_LABELS_IN_ORDER]
    assert positions == sorted(positions)
    assert trace.count("Meta model</span>") == 4 and trace.count("Rule, no model") == 3
    assert "<code>fake-llm</code>" in trace and "<code>fake-sam</code>" in trace
    assert "5 ms · live call · $0.0100" in trace  # the facts call: latency, cache, cost
    assert "7 ms · live call · $0.0025" in trace  # each SAM call
    assert "flood knee-deep, 23 houses flooded" in trace
    assert "roof damage: partial" in trace and "2 roofs found" in trace
    assert "barangay Malaban (named in text)" in trace
    assert "started incident #1" in trace
    assert "Total: 4 Meta model calls (0 from cache)" in trace
    assert "$0.0250 estimated" in trace  # 0.01 + 0.01 + 2 x 0.0025
    assert "Agent trace <span" in trace and "7 steps · 4 Meta model calls" in trace


async def test_the_pair_judgment_shows_its_call_beside_the_decision(settings, client):
    engine = client.app.state.engine
    await assessed(settings, engine, user=111)
    second = await assessed(
        settings, engine, user=222, caption="Baha rin sa Malaban", judge=FakeJudge(DIFFERENT)
    )
    # FakeJudge without an engine logs nothing: log its call as the real client would.
    with Session(engine) as session:
        decision = session.exec(
            select(AgentDecision).where(
                AgentDecision.report_id == second, col(AgentDecision.judge_model).is_not(None)
            )
        ).one()
        session.add(
            ModelCall(
                at=decision.created_at,
                model="muse-spark-1.3",
                latency_ms=1200,
                cached=True,
                report_id=second,
                step=Step.PAIR_JUDGE,
                subject=f"incident:{decision.compared_incident_id}",
            )
        )
        session.commit()
        incident_id, compared = decision.incident_id, decision.compared_incident_id

    trace = trace_html(client.get(f"/incidents/{incident_id}").text, second)

    assert "Judge: same incident?" in trace
    assert "Muse Spark 1.3</span> <code>muse-spark-1.3</code>" in trace
    assert "Meta Model API" in trace and "1.2 s · from cache · $0" in trace
    expected = f"different from incident #{compared}, 96% → kept apart, in incident #{incident_id}"
    assert expected in trace


async def test_officer_actions_follow_the_agent_steps_labelled_as_human(settings, client):
    engine = client.app.state.engine
    report_id = await assessed(settings, engine)
    with Session(engine) as session:
        report = session.get(Report, report_id)
        session.add(
            OfficerAction(
                kind="officer_fallen_tree",
                report_id=report_id,
                incident_id=report.incident_id,
                officer="RC",
                detail={"verdict": "absent"},
                note="private note text",
            )
        )
        session.commit()

    trace = trace_html(client.get("/reports").text, report_id)

    assert trace.index("Group into an incident") < trace.index("Officer RC")
    assert "fallen tree reviewed: no fallen tree" in trace
    assert "private note text" not in trace  # free text stays on the incident page only


def test_a_report_assessed_before_the_trace_existed_says_so(settings, client):
    engine = client.app.state.engine
    with Session(engine) as session:
        reporter = Reporter(pseudonym="old")
        session.add(reporter)
        session.flush()
        report = Report(reporter_id=reporter.id, channel="telegram", text="baha", status="assessed")
        session.add(report)
        session.flush()
        session.add(Assessment(report_id=report.id, llm_model="muse-spark-1.3", facts=facts()))
        session.commit()
        report_id = report.id

    trace = trace_html(client.get("/reports").text, report_id)

    assert "Trace not recorded" in trace and "(2026-10-30)" in trace


def test_calls_without_a_saved_output_are_still_listed_and_counted():
    report = Report(id=1, reporter_id=1, channel="telegram", text="baha")
    at = datetime(2026, 10, 30, 4)
    calls = [
        ModelCall(
            id=1,
            at=at,
            model="sam-3.1",
            latency_ms=900,
            cached=False,
            cost_estimate=0.0025,
            report_id=1,
            step="sam_roof",
            subject="media:9",
        ),  # fmt: skip
        ModelCall(
            id=2,
            at=at,
            model="muse-spark-1.3",
            latency_ms=300,
            cached=True,
            report_id=1,
            step="facts",
        ),  # fmt: skip
    ]
    trace = build_trace(report, None, [], calls, [], [])
    assert [(s.label, s.output) for s in trace.steps] == [
        ("Read the facts", "no facts saved"),
        ("Find roofs (SAM)", "no saved output"),
    ]
    assert (trace.calls, trace.cached_calls, trace.model_ms) == (2, 1, 1200)
    assert trace.steps[1].family.name == "SAM 3.1" and trace.recorded


def test_repeated_calls_of_one_step_count_once_with_the_latest_latency(settings, client):
    engine = client.app.state.engine
    with Session(engine) as session:
        reporter = Reporter(pseudonym="r")
        session.add(reporter)
        session.flush()
        report = Report(reporter_id=reporter.id, channel="telegram", text="baha")
        session.add(report)
        session.flush()
        session.add(Assessment(report_id=report.id, llm_model="muse-spark-1.3", facts=facts()))
        for latency, cached in ((2000, False), (3, True)):  # assessed, then re-assessed
            session.add(
                ModelCall(
                    model="muse-spark-1.3",
                    latency_ms=latency,
                    cached=cached,
                    cost_estimate=0.0 if cached else 0.004,
                    report_id=report.id,
                    step="facts",
                )  # fmt: skip
            )
        session.commit()
        (trace,) = report_traces(session, [report]).values()
    (facts_step,) = [s for s in trace.steps if s.label == "Read the facts"]
    assert (facts_step.stats.calls, facts_step.stats.cached_calls) == (2, 1)
    assert (facts_step.stats.latency_ms, facts_step.stats.cached) == (3, True)
    assert facts_step.stats.cost == pytest.approx(0.004)


def test_photo_steps_are_numbered_when_a_report_has_several_photos():
    report = Report(id=1, reporter_id=1, channel="telegram")
    judgment = {"roof_damage": "none", "floodwater": True}
    measurement = {"roof_count": 1, "floodwater_present": True, "floodwater_fraction": 0.31}
    photos = [
        PhotoAssessment(
            report_id=1, media_id=m, llm_model="x", judgment=judgment, measurement=measurement
        )  # fmt: skip
        for m in (5, 4)
    ]
    trace = build_trace(report, None, photos, [], [], [])
    assert [s.label for s in trace.steps][:4] == [
        "Judge photo damage · photo 1",
        "Find roofs (SAM) · photo 1",
        "Find floodwater (SAM) · photo 1",
        "Judge photo damage · photo 2",
    ]
    assert trace.steps[2].output == "floodwater over 31% of the photo"


def test_durations_and_costs_read_plainly():
    assert [duration(v) for v in (None, 850, 4200, 65_000)] == ["–", "850 ms", "4.2 s", "1 min 5 s"]
    assert [usd(v) for v in (0, 0.0025, 1.934)] == ["$0", "$0.0025", "$1.93"]


async def test_the_rdana_draft_page_shows_the_calls_that_wrote_its_summary(settings, client):
    from tests.test_rdana import SummaryLLM, add_report, cite_all

    engine = client.app.state.engine
    add_report(engine, hazards=["flood"])
    recorder = make_recorder(engine)

    class Recording(SummaryLLM):
        model = "muse-spark-1.3"

        async def chat_json(self, **kwargs):
            recorder(CallRecord(self.model, 2100, False, 0.0031))
            return await super().chat_json(**kwargs)

    client.app.state.drafts.llm = Recording(cite_all)
    draft = await client.app.state.drafts.generate_rdana("Malaban")

    html = client.get(f"/drafts/{draft.id}").text

    assert "Model calls: 1 to Muse Spark 1.3 <code>muse-spark-1.3</code>" in html
    assert "(0 from cache) · 2.1 s · $0.0031 estimated." in html
