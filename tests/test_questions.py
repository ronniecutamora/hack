"""Simple mode's Review (app.questions): one question per open reason, answered with a tap, each
answer recording the same officer action as Technical mode's forms."""

import asyncio
import html as html_lib
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import Session, select

from app.auth import DEV_OFFICER_COOKIE
from app.incidents import IncidentService
from app.main import create_app
from app.models import (
    OFFICER_FALLEN_TREE,
    OFFICER_KEPT_SEPARATE,
    OFFICER_MERGED,
    OFFICER_SET_PLACE,
    OFFICER_SIGNAL_REVIEWED,
    Assessment,
    OfficerAction,
    PhotoAssessment,
)
from app.questions import DUPLICATE, PLACE, SIGNAL, TREE, place_choices, plan, summary
from app.review import queue
from tests.test_incidents import AT, FakeJudge
from tests.test_incidents import add_report as add_grouped_report
from tests.test_rdana import SummaryLLM, cite_all
from tests.test_review import casile_with_uncertain_photo, flag_photo_reuse


@pytest.fixture
def judge():
    return FakeJudge()


def simple_client(settings, judge):
    app = create_app(
        settings.model_copy(update={"dashboard_mode": "simple"}),
        summary_llm=SummaryLLM(*[cite_all] * 20),
        pair_judge=judge,
    )
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def client(settings, judge):
    with simple_client(settings, judge) as client:
        client.post("/initials", data={"officer": "JDC"})
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def items(page: str) -> list[str]:
    """The queue's questions, in order, as their keys."""
    return re.findall(r'<li class="q[^"]*" id="(q-[^"]+)"', page)


def item(page: str, key: str) -> str:
    start = page.index(f'id="{key}"')
    return page[start : page.index("</li>", page.index('class="q__more"', start))]


def answers(fragment: str) -> list[tuple[str, str, dict[str, str]]]:
    """Each answer button in a question: (label, action, hidden fields)."""
    found = []
    for match in re.finditer(r"<form\b[^>]*data-answer[^>]*>(.*?)</form>", fragment, re.S):
        tag = match.group(0)[: match.group(0).index(">") + 1]
        action = re.search(r'action="([^"]+)"', tag).group(1)
        fields = {
            name: html_lib.unescape(value)
            for name, value in re.findall(
                r'<input type="hidden" name="([^"]+)" value="([^"]*)"', match.group(1)
            )
        }
        label = re.search(r"<button[^>]*>([^<]+)</button>", match.group(1)).group(1).strip()
        found.append((html_lib.unescape(label), action, fields))
    return found


def tap(client, fragment: str, label: str):
    """Tap one answer: post its form exactly as the browser would."""
    ((action, fields),) = [(a, f) for lbl, a, f in answers(fragment) if lbl == label]
    return client.post(action, data=fields, follow_redirects=False)


def actions(engine) -> list[OfficerAction]:
    with Session(engine) as session:
        return list(session.exec(select(OfficerAction).order_by(OfficerAction.id)).all())


def test_a_report_without_a_tree_suggestion_is_never_asked_about_a_tree(
    client, engine, settings, judge
):
    _voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    page = client.get("/review").text
    assert items(page) == [f"q-{photo.id}-{DUPLICATE}"]
    assert "fallen tree" not in page.lower()
    assert "/review/tree" not in page  # not even under "More options"

    with Session(engine) as session:  # a photo judgment suggests one: now it is asked
        session.add(
            PhotoAssessment(
                report_id=photo.id, media_id=1, llm_model="fake", judgment={"fallen_tree": True}
            )
        )
        session.commit()
    page = client.get("/review").text
    assert items(page) == [f"q-{photo.id}-{DUPLICATE}", f"q-{photo.id}-{TREE}"]
    assert "Is there a fallen tree?" in item(page, f"q-{photo.id}-{TREE}")


def test_each_answer_records_the_same_officer_action_as_the_technical_forms(
    client, engine, settings, judge
):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    key = flag_photo_reuse(engine, voice.id)
    with Session(engine) as session:
        session.add(
            PhotoAssessment(
                report_id=voice.id, media_id=1, llm_model="fake", judgment={"fallen_tree": True}
            )
        )
        session.commit()
    page = client.get("/review").text
    duplicate = item(page, f"q-{photo.id}-{DUPLICATE}")
    assert re.search(r"Is this the same flood as .+ \(1 report\)\?", html_lib.unescape(duplicate))
    assert [a[0] for a in answers(duplicate)] == ["Same flood", "Different"]
    assert tap(client, duplicate, "Same flood").status_code == 303
    merged = actions(engine)[-1]
    assert (merged.kind, merged.report_id, merged.incident_id, merged.officer) == (
        OFFICER_MERGED,
        photo.id,
        voice.incident_id,
        "JDC",
    )

    page = client.get("/review").text
    signal = item(page, f"q-{voice.id}-{SIGNAL}-{key}")
    assert "Is it OK to use?" in html_lib.unescape(signal)
    assert tap(client, signal, "It's fine").status_code == 303
    reviewed = actions(engine)[-1]
    assert reviewed.kind == OFFICER_SIGNAL_REVIEWED
    assert reviewed.detail == {"key": key, "code": "photo_reused", "verdict": "genuine"}

    page = client.get("/review").text
    tree = item(page, f"q-{voice.id}-{TREE}")
    assert tap(client, tree, "No").status_code == 303
    verdict = actions(engine)[-1]
    assert (verdict.kind, verdict.detail) == (OFFICER_FALLEN_TREE, {"verdict": "absent"})
    assert "All done." in client.get("/review").text


def test_different_keeps_it_separate(client, engine, settings, judge):
    _voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    page = client.get("/review").text
    assert tap(client, item(page, f"q-{photo.id}-{DUPLICATE}"), "Different").status_code == 303
    assert actions(engine)[-1].kind == OFFICER_KEPT_SEPARATE


def test_an_unknown_place_is_answered_from_the_likely_barangays(client, engine, settings, judge):
    service = IncidentService(settings, engine, judge)
    asyncio.run(service.group(add_grouped_report(engine, barangay="Casile")))
    lost = asyncio.run(
        service.group(add_grouped_report(engine, barangay=None, at=AT + timedelta(hours=1)))
    )
    with Session(engine) as session:
        assessment = session.exec(select(Assessment).where(Assessment.report_id == lost.id)).one()
        assessment.geo_source, assessment.geo_detail = "ambiguous", "Malaban or San Antonio"
        session.add(assessment)
        session.commit()
    page = client.get("/review").text
    place = item(page, f"q-{lost.id}-{PLACE}")
    assert "Where is this?" in place
    # The names the agent found too many of first, then where floods are reported now.
    assert [a[0] for a in answers(place)][:3] == ["Malaban", "San Antonio", "Casile"]
    assert "Other…" in place and 'name="barangay"' in place
    assert tap(client, place, "Malaban").status_code == 303
    action = actions(engine)[-1]
    assert action.kind == OFFICER_SET_PLACE and action.detail["barangay"] == "Malaban"
    assert action.note  # the audit still has its reason


def test_initials_are_asked_once_and_never_shown_again_in_simple_mode(settings, judge):
    with simple_client(settings, judge) as client:
        engine = client.app.state.engine
        casile_with_uncertain_photo(engine, settings, judge)
        page = client.get("/review").text
        assert "Who is answering?" in page and 'action="/initials"' in page
        assert "data-answer" not in page  # no question until the officer says who answers
        refused = client.post("/initials", data={"officer": "  "})
        assert refused.status_code == 400 and "Write your initials" in refused.text

        saved = client.post("/initials", data={"officer": "MRS"}, follow_redirects=False)
        assert saved.status_code == 303 and DEV_OFFICER_COOKIE in saved.headers["set-cookie"]
        page = client.get("/review").text
        assert "Who is answering?" not in page and "Answering as MRS" in page
        assert not re.search(r'<input type="text" name="officer"', page)
        assert 'name="officer" value="MRS"' in page
        assert "Who is answering?" in client.get("/review?initials=change").text

        client.get("/?mode=technical")  # Technical mode keeps the visible field
        page = client.get("/review").text
        assert re.search(r'<input type="text" name="officer" required[^>]*value="MRS"', page)


def test_signed_in_initials_are_kept_in_the_signed_session(settings, judge):
    secured = settings.model_copy(
        update={
            "officer_passcode": SecretStr("correct horse battery"),
            "session_secret": SecretStr("s" * 40),
            "dashboard_mode": "simple",
        }
    )
    app = create_app(secured, summary_llm=SummaryLLM(*[cite_all] * 20), pair_judge=judge)
    with TestClient(app, base_url="http://localhost") as client:
        client.post("/login", data={"passcode": "correct horse battery", "officer": ""})
        casile_with_uncertain_photo(app.state.engine, secured, judge)
        assert "Who is answering?" in client.get("/review").text
        client.post("/initials", data={"officer": "JDC"})
        page = client.get("/review").text
        assert "Answering as JDC" in page and "Signed in as JDC" in page


def test_home_and_the_badge_count_the_questions_the_queue_asks(client, engine, settings, judge):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    flag_photo_reuse(engine, voice.id)
    page = client.get("/review").text
    asked = items(page)
    assert len(asked) == 2 and 'data-q-total data-kpi="review_total">2<' in page
    home = client.get("/").text
    assert "2 quick questions about reports" in home
    assert "1 may be the same flood as another report; 1 photo or pin to double-check" in home
    assert 'href="/review">Answer</a>' in home
    assert 'data-badge="review"><span class="visually-hidden">: </span>2<' in home
    with Session(engine) as session:
        assert len(plan(queue(session))) == 2


def test_skip_and_back_work_without_javascript(client, engine, settings, judge):
    voice, photo = casile_with_uncertain_photo(engine, settings, judge)
    flag_photo_reuse(engine, voice.id)
    first, second = items(client.get("/review").text)
    page = client.get(f"/review?at={second}").text
    assert f'id="{first}" data-key="{first}" data-q hidden' in page
    assert f'id="{second}" data-key="{second}" data-q>' in page
    assert f'href="/review?at={first}" data-q-go="prev"' in page


def test_place_choices_and_summary_read_plainly():
    assert summary([]) == ""
    assert place_choices(
        None, type("R", (), {"text": "Taga Casile po", "transcript": None})(), []
    ) == ["Casile"]
