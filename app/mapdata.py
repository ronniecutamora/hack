"""Officer map features: one marker per active incident, plus flagged pins outside Biñan.

An incident's marker sits at the mean of its reports' GPS points (location pin, else photo GPS)
that fall inside Biñan; an incident without one sits at its barangay's centre and is marked
approximate. A report pinned outside Biñan is shown only when trust signals flagged it, as its own
marker, and the map never pans to it. Properties carry what the incident pages already show
(title, barangay, status, counts, depth); never report text or reporter pseudonyms.
"""

from collections import defaultdict
from statistics import fmean
from typing import Any

from sqlmodel import Session

from app.dashboard import active_incidents, incident_trust
from app.geo import barangay_at, centroid
from app.incidents import observations
from app.models import Incident

STATUS_LABELS = {"open": "Open", "needs_officer": "Needs officer review"}


def _depth(value: str | None) -> str:
    return (value or "unknown").replace("_", " ")


def map_features(session: Session) -> tuple[dict[str, Any], list[Incident]]:
    """A GeoJSON FeatureCollection, and the active incidents with no point and no barangay."""
    points: defaultdict[int, list[tuple[float, float]]] = defaultdict(list)
    outside = []
    for obs in observations(session).values():
        if obs.point is None:
            continue
        if barangay_at(*obs.point) is not None:
            if obs.incident_id is not None:
                points[obs.incident_id].append(obs.point)
        elif obs.flagged:
            outside.append(obs)
    features, unplaced = [], []
    for incident in active_incidents(session):
        mine = points.get(incident.id, [])
        if mine:
            lat, lon, approximate = fmean(p[0] for p in mine), fmean(p[1] for p in mine), False
        elif incident.barangay and (centre := centroid(incident.barangay)):
            (lat, lon), approximate = centre, True
        else:
            unplaced.append(incident)
            continue
        facts = incident.latest_facts
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                "properties": {
                    "kind": "incident",
                    "id": incident.id,
                    "url": f"/incidents/{incident.id}",
                    "title": incident.title,
                    "barangay": incident.barangay,
                    "status": incident.status,
                    "status_label": STATUS_LABELS.get(incident.status, incident.status),
                    "approximate": approximate,
                    "points": len(mine),
                    "report_count": facts.get("report_count", 0),
                    "latest_depth": _depth(facts.get("latest_depth")),
                    "max_depth": _depth(facts.get("max_depth")),
                    **incident_trust(incident),
                },
            }
        )
    for obs in outside:
        url = (
            f"/incidents/{obs.incident_id}#report-{obs.id}"
            if obs.incident_id
            else f"/reports?around={obs.id}#report-{obs.id}"
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(obs.point[1], 6), round(obs.point[0], 6)],
                },
                "properties": {"kind": "outside", "report_id": obs.id, "url": url},
            }
        )
    return {"type": "FeatureCollection", "features": features}, unplaced
