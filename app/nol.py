"""Fill a PCIC claim form (a commodity of app.nol_form: a Notice of Loss or a Claim for
Indemnity) for one incident.

Deterministic: no model call. Every evidence field is a rule over the incident's typed report
facts, with the reports behind it; identity, policy, lot and harvest fields are the assured's,
entered on the page (app.fillable). The evidence annex packages, per report, the time, photo
and SAM overlay links, the photo judgments and SAM measurements (evidence for the adjuster,
never the loss figure), a name-redacted excerpt of the message or voice note, and the trust
signals. The filled form is
stored once and never recomputed; the countdown is computed when the page is shown.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlmodel import Session, col, select

from app.counting import CityEvidence, IncidentEvidence, count, digest
from app.deadlines import DEADLINES, Countdown, countdown
from app.models import Assessment, Media, PhotoAssessment, Report, manila_time
from app.nol_form import SAM_CONCEPTS, Commodity, NolField
from app.pair_judge import redact_personal
from app.rdana import HAZARD_TYPES, count_field, figure_text, filled, incident_ref, mean_pin
from app.rdana_form import CITY, PROVINCE

NOT_FILED = (
    "Approved for filing, not filed: Ulat never sends anything. The assured (or the City "
    "Agriculturist on their behalf) files the {document} with PCIC."
)
SAM_LABEL = "evidence for the adjuster, not the loss figure"
PHOTO_EVIDENCE = "photo evidence"
PHOTO_PAGE = f"{PHOTO_EVIDENCE} page"
PHOTO_REFERENCE = f"measurements can be included on a separate {PHOTO_PAGE} ({SAM_LABEL})"
LEGACY_PHOTO_REFERENCE = (
    f", with the agent's judgments and SAM 3.1 measurements in the annex ({SAM_LABEL})"
)
EXCERPT_MAX = 280
JUDGMENT_KEYS = (
    "roof_damage",
    "floodwater",
    "debris",
    "tarpaulin",
    "fallen_tree",
    "house_count_estimate",
    "crop_field",
    "notes",
)


def current_photo_reference(text: str | None) -> str | None:
    """Correct the old generated wording when displaying an immutable saved draft."""
    return text.replace(LEGACY_PHOTO_REFERENCE, f"; {PHOTO_REFERENCE}") if text else text


def _reporting(incident: IncidentEvidence, list_field: str, values: tuple[str, ...]):
    """Reports listing any of `values` in `list_field`, and the values found in order."""
    ids, found = [], []
    for member in incident.members:
        listed = incident.facts(member).get(list_field) or []
        if hits := [v for v in values if v in listed]:
            ids.append(member.id)
            found += hits
    return ids, [v for v in values if v in found]


def stating(commodity: Commodity, incident: IncidentEvidence) -> list[int]:
    """The reports in the incident that state the commodity's damage: a livelihood damage, a
    positive count or a typed fact ("crop": "rice"), with or without a figure."""
    return [
        m.id
        for m in incident.members
        if any(v in (incident.facts(m).get("livelihood_damage") or [])
               for v in commodity.eligible_livelihood)
        or any((incident.facts(m).get(name) or 0) > 0 for name in commodity.eligible_counts)
        or any(incident.facts(m).get(name) == value for name, value in commodity.eligible_facts)
    ]  # fmt: skip


def eligible(commodity: Commodity, incident: IncidentEvidence) -> bool:
    """A report in the incident states the commodity's damage. A report about two insured
    livelihoods (a fish pen and a rice field) makes the incident eligible for both claims,
    each drafted from its own facts."""
    return bool(stating(commodity, incident))


def eligible_incidents(commodity: Commodity, city: CityEvidence) -> list[IncidentEvidence]:
    """Grouped incidents a Notice of Loss can be drafted for, oldest first."""
    return [i for i in city.incidents() if i.incident_id is not None and eligible(commodity, i)]


def fingerprint(incident: IncidentEvidence) -> str:
    """The incident's evidence plus its photos, since the annex shows each photo's assessment.

    A re-assessed or added photo makes the Notice of Loss stale; the RDANA fingerprint
    (IncidentEvidence.fingerprint_data) leaves photos out and is unchanged.
    """
    return digest(
        {
            "incident": incident.fingerprint_data(),
            "photos": [[m.id, [list(p) for p in m.photos]] for m in incident.members],
        }
    )


def occurred_at(incident: IncidentEvidence) -> datetime:
    """The earliest report: the loss may be earlier, never later."""
    return incident.members[0].at


def deadline_table(commodity: Commodity) -> list[dict[str, Any]]:
    """The commodity's deadline rows: its first filing first, then the later ones."""
    return [DEADLINES[key].as_dict() for key in (commodity.deadline, *commodity.later_deadlines)]


def deadline_rows(commodity: Commodity, occurred: datetime) -> list[dict[str, Any]]:
    """The commodity's deadline rows, each with its due time for a loss at `occurred`."""
    return [
        {**row, "due_at": DEADLINES[row["key"]].due_at(occurred).isoformat()}
        for row in deadline_table(commodity)
    ]


def countdowns(rows: list[dict[str, Any]], now: datetime) -> list[tuple[dict, Countdown]]:
    return [(row, countdown(datetime.fromisoformat(row["due_at"]), now)) for row in rows]


def excerpt(text: str) -> str:
    """Names and contact details removed first, then shortened, so a cut never exposes half of
    one. The assured gives their own contact number on the form (field 12)."""
    text = " ".join(redact_personal(text).split())
    return text if len(text) <= EXCERPT_MAX else text[: EXCERPT_MAX - 1].rstrip() + "…"


def _judgment(judgment: dict[str, Any] | None) -> dict[str, Any]:
    """The photo judgment's known keys; its free-text note is redacted like an excerpt."""
    kept = {k: judgment[k] for k in JUDGMENT_KEYS if k in (judgment or {})}
    if isinstance(kept.get("notes"), str):
        kept["notes"] = excerpt(kept["notes"])
    return kept


def annex(
    session: Session, incident: IncidentEvidence, commodity: Commodity
) -> list[dict[str, Any]]:
    """The evidence annex: one entry per report in the incident, oldest first."""
    ids = incident.report_ids
    reports = {r.id: r for r in session.exec(select(Report).where(col(Report.id).in_(ids)))}
    assessments = {
        a.report_id: a
        for a in session.exec(select(Assessment).where(col(Assessment.report_id).in_(ids)))
    }
    photos: defaultdict[int, list[PhotoAssessment]] = defaultdict(list)
    for row in session.exec(
        select(PhotoAssessment)
        .where(col(PhotoAssessment.report_id).in_(ids))
        .order_by(PhotoAssessment.id)
    ):
        photos[row.report_id].append(row)
    unassessed = session.exec(
        select(Media.id, Media.report_id).where(
            col(Media.report_id).in_(ids),
            Media.kind == "photo",
            col(Media.id).not_in(select(PhotoAssessment.media_id)),
        )
    ).all()
    measured = [k for concept in commodity.sam_concepts for k in SAM_CONCEPTS[concept]]
    entries = []
    for member in incident.members:
        report, assessment = reports[member.id], assessments.get(member.id)
        voice = (assessment.transcript_clean if assessment else None) or report.transcript
        entries.append(
            {
                "report_id": member.id,
                "at": manila_time(member.at),
                "excerpts": [
                    {"source": source, "text": excerpt(text)}
                    for source, text in (("message", report.text), ("voice note", voice))
                    if text and text.strip()
                ],
                "photos": [
                    {
                        "media_id": p.media_id,
                        "overlay_media_id": p.overlay_media_id,
                        "judgment": _judgment(p.judgment),
                        "measurement": {
                            k: p.measurement[k] for k in measured if k in (p.measurement or {})
                        },
                        "model": p.llm_model,
                    }
                    for p in photos[member.id]
                ]
                + [
                    {"media_id": media_id, "overlay_media_id": None, "judgment": {}}
                    for media_id, report_id in unassessed
                    if report_id == member.id
                ],
                "signals": [
                    {k: s.get(k) for k in ("code", "severity", "detail", "related_report_id")}
                    for s in (assessment.signals if assessment else None) or []
                ],
                "flagged": member.flagged,
            }
        )
    return entries


@dataclass(frozen=True)
class NolContext:
    commodity: Commodity
    incident: IncidentEvidence
    annex: list[dict[str, Any]]


def _location(ctx: NolContext):
    """The barangay, city and province when the incident has a barangay; without one, only
    what the reports say and the pin: a place outside Biñan's barangays is never called
    Biñan."""
    incident = ctx.incident
    where = incident.barangay and f"Barangay {incident.barangay}, {CITY}, {PROVINCE}"
    place, pin = incident.place(), mean_pin(list(incident.members))
    if not incident.barangay and not place and not pin:
        return None
    parts = [where, place, pin and f"GPS {pin[0]}"]
    notes = ["Place details as reported (names of people removed)."] if place else []
    if pin:
        notes.append(pin[1])
    refs = [incident_ref(incident, incident.report_ids)]
    return filled("; ".join(p for p in parts if p), "reports", refs, " ".join(notes))


def _cause(ctx: NolContext):
    commodity = ctx.commodity
    ids, found = _reporting(ctx.incident, "hazards", tuple(HAZARD_TYPES))
    detail_ids, details = [], []
    if commodity.cause_detail_fact:
        words = dict(commodity.cause_detail)
        detail_ids, details = _reporting(ctx.incident, commodity.cause_detail_fact, tuple(words))
        details = [words[d] for d in details]
    if not found and not details:
        return None
    value = ", ".join(HAZARD_TYPES[h] for h in found) or "Not stated"
    if details:
        value += f" ({', '.join(details)})"
    note = "As reported; PCIC's adjusters verify the cause of loss."
    refs = [incident_ref(ctx.incident, sorted({*ids, *detail_ids}))]
    return filled(value, "reports", refs, note)


def _first_reported(ctx: NolContext):
    first = ctx.incident.members[0]
    note = (
        "Time of the earliest report in this incident, Philippine time. The loss may be "
        "earlier: the assured corrects it if so. The filing deadlines run from the loss."
    )
    refs = [incident_ref(ctx.incident, [first.id])]
    return filled(f"{manila_time(first.at)} (first reported)", "reports", refs, note)


def _stated(ctx: NolContext, names: tuple[str, ...], unit: str, what: str):
    """The first stated figure among `names` (the counting rule), as reported."""
    for name in names:
        if (stated := count([ctx.incident], name)) is not None:
            figure = count_field(stated)
            note = "As stated in the reports; never estimated from photos or SAM masks."
            return filled(
                f"{figure_text(stated, unit)} (as reported)", "reports", figure["evidence"], note
            )
    return filled(None, None, (), f"No report states {what}; it is never estimated from photos.")


def _stated_area(ctx: NolContext):
    unit = f" {ctx.commodity.area_unit}" if ctx.commodity.area_unit else ""
    return _stated(ctx, ctx.commodity.area_facts, unit, "an area")


def _stated_percent(ctx: NolContext):
    return _stated(ctx, ctx.commodity.percent_facts, "", "a share of the crop lost")


def _insured(ctx: NolContext):
    """The commodity's own option ("Palay"): the reports are about it. The policy decides."""
    ids = stating(ctx.commodity, ctx.incident)
    note = "The reports are about this crop; check it against the assured's policy."
    return filled(ctx.commodity.insured_option, "reports", [incident_ref(ctx.incident, ids)], note)


def _stage(ctx: NolContext):
    """The latest stage a report states, in the form's words."""
    commodity, incident = ctx.commodity, ctx.incident
    labels = dict(commodity.stage_labels)
    for member in reversed(incident.members):
        stage = incident.facts(member).get(commodity.stage_fact or "")
        if stage in labels:
            note = (
                "As reported; the farmer gives the age in days. The stage of cultivation at the "
                "time of loss sets the indemnity."
            )
            refs = [incident_ref(incident, [member.id])]
            return filled(f"{labels[stage]} (as reported)", "reports", refs, note)
    return None


def _extent(ctx: NolContext):
    commodity, incident = ctx.commodity, ctx.incident
    parts, refs = [], []
    ids, found = _reporting(incident, "livelihood_damage", commodity.extent_livelihood)
    if found:
        parts.append("reported damage to " + ", ".join(v.replace("_", " ") for v in found))
        refs.append(incident_ref(incident, ids))
    for name in commodity.extent_counts:
        if (stated := count([incident], name)) is not None:
            parts.append(f"{name.replace('_', ' ')}: {figure_text(stated)} (reported count)")
            refs += count_field(stated)["evidence"]
    photographed = [e["report_id"] for e in ctx.annex if e["photos"]]
    if photographed:
        parts.append(
            f"photos from {len(photographed)} report{'s' if len(photographed) != 1 else ''}; "
            f"{PHOTO_REFERENCE}"
        )
        refs.append(incident_ref(incident, photographed))
    if not parts:
        return None
    value = "; ".join(parts)
    note = f"{commodity.loss_bands} Source: {commodity.loss_bands_source}."
    return filled(value[0].upper() + value[1:], "reports", refs, note)


RULES: dict[str, Callable[[NolContext], dict[str, Any] | None]] = {
    "location": _location,
    "cause": _cause,
    "first_reported": _first_reported,
    "stated_area": _stated_area,
    "extent": _extent,
    "insured": _insured,
    "stage": _stage,
    "stated_percent": _stated_percent,
}


def claim_place(draft: Any) -> str | None:
    """Where a claim draft is: its barangay, else the reports' place detail (an example farm
    outside the city's barangays); drafts made before the place was stored have only the first."""
    return draft.barangay or (draft.content or {}).get("place")


def fill_field(form_field: NolField, ctx: NolContext) -> dict[str, Any]:
    result = RULES[form_field.rule](ctx) if form_field.mode == "evidence" else None
    return {
        "key": form_field.key,
        "number": form_field.number,
        "label": form_field.label,
        "mode": form_field.mode,
        **(result or {"value": None, "source": None, "evidence": [], "note": None}),
    }


def fill_nol(
    commodity: Commodity,
    incident: IncidentEvidence,
    entries: list[dict[str, Any]],
    drafted_at: datetime,
) -> dict[str, Any]:
    """The complete draft as JSON, in the form's order, with its annex and deadlines."""
    ctx = NolContext(commodity, incident, entries)
    fields = [fill_field(f, ctx) for f in commodity.fields]
    occurred = occurred_at(incident)
    reused = [
        {"report_id": e["report_id"], "related_report_id": s["related_report_id"]}
        for e in entries
        for s in e["signals"]
        if s["code"] == "photo_reused"
    ]
    missing = []
    if "photo" in commodity.required_evidence and not any(e["photos"] for e in entries):
        missing.append("No report in this incident has a photo of the damage.")
    return {
        "commodity": commodity.key,
        "commodity_name": commodity.name,
        "document": commodity.document,
        "title": commodity.title,
        "form_id": commodity.form_id,
        "revision": commodity.revision,
        "form_source": commodity.form_source,
        "form_url": commodity.form_url,
        "office": asdict(commodity.office),
        "preamble": commodity.preamble,
        "preamble_blanks": list(commodity.preamble_blanks),
        "closing": commodity.closing,
        "incident_id": incident.incident_id,
        "barangay": incident.barangay or None,
        "place": incident.place(),  # the reports' place detail, for a draft with no barangay
        "drafted_at": manila_time(drafted_at),
        "occurred_at": occurred.isoformat(),
        "first_reported": manila_time(occurred),
        "deadlines": deadline_rows(commodity, occurred),
        "fields": fields,
        "filled_count": sum(f["value"] is not None for f in fields),
        "assured_count": sum(f["mode"] == "assured" for f in fields),
        "loss_bands": commodity.loss_bands,
        "loss_bands_source": commodity.loss_bands_source,
        "sam_label": SAM_LABEL,
        "sam_caveat": commodity.sam_caveat,
        "not_filed": NOT_FILED.format(document=commodity.document),
        "annex": entries,
        "reused_photos": reused,
        "missing_evidence": missing,
        "report_ids": incident.report_ids,
        "reporter_count": incident.reporter_count,
        "single_source": incident.reporter_count == 1,
        "grouping_status": incident.status,
        "pending_report_ids": incident.report_ids if incident.pending else [],
    }
