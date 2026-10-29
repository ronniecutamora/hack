"""Livelihood damage without a count: schema, old facts, rollup, the pair judge and RDANA 18."""

import json
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.db import init_db, make_engine
from app.facts import FACTS_SCHEMA, FACTS_SYSTEM, LIVELIHOOD_DAMAGE
from app.incidents import IncidentService, rollup
from app.main import fact_lines
from app.meta.schema import validate
from app.models import AgentDecision, Incident, Report
from app.pair_judge import report_context, safe_facts
from tests.test_assessment import facts
from tests.test_incidents import AT, FakeJudge, observation
from tests.test_incidents import add_report as add_grouping_report
from tests.test_rdana import add_incident, add_report, fields, generate

PENS = "fish_pens_or_cages"
DE_LA_PAZ_POINT = (14.3364, 121.0816)


@pytest.fixture
def engine(settings):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def stored_before_slice5(**fields):
    """Facts as stored before livelihood_damage, fishery_area_hectares and the new need."""
    old = facts(**fields)
    for key in ("livelihood_damage", "fishery_area_hectares"):
        old.pop(key, None)
    return old


def test_schema_accepts_livelihood_damage_and_the_materials_need():
    reply = facts(
        livelihood_damage=[PENS, "fishing_gear"],
        needs=["livelihood_materials"],
        fishery_area_hectares=0.5,
    )
    validate(FACTS_SCHEMA, reply)
    assert set(FACTS_SCHEMA["required"]) == set(FACTS_SCHEMA["properties"])
    assert FACTS_SCHEMA["properties"]["livelihood_damage"]["items"]["enum"] == list(
        LIVELIHOOD_DAMAGE
    )
    # Counts stay nullable: damage can be stated without a number.
    assert FACTS_SCHEMA["properties"]["fish_cages_or_pens_damaged"]["type"] == ["integer", "null"]
    for cue in ("fish cage", "fish pen", "baklad", "kulungan", "lambat", "kawayan", "bangka"):
        assert cue in FACTS_SYSTEM


def test_old_facts_without_the_new_fields_still_render_and_roll_up():
    old = stored_before_slice5(hazards=["flood"], fish_cages_or_pens_damaged=2)
    assert "livelihood_damage" not in old
    assert "2 fish cages/pens damaged" in fact_lines(old)
    result = rollup([observation(facts=old)])
    assert result["livelihood_damage"] == []
    assert result["report_count"] == 1
    assert "livelihood_damage" not in safe_facts(old)


def test_rollup_lists_each_stated_livelihood_damage_once_in_order():
    first = observation(facts=facts(livelihood_damage=[PENS, "fishing_gear"]))
    second = observation(
        id=2, at=AT + timedelta(days=1), facts=facts(livelihood_damage=["bogus", PENS])
    )
    assert rollup([second, first])["livelihood_damage"] == [PENS, "fishing_gear"]
    lines = fact_lines(facts(livelihood_damage=[PENS], needs=["livelihood_materials"]))
    assert "livelihood damage: fish pens or cages" in lines
    assert "needs: livelihood materials" in lines


def test_the_judge_sees_livelihood_damage_and_the_materials_need():
    context = report_context(
        barangay="De La Paz",
        facts=facts(livelihood_damage=[PENS], needs=["livelihood_materials"]),
        judgments=[],
    )
    assert context["facts"]["livelihood_damage"] == [PENS]
    assert context["facts"]["needs"] == ["livelihood_materials"]
    assert "livelihood_damage: ['fish_pens_or_cages']" in context["summary"]
    invalid = report_context(barangay=None, facts={"livelihood_damage": ["a name"]}, judgments=[])
    assert "livelihood_damage" not in invalid["facts"]


class LivelihoodJudge(FakeJudge):
    """Answers "same" only when both reports state the same livelihood damage, else "unsure"
    (what the real judge answered when neither report could say so)."""

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        sent = json.loads(kwargs["user_text"])
        new = sent["new_report"]["facts"].get("livelihood_damage")
        old = [r["facts"].get("livelihood_damage") for r in sent["incident_recent_reports"]]
        if new and all(o == new for o in old):
            return {"decision": "same", "confidence": 0.93, "reason": "Same damaged fish pens."}
        return {"decision": "unsure", "confidence": 0.75, "reason": "No damage stated."}


async def group_fisherfolk_pair(settings, engine, **second):
    service = IncidentService(settings, engine, LivelihoodJudge())
    first = add_grouping_report(
        engine,
        barangay="De La Paz",
        point=DE_LA_PAZ_POINT,
        livelihood_damage=[PENS],
    )
    later = add_grouping_report(
        engine,
        barangay="De La Paz",
        at=AT + timedelta(days=10),
        point=(DE_LA_PAZ_POINT[0] + 0.0001, DE_LA_PAZ_POINT[1]),
        needs=["livelihood_materials"],
        **second,
    )
    for report_id in (first, later):
        await service.group(report_id)
    with Session(engine) as session:
        rows = session.exec(select(Report).order_by(Report.id)).all()
        decision = session.exec(
            select(AgentDecision).where(AgentDecision.report_id == later)
        ).first()
        incident = session.get(Incident, rows[0].incident_id)
        return rows, decision, incident


async def test_the_de_la_paz_fish_pen_pair_merges_when_both_state_the_damage(settings, engine):
    rows, decision, incident = await group_fisherfolk_pair(
        settings, engine, livelihood_damage=[PENS]
    )
    assert rows[0].incident_id == rows[1].incident_id
    assert decision.kind == "merged"
    assert incident.status == "open"
    assert incident.latest_facts["livelihood_damage"] == [PENS]


async def test_without_the_new_fields_the_pair_stays_split_for_review(settings, engine):
    rows, decision, _ = await group_fisherfolk_pair(settings, engine, livelihood_damage=[])
    assert rows[0].incident_id != rows[1].incident_id
    assert decision.kind == "needs_officer"


async def test_rdana_18_uses_livelihood_damage_and_the_materials_need(engine, settings):
    pens = add_incident(engine, barangay="De La Paz")
    first = add_report(
        engine,
        incident=pens,
        barangay="De La Paz",
        hazards=["strong_wind"],
        livelihood_damage=[PENS, "fishing_gear"],
    )
    second = add_report(
        engine,
        incident=pens,
        reporter="b",
        barangay="De La Paz",
        hours=200,
        livelihood_damage=[PENS],
        needs=["livelihood_materials"],
    )
    draft, _ = await generate(engine, settings, barangay="De La Paz")
    f = fields(draft)
    assert f["18.1"]["value"] == "damage reported to fish pens or cages, fishing gear"
    assert f["18.1"]["evidence"][0]["report_ids"] == [first, second]
    assert f["18.2"]["value"] == "Others" and "nets, bamboo or boats" in f["18.2"]["note"]
    assert f["18.2"]["evidence"][0]["report_ids"] == [second]
