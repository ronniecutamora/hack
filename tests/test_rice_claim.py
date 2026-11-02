"""PCIC rice claims through the same pipeline: a Taglish report with a photo, its crop facts,
the Claim for Indemnity draft (RC-CAS-01) with its 20-day clock, fill, approval and the
printout in the paper's own words; and the office's PCIC lines (rice is off for Biñan)."""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import pcic_lines
from app.assessment import AssessmentService
from app.db import init_db, make_engine
from app.deadlines import DEADLINES
from app.facts import FACTS_SYSTEM, fact_lines
from app.intake import IntakeService
from app.main import create_app
from app.models import Draft, PhotoAssessment, Report
from app.modes import drafts_help, guide_steps
from app.nol import eligible
from app.nol_form import FISHERIES, RICE
from tests.test_assessment import FakeLLM, FakeSam, facts, jpeg
from tests.test_fill import approve, fill, paper, printed_words, saved_id, stamps
from tests.test_nol import Clock, fisherfolk_incident, generate
from tests.test_photo import JUDGMENT
from tests.test_rdana import AT, SummaryLLM, add_incident, add_report, cite_all

FARM = (14.2238, 121.3561)  # the demo's example farm: outside every Biñan barangay
TAGLISH = (
    "Lubog po ang palay namin dito sa Example Farm, mga isang ektarya po ang nakalubog. "
    "Namumulaklak na po sana."
)
RICE_FACTS = facts(
    hazards=["flood"],
    crop="rice",
    crop_area_hectares=1,
    crop_stage="reproductive",
    crop_damage=["submerged"],
    livelihood_damage=["crops"],
    place_detail="Example Farm",
    summary_en="About one hectare of flowering rice is submerged.",
)
FIELD = {
    **JUDGMENT,
    "roof_damage": "none",
    "debris": False,
    "house_count_estimate": 0,
    "crop_field": "flooded",
    "notes": "Floodwater covers most of a rice field.",
}
BOTH = ["fisheries", "rice"]


class RiceLLM(FakeLLM):
    """The facts reply for the text, a flooded-field judgment for the photo."""

    async def chat_json(self, **kwargs):
        reply = await super().chat_json(**kwargs)
        return FIELD if kwargs.get("image") is not None else reply


@pytest.fixture
def client(settings):
    with app_client(settings) as client:
        yield client


@pytest.fixture
def engine(client):
    return client.app.state.engine


def rice_on(settings):
    return settings.model_copy(update={"pcic_commodities": BOTH})


def ingest_and_assess(settings) -> tuple[int, RiceLLM]:
    """The Taglish photo report and its pin, through intake and assessment (models faked),
    received at AT like a backdated demo report."""
    engine = make_engine(settings)
    init_db(engine)
    llm = RiceLLM(RICE_FACTS)

    async def run():
        intake = IntakeService(settings, engine)
        photo = await intake.ingest_photo(9, jpeg(), caption=TAGLISH)
        pin = await intake.ingest_location(9, *FARM)
        assert pin.id == photo.id  # the pin joins the photo's report
        with Session(engine) as session:
            row = session.get(Report, photo.id)
            row.received_at = AT
            session.add(row)
            session.commit()
        out = await AssessmentService(settings, engine, llm, FakeSam()).assess(photo.id)
        assert out.status == "assessed"
        return photo.id

    report_id = asyncio.run(run())
    engine.dispose()
    return report_id, llm


def app_client(settings):
    clock = Clock(AT + timedelta(hours=5))
    app = create_app(settings, summary_llm=SummaryLLM(*[cite_all] * 5), clock=clock)
    return TestClient(app, base_url="http://localhost")


def rice_incident(engine) -> int:
    with Session(engine) as session:
        return session.exec(select(Report)).one().incident_id


# --- end to end -----------------------------------------------------------------------------


def test_a_taglish_rice_report_becomes_a_claim_for_indemnity_that_prints_as_rc_cas_01(settings):
    settings = rice_on(settings)
    report_id, llm = ingest_and_assess(settings)
    facts_call = next(c for c in llm.calls if c.get("image") is None)
    assert facts_call["system"] == FACTS_SYSTEM and "Example Farm" in facts_call["user_text"]
    with app_client(settings) as client:
        engine = client.app.state.engine
        with Session(engine) as session:
            report = session.get(Report, report_id)
            photo = session.exec(select(PhotoAssessment)).one()
        assert report.barangay is None and report.incident_id is not None  # pinned, outside
        assert photo.judgment["crop_field"] == "flooded"
        incident = report.incident_id

        drafts = client.get("/drafts").text
        section = drafts[drafts.index('id="nol-rice"') :].split("</section>")[0]
        assert "PCIC Claim for Indemnity (rice)" in section
        assert "RC-CAS-01 (Rev. 2017/FEB)" in section and "damage to a rice crop" in section
        assert "Example Farm" in section  # the incident's place, as no barangay is known
        assert "Section III" in section and "Generate Claim for Indemnity draft" in section

        draft_id = generate(client, incident, commodity="rice")
        with Session(engine) as session:
            draft = session.get(Draft, draft_id)
        assert draft.kind == "pcic_ci_rice" and draft.barangay is None
        content = draft.content
        assert content["document"] == "Claim for Indemnity" and content["place"] == "Example Farm"
        (deadline,) = content["deadlines"]
        assert deadline["key"] == "pcic_rice_ci"
        assert deadline["due_at"] == DEADLINES["pcic_rice_ci"].due_at(AT).isoformat()
        assert DEADLINES["pcic_rice_ci"].amount == 20
        values = {f["key"]: f["value"] for f in content["fields"]}
        assert values["I.4"].startswith("Example Farm; GPS ")
        assert "Biñan" not in values["I.4"]
        assert values["I.5"] == "Palay"
        assert values["II.1"] == "Flood (rice submerged)"
        assert values["II.2"].endswith("(first reported)")
        assert values["II.3"] == "Reproductive (booting to flowering) (as reported)"
        assert values["II.4"] == "1 (as reported)"
        assert values["II.5"] is None  # no share stated: never estimated from the photo
        assert {f["key"] for f in content["fields"] if f["mode"] == "assured"} >= {"I.1", "I.9"}
        (entry,) = content["annex"]
        assert entry["photos"][0]["judgment"]["crop_field"] == "flooded"

        page = client.get(f"/drafts/{draft_id}").text
        assert "CLAIM FOR INDEMNITY" in page and "crop field: flooded" in page
        assert "roof damage: none" not in page  # a field photo: no roof chip
        assert "PCIC Claim for Indemnity (rice)" in page and "the farmer files" in page

        filled = saved_id(
            fill(
                client,
                draft_id,
                [
                    ("date", "2026-10-28"),
                    ("I.1", "Juana Dela Cruz"),  # personal: typed for the printout, never stored
                    ("I.6", "1.5"),
                    ("I.7", "NSIC Rc 222"),
                    ("I.11", "RSBSA"),
                    ("II.2", "2026-10-27"),
                    ("II.5", "60"),
                    ("II.6", "2026-11-20"),
                ],
            )
        )
        with Session(engine) as session:
            entries = session.get(Draft, filled).entries
        assert set(entries) == {"date", "I.6", "I.7", "I.11", "II.2", "II.5", "II.6"}
        assert "Juana" not in str(entries)

        response = approve(client, filled, decided_by="CAO", version="2", reason="Checked.")
        assert response.status_code == 303, response.text
        page = client.get(f"/drafts/{filled}").text
    sheet, attachment = paper(page).split('<div class="paper paper--attachment', 1)
    assert "Attachment to the Claim for Indemnity (RC-CAS-01): photo evidence" in attachment
    assert sheet.startswith('<div class="paper paper--claim print-only">')
    (stamp,) = stamps(sheet)
    assert stamp.startswith("APPROVED · version 2 · ") and stamp.endswith("· CAO")
    assert [w for w in printed_words(sheet) if w not in claim_wording()] == []
    for words in RC_CAS_01:
        assert words in sheet, words
    for key in ("I.1", "I.2", "I.3", "I.9"):
        assert f'data-personal-print="{key}"' in sheet
    assert sheet.count('data-personal-print="I.1"') == 2  # the name, and over the signature
    assert "NSIC Rc 222" in sheet and "(✓)</span> RSBSA" in sheet and "(✓)</span> Palay" in sheet
    assert '<span class="paper__line">1.5</span><span class="paper__unit">ha.' in sheet
    for absent in ("<form", "<a ", "Annex", "Filing clock", "chip", "Juana"):
        assert absent not in sheet, absent


# The paper's own words, copied from RC-CAS-01 (Rev. 2017/FEB), and how they print.
RC_CAS_01 = (
    "RC-CAS-01<br>Rev. 2017/FEB",
    ">CLAIM FOR INDEMNITY</h1>",
    ">(PAGHAHABOL BAYAD)</p>",
    "DATE <i>(PETSA)</i>",
    "<p>The Chief CAD</p><p>PCIC-RO</p>",
    "<p>Please send your Team of Adjusters to assess damage of my insured crop.</p>",
    "<i>(Mangyaring magpadala kayo ng tagapag-imbistiga upang tasahin ang naging pinsala ng "
    "aking</i> pananim",
    "<p>Hereunder are the basic information needed by your office:</p>",
    "<i>Narito ang mga kinakailangang tala ng iyong tanggapan:)</i>",
    "BASIC INFORMATION <i>(MGA PANGUNAHING IMPORMASYON)</i>:",
    "Name of Farmer-Assured <i>(Pangalan ng Magsasaka)</i>:",
    "Address <i>(Tirahan)</i>:",
    "Cell Phone Number <i>(Numero ng Telepono)</i>:",
    "Location of Farm <i>(Lugar ng Saka)</i>:",
    "Insured Crops <i>(Pananim na Ipinaseguro)</i>:",
    "Area Insured <i>(Luwang/Sukat ng Bukid na Ipinaseguro)</i>:",
    "ha. <i>(ektarya)</i>",
    "Variety Planted <i>(Binhing Itinanim)</i>:",
    "Actual Date of Planting <i>(Aktwal na Petsa ng Pagkakatanim)</i>:",
    "CIC Number <i>(Numero ng CIC)</i>:",
    "Underwriter/Cooperative <i>(Pangalan ng Ahente o Kooperatiba)</i>:",
    "Program <i>(Programa)</i>:",
    "Sikat Saka",
    "APCP-CAP-PBD",
    "Cooperate Rice Farming",
    "DAMAGE INDICATORS <i>(MGA IMPORMASYON TUNGKOL SA PINSALA)</i>",
    "Cause of Loss <i>(Sanhi ng Pinsala)</i>:",
    "Date of Loss Occurrence <i>(Petsa ng Pinsala)</i>:",
    "Age/Stage of Cultivation at time of loss <i>(Edad ng Pananim ng Mapinsala)</i>:",
    "Area Damaged <i>(Luwang o Sukat ng Napinsalang Bahagi)</i>:",
    "Extent/Degree of Damage <i>(Tindi o Porsyento ng Pinsala)</i>:",
    "% <i>(porsyento)</i>",
    "Expected Date of Harvest <i>(Tinatayang Petsa ng Pagpapagapas o Pag-ani)</i>:",
    "LOCATION SKETCH PLAN OF DAMAGED INSURED CROPS (LSP)<br>"
    "<i>(KROKIS NG BUKID NG MGA NASALANTANG NAKASEGURONG PANANIM)</i>",
    "<i>Isulat ang pangalan ng mga may-ari/nagsasaka ng karatig na sakahan</i>",
    "North <i>(Hilaga)</i>",
    "South <i>(Timog)</i>",
    "East <i>(Silangan)</i>",
    "West <i>(Kanluran)</i>",
    ">Thank you.</p>",
    "<p>Very truly yours,</p>",
    "<strong>Signature over Printed Name of Assured Farmer-Claimant</strong>"
    "<i>(Lagda sa ibabaw ng Pangalan ng Magsasakang Nakaseguro)</i>",
)


def claim_wording() -> set[str]:
    """Every piece of text RC-CAS-01 prints, from the commodity's data."""
    c, p = RICE, RICE.claim_paper
    words = {c.form_id, c.revision, c.title, p.title_fil, "TO", ":", *p.to}
    words |= {text.strip() for line in p.request for text, _italic in line}
    for s in (*p.sections, p.sketch):
        words |= {s.numeral, s.title, f"({s.title_fil})"}
    for f in (*c.letter_fields, *c.fields):
        words |= {f"{f.number}.", f.label, f.paper_label or "", f"({f.label_fil})", *f.options}
        if f.paper_unit:
            words |= {f.paper_unit[0], f"({f.paper_unit[1]})"}
    words |= {f"{o}:" for o in p.write_in}
    words |= {"( )", "(✓)", p.sketch_note, "ha.", p.thanks, p.closing, p.signature}
    words |= {f"Lot {n}" for n in range(1, p.sketch_lots + 1)}
    words |= {f"{n}." for n in range(1, len(p.sketch_rows) + 1)}
    words |= {d for row in p.sketch_rows for d in (row[0], f"({row[1]})")}
    words |= {f"({p.signature_fil})"}
    return words - {""}


# --- facts ----------------------------------------------------------------------------------


def test_the_facts_prompt_knows_rice_words_and_keeps_fishponds_apart():
    for words in ("Palay, palayan and bukid", "palaisdaan is a fishpond, not a crop"):
        assert words in FACTS_SYSTEM
    for words in ('"lubog"', '"nadapa"', "namumulaklak", '"isang ektarya" = 1'):
        assert words in FACTS_SYSTEM
    assert fact_lines(RICE_FACTS)[0] == "rice, 1 ha, submerged, at the reproductive stage"
    stored_before_7m = {k: v for k, v in RICE_FACTS.items() if not k.startswith("crop_")}
    stored_before_7m["crop"] = "rice"
    assert fact_lines(stored_before_7m)[0] == "rice"


# --- one pipeline: eligibility, mixed reports, the lines an office drafts ------------------


def test_a_mixed_report_is_eligible_for_both_claims_and_a_fish_report_for_one(client, engine):
    from app.counting import collect

    mixed = add_incident(engine, barangay="De La Paz")
    add_report(
        engine,
        incident=mixed,
        barangay="De La Paz",
        livelihood_damage=["fish_pens_or_cages", "crops"],
        crop="rice",
    )
    fish, _first, _second = fisherfolk_incident(engine)
    with Session(engine) as session:
        city = collect(session)
    by_id = {i.incident_id: i for i in city.incidents()}
    assert eligible(FISHERIES, by_id[mixed]) and eligible(RICE, by_id[mixed])
    assert eligible(FISHERIES, by_id[fish]) and not eligible(RICE, by_id[fish])


def test_rice_is_off_for_binan_by_default_and_on_by_configuration(settings, client, engine):
    assert [c.key for c in pcic_lines.enabled(settings)] == ["fisheries"]
    assert [c.key for c in pcic_lines.enabled(rice_on(settings))] == BOTH
    rice_line = next(line for line in pcic_lines.LINES if line.commodity is RICE)
    assert not rice_line.on and rice_line.source.startswith("⚠️")
    with pytest.raises(ValueError, match="unknown commodities: corn"):
        pcic_lines.enabled(settings.model_copy(update={"pcic_commodities": ["corn"]}))

    incident = add_incident(engine, barangay="Casile")
    add_report(engine, incident=incident, barangay="Casile", crop="rice", hazards=["flood"])
    page = client.get("/drafts").text
    assert 'id="nol-rice"' not in page
    assert "Claim for Indemnity (rice)" not in page and "RC-CAS-01" not in page
    refused = client.post(
        "/drafts/notice-of-loss", data={"commodity": "rice", "incident_id": str(incident)}
    )
    assert refused.status_code == 422 and "does not draft PCIC rice claims" in refused.text
    help_page = client.get("/help").text
    assert "Claim for Indemnity" not in help_page and "rice farmers" not in help_page
    assert "the fisherfolk file their own Notice of Loss." in help_page


def test_the_guide_and_help_name_the_claims_an_office_drafts():
    (_, come_in), (_, drafts), _ = guide_steps([FISHERIES])
    assert come_in.startswith("Residents, barangay officials and fisherfolk send")
    assert drafts.endswith("fills in the RDANA form and the fisherfolk's Notices of Loss.")
    (_, come_in), (_, drafts), _ = guide_steps([FISHERIES, RICE])
    assert come_in.startswith("Residents, barangay officials, fisherfolk and rice farmers send")
    assert drafts.endswith(
        "the RDANA form, the fisherfolk's Notices of Loss and the rice farmers' Claims for "
        "Indemnity."
    )
    assert drafts_help([FISHERIES]) == (
        "The RDANA forms, the damaged-houses table and the Notices of Loss. Nothing is sent or "
        "filed until you approve it, and the fisherfolk file their own Notice of Loss."
    )
    assert drafts_help([FISHERIES, RICE]).endswith(
        "and the fisherfolk and rice farmers file their own claims."
    )
    assert drafts_help([]).endswith("until you approve it.")


def test_simple_mode_names_the_rice_claim_plainly_on_home(settings):
    settings = rice_on(settings).model_copy(update={"dashboard_mode": "simple"})
    ingest_and_assess(settings)
    with app_client(settings) as client:
        home = client.get("/").text
        assert "Example Farm: rice claim" in home and "The farmer must file it" in home
        incident = rice_incident(client.app.state.engine)
        draft_id = generate(client, incident, commodity="rice")
        home = client.get("/").text
        assert "Example Farm: rice claim ready to approve" in home
        drafts = client.get("/drafts").text
        assert "PCIC rice claim · Example Farm" in drafts
        assert "What&#39;s Claim for Indemnity (rice)?" in drafts or "What's Claim" in drafts
        page = client.get(f"/drafts/{draft_id}").text
        assert "Ulat sends nothing: the farmer files it with PCIC." in page
        help_page = client.get("/help").text
        assert "Claim for Indemnity (rice)" in help_page and "rice farmers" in help_page
