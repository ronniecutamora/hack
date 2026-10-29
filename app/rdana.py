"""Fill the RDANA form (app.rdana_form) from one barangay's report evidence.

Every value is either fixed, given by the officer, written by a deterministic rule over typed
report facts (with the incidents and reports behind it), or the validated agent summary. No
rule turns missing evidence into "No" or "0": a field without evidence stays empty and is shown
as left for the RDANA team. The filled form is plain JSON, stored once and never recomputed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.counting import COUNT_FIELDS, COUNTING_RULE, BarangayEvidence, Count, IncidentEvidence
from app.incidents import distance_m
from app.models import manila_time
from app.rdana_form import (
    FORM_ID,
    FORM_SOURCE,
    FORM_SUBTITLE,
    FORM_TITLE,
    SECTIONS,
    FormField,
)
from app.rdana_summary import SummaryResult

NOT_IN_REPORTS = "— not in reports (for the RDANA team)"
NEED_VALUES = {
    "shelter": "Shelter assistance requested (the reports do not name the item)",
    "food": "Food requested",
    "water": "Water requested",
    "medicine": "Medicine requested",
}
ACCESS_NEEDS = {"transport": "Transport", "clearing": "Debris clearing"}
HAZARD_TYPES = {
    "flood": "Flood",
    "storm_surge": "Storm surge",
    "strong_wind": "Strong wind",
    "fallen_tree": "Fallen trees",
    "landslide": "Landslide",
    "fire": "Fire",
    "other": "Other",
}
WATER_HAZARDS = ("flood", "storm_surge")


@dataclass(frozen=True)
class EventInput:
    """Officer-entered RDANA 1.1 values; each is optional."""

    name: str | None = None
    type: str | None = None
    at: datetime | None = None  # naive UTC


@dataclass(frozen=True)
class RuleContext:
    evidence: BarangayEvidence
    event: EventInput
    drafted_at: datetime  # naive UTC
    summary: SummaryResult


def _incident_ref(incident: IncidentEvidence, report_ids: list[int], **extra) -> dict[str, Any]:
    return {
        "incident_id": incident.incident_id,
        "label": incident.key,
        "report_ids": report_ids,
        "trust": incident.trust(),
        "flagged_report_ids": [r for r in report_ids if r in incident.flagged_report_ids],
        "status": incident.status,
        **extra,
    }


def _filled(value: str, source: str, evidence=(), note: str | None = None) -> dict[str, Any]:
    return {"value": value, "source": source, "evidence": list(evidence), "note": note}


def _listing(evidence: BarangayEvidence, list_field: str, values: tuple[str, ...]):
    """Incidents with reports listing any of `values`, and which values were found."""
    refs, found = [], []
    for incident in evidence.incidents:
        ids = sorted({r for v in values for r in incident.reports_listing(list_field, v)})
        if ids:
            refs.append(_incident_ref(incident, ids))
        found += [v for v in values if incident.reports_listing(list_field, v)]
    return refs, list(dict.fromkeys(v for v in values if v in found))


def pending_text(count: Count, unit: str = "") -> str:
    """The pending bucket in words, e.g. "pending grouping review: 5 (reports 4, 7)"."""
    ids = count.pending_report_ids
    return (
        f"pending grouping review: {count.pending_value:g}{unit} "
        f"(report{'s' if len(ids) != 1 else ''} {', '.join(map(str, ids))})"
    )


def figure_text(count: Count, unit: str = "") -> str:
    """A count for prose: the confirmed figure and, beside it, never added, the pending one."""
    if not count.pending:
        return f"{count.value:g}{unit}"
    if not count.confirmed:
        return pending_text(count, unit)
    return f"{count.value:g}{unit}; {pending_text(count, unit)}"


def count_field(count: Count | None) -> dict[str, Any] | None:
    """A count field: the confirmed figure, its evidence and the pending bucket beside it.

    With only pending evidence the value stays empty: 0 would read as "no damage".
    """
    if count is None:
        return None
    refs = [
        _incident_ref(
            c.incident,
            list(c.stating_report_ids),
            value=c.value,
            rule=c.rule,
            source_report_id=c.source_report_id,
            single_source=c.single_source,
            pending=pending,
        )
        for pending, contributions in ((False, count.contributions), (True, count.pending))
        for c in contributions
    ]
    total = count.value
    notes = []
    if count.confirmed:
        notes.append(
            f"{count.single_source_value:g} of {total:g} stated by only one reporter; "
            f"{count.flagged_value:g} of {total:g} from flagged reports."
        )
    if count.pending:
        notes.append(f"Not added: {pending_text(count)}.")
    value = f"{total:g}" if count.confirmed else None
    return _filled(value, "reports" if count.confirmed else None, refs, " ".join(notes)) | {
        "count": {
            "value": total if count.confirmed else None,
            "single_source": count.single_source_value,
            "flagged": count.flagged_value,
            "pending": count.pending_value,
            "pending_report_ids": count.pending_report_ids,
        }
    }


def _event_name(ctx: RuleContext):
    return _filled(ctx.event.name, "officer") if ctx.event.name else None


def _event_type(ctx: RuleContext):
    refs, found = _listing(ctx.evidence, "hazards", tuple(HAZARD_TYPES))
    reported = ", ".join(HAZARD_TYPES[h] for h in found)
    if ctx.event.type:
        note = f"Reports mention: {reported}." if reported else None
        return _filled(ctx.event.type, "officer", refs, note)
    return _filled(reported, "reports", refs, "Hazards named in the reports.") if found else None


def _event_at(ctx: RuleContext):
    first = ctx.evidence.members[0]
    incident = next(i for i in ctx.evidence.incidents if first in i.members)
    refs = [_incident_ref(incident, [first.id])]
    if ctx.event.at is not None:
        note = f"Earliest report: {manila_time(first.at)}."
        filled = _filled(manila_time(ctx.event.at), "officer", refs, note)
        if ctx.event.at > first.at:
            filled["warning"] = "Later than the earliest report: check the date."
        return filled
    note = "Time of the earliest report; the event itself may be earlier."
    return _filled(manila_time(first.at), "reports", refs, note)


def _barangay(ctx: RuleContext):
    refs = [_incident_ref(i, i.report_ids) for i in ctx.evidence.incidents]
    return _filled(ctx.evidence.barangay, "reports", refs)


def _places(ctx: RuleContext):
    refs, places = [], []
    for incident in ctx.evidence.incidents:
        if place := incident.place():
            refs.append(_incident_ref(incident, incident.report_ids))
            places.append(place)
    note = "Place details as reported (names of people removed)."
    return _filled("; ".join(dict.fromkeys(places)), "reports", refs, note) if places else None


def _gps(ctx: RuleContext):
    pinned = [m for m in ctx.evidence.members if m.point is not None]
    if not pinned:
        return None
    lat = sum(m.point[0] for m in pinned) / len(pinned)
    lon = sum(m.point[1] for m in pinned) / len(pinned)
    spread = max(distance_m((lat, lon), m.point) for m in pinned)
    refs = [
        _incident_ref(i, ids)
        for i in ctx.evidence.incidents
        if (ids := [m.id for m in i.members if m.point is not None])
    ]
    note = (
        f"Mean of {len(pinned)} report location{'s' if len(pinned) != 1 else ''} "
        f"(pin or photo GPS); farthest is {spread:.0f} m from it."
    )
    return _filled(f"{lat:.5f}, {lon:.5f}", "reports", refs, note)


def _drafted_at(ctx: RuleContext):
    return _filled(manila_time(ctx.drafted_at), "rule", note="When this draft was generated.")


def _summary(ctx: RuleContext):
    summary = ctx.summary
    cited = set(summary.cited_report_ids)
    refs = [
        _incident_ref(i, ids)
        for i in ctx.evidence.incidents
        if (ids := [r for r in i.report_ids if r in cited])
    ]
    source = "rule" if summary.fallback else "agent"
    if summary.fallback:
        note = f"Deterministic summary. {summary.fallback_reason or ''}".strip()
    else:
        note = (
            f"Drafted by {summary.model}; every number checked against the computed count it "
            "describes, and every citation against the reports."
        )
    return _filled(summary.text, source, refs, note)


def _roads(ctx: RuleContext):
    refs, found = _listing(ctx.evidence, "infrastructure", ("road", "bridge"))
    if not found:
        return None
    return _filled("Yes", "reports", refs, f"Reported damaged or impassable: {', '.join(found)}.")


def _access_need(ctx: RuleContext):
    refs, found = _listing(ctx.evidence, "needs", tuple(ACCESS_NEEDS))
    return _filled(", ".join(ACCESS_NEEDS[n] for n in found), "reports", refs) if found else None


def _power(ctx: RuleContext):
    refs, found = _listing(ctx.evidence, "infrastructure", ("power",))
    if not found:
        return None
    note = "Evidence note only: the reports do not say which of the listed damages it is."
    return _filled("Power outage or damage reported", "reports", refs, note)


def _srr_needed(ctx: RuleContext):
    refs, found = _listing(ctx.evidence, "needs", ("rescue",))
    return _filled("Yes", "reports", refs, "Rescue requested in the reports.") if found else None


def _srr_type(ctx: RuleContext):
    """Water SRR only where rescue was asked for in an incident with flood or storm surge."""
    refs = []
    for incident in ctx.evidence.incidents:
        rescue = incident.reports_listing("needs", "rescue")
        water = sorted({r for h in WATER_HAZARDS for r in incident.reports_listing("hazards", h)})
        if rescue and water:
            refs.append(_incident_ref(incident, sorted(set(rescue) | set(water))))
    note = "Rescue requested in an incident with flood or storm surge."
    return _filled("Water", "reports", refs, note) if refs else None


def _need(name: str) -> Callable[[RuleContext], dict[str, Any] | None]:
    def rule(ctx: RuleContext):
        refs, found = _listing(ctx.evidence, "needs", (name,))
        return _filled(NEED_VALUES[name], "reports", refs) if found else None

    return rule


def _count(name: str) -> Callable[[RuleContext], dict[str, Any] | None]:
    return lambda ctx: count_field(ctx.evidence.count(name))


def _livelihood(ctx: RuleContext):
    """An evidence note on livelihood losses; it does not claim the main livelihood."""
    parts, refs = [], []
    pens = ctx.evidence.count("fish_cages_or_pens_damaged")
    if pens is not None:
        parts.append(f"fish cages or pens damaged: {figure_text(pens)}")
        refs += count_field(pens)["evidence"]
    crops: dict[str, list[int]] = {}
    for incident in ctx.evidence.incidents:
        ids = []
        for member in incident.members:
            crop = incident.facts(member).get("crop")
            if crop not in (None, "none"):
                crops.setdefault(crop, []).append(member.id)
                ids.append(member.id)
        if ids:
            refs.append(_incident_ref(incident, ids))
    hectares = ctx.evidence.count("crop_area_hectares")
    if crops:
        crop_text = "crops damaged: " + ", ".join(crops)
        if hectares is not None:
            crop_text += (
                f" ({hectares.value:g} ha reported)"
                if not hectares.pending
                else f"; area: {figure_text(hectares, ' ha')}"
            )
        parts.append(crop_text)
    if not parts:
        return None
    note = "Evidence note on livelihood losses, not a survey of the main livelihood."
    return _filled("; ".join(parts), "reports", refs, note)


RULES: dict[str, Callable[[RuleContext], dict[str, Any] | None]] = {
    "event_name": _event_name,
    "event_type": _event_type,
    "event_at": _event_at,
    "barangay": _barangay,
    "places": _places,
    "gps": _gps,
    "drafted_at": _drafted_at,
    "summary": _summary,
    "roads": _roads,
    "access_need": _access_need,
    "power": _power,
    "srr_needed": _srr_needed,
    "srr_type": _srr_type,
    "livelihood": _livelihood,
    **{f"need:{n}": _need(n) for n in NEED_VALUES},
}


def _rule(name: str) -> Callable[[RuleContext], dict[str, Any] | None]:
    if name.startswith("count:"):
        return _count(name.removeprefix("count:"))
    return RULES[name]


def fill_field(form_field: FormField, ctx: RuleContext) -> dict[str, Any]:
    filled = None
    if form_field.mode == "fixed":
        filled = _filled(form_field.value, "fixed")
    elif form_field.rule:
        filled = _rule(form_field.rule)(ctx)
    return {
        "key": form_field.key,
        "number": form_field.number,
        "sic": form_field.sic,
        "label": form_field.label,
        "mode": form_field.mode,
        "heading": form_field.heading,
        "required": form_field.required,
        "options": list(form_field.options),
        **(filled or {"value": None, "source": None, "evidence": [], "note": None}),
    }


def flood_support(evidence: BarangayEvidence) -> list[dict[str, Any]]:
    """1.4 support data: latest and maximum flood depth per incident."""
    rows = []
    for incident in evidence.incidents:
        depth = incident.depth()
        if depth["max"] == "unknown" and depth["latest"] == "unknown":
            continue
        rows.append(_incident_ref(incident, incident.report_ids, place=incident.place(), **depth))
    return rows


def fill_rdana(
    evidence: BarangayEvidence, event: EventInput, drafted_at: datetime, summary: SummaryResult
) -> dict[str, Any]:
    """The complete filled form as JSON, in official order."""
    ctx = RuleContext(evidence, event, drafted_at, summary)
    sections = [
        {
            "number": section.number,
            "title": section.title,
            "note": section.note,
            "fields": [fill_field(f, ctx) for f in section.fields],
        }
        for section in SECTIONS
    ]
    fields = [f for s in sections for f in s["fields"] if not f["heading"]]
    return {
        "form": FORM_ID,
        "title": FORM_TITLE,
        "subtitle": FORM_SUBTITLE,
        "source": FORM_SOURCE,
        "barangay": evidence.barangay,
        "drafted_at": manila_time(drafted_at),
        "sections": sections,
        "flood_depths": flood_support(evidence),
        "counting_rule": COUNTING_RULE,
        "not_in_reports": NOT_IN_REPORTS,
        "report_ids": evidence.report_ids,
        "incident_count": len(evidence.incidents),
        "unassessed_report_ids": evidence.unassessed_report_ids,
        "pending_report_ids": evidence.pending_report_ids(),
        "filled_count": sum(f["value"] is not None for f in fields),
        "field_count": len(fields),
        "summary": {
            "model": summary.model,
            "fallback": summary.fallback,
            "fallback_reason": summary.fallback_reason,
            "cited_report_ids": summary.cited_report_ids,
        },
    }


def summary_counts(evidence: BarangayEvidence) -> dict[str, int | float]:
    """The confirmed counts given to the summary agent; pending figures are never totals."""
    return {name: c.value for name in COUNT_FIELDS if (c := evidence.count(name)) and c.confirmed}
