"""PCIC Notices of Loss as data: one entry per insured commodity.

Each commodity says once which PCIC form it drafts (fields in the form's own order and
numbering) and the PCIC office it is addressed to (fixed for every Biñan draft), which
deadlines-table rows set its filing clock, which typed report facts make an
incident eligible, the evidence the adjuster needs, and how each field is filled:

- assured: only the assured fisherfolk or farmer can give it (name, address, policy, lot,
  harvest date, underwriter, contact); Ulat stores no names, addresses or phone numbers, so
  these stay "to be filled by the assured";
- evidence: a deterministic rule over the incident's report facts (`rule`, in app.nol).

The drafting, deadline and approval code reads these entries and never names a commodity, so
adding one (crops later, once their PCIC deadlines are verified for this project) is one more
entry here plus its tests. Only fisheries is in scope for the hackathon.
"""

from dataclasses import dataclass

from app.deadlines import DEADLINES

MODES = ("assured", "evidence")
RULES = ("location", "cause", "first_reported", "stated_area", "extent")
KIND_PREFIX = "pcic_nl_"
ASSURED = "— to be filled by the assured"


@dataclass(frozen=True)
class NolField:
    number: str
    label: str
    mode: str = "assured"
    rule: str | None = None


@dataclass(frozen=True)
class Office:
    """The PCIC office a Notice of Loss is addressed to, with where that was read."""

    name: str
    addressee: str
    address: str
    email: str
    phone: str
    source: str


@dataclass(frozen=True)
class Commodity:
    key: str
    name: str  # for headings: "fisheries"
    title: str  # as printed
    form_id: str
    revision: str
    form_source: str
    form_url: str
    office: Office
    preamble: str  # as printed; the blanks are the assured's
    preamble_blanks: tuple[str, ...]
    fields: tuple[NolField, ...]
    deadline: str  # the Notice of Loss row of app/data/deadlines.json
    later_deadlines: tuple[str, ...]  # rows that run from the same occurrence (the claim)
    # An incident is eligible when a report states one of these livelihood damages, or a
    # positive count in one of these fields.
    eligible_livelihood: tuple[str, ...]
    eligible_counts: tuple[str, ...]
    # Field 9 (extent) reads these; the area field reads `area_facts` and nothing else.
    extent_livelihood: tuple[str, ...]
    extent_counts: tuple[str, ...]
    area_facts: tuple[str, ...]
    area_unit: str
    required_evidence: tuple[str, ...]  # "photo": at least one photo of the damage
    loss_bands: str
    loss_bands_source: str
    closing: str

    @property
    def kind(self) -> str:
        """The Draft.kind of this commodity's Notices of Loss."""
        return KIND_PREFIX + self.key


PCIC_REGION_IV = Office(
    name="PCIC Regional Office No. IV",
    addressee="THE REGIONAL MANAGER",
    address="2/F Rizal Commercial Center, J.P. Rizal St. corner MH Del Pilar, Calamba City, Laguna",
    email="ro4@pcic.gov.ph",
    phone="(049) 530-0935 / (049) 536-2129",
    source="PCIC Fisheries Insurance general information, 2024 V4 (Sept 2024)",
)

FISHERIES = Commodity(
    key="fisheries",
    name="fisheries",
    title="NOTICE OF LOSS (Fisheries Insurance)",
    form_id="FIP CAS Form No. 1",
    revision="REV. 2022/MAY",
    form_source="PCIC, FIP CAS Form No. 1, Notice of Loss (Fisheries Insurance), REV. 2022/MAY",
    form_url="https://pcic.gov.ph/wp-content/uploads/2024/02/FIP-CAS-01-NL.pdf",
    office=PCIC_REGION_IV,
    preamble=(
        "This is to file a NOTICE OF LOSS with respect to the (Type of Policy i.e. Milkfish "
        "Insurance, Fish Cage Insurance) with Insurance Policy No. ______ issued by PCIC, with "
        "the following particulars:"
    ),
    preamble_blanks=("type of policy", "insurance policy number"),
    fields=(
        NolField("1", "Name of Assured Fisherfolk/Fish Grower"),
        NolField("2", "Address of Assured"),
        NolField("3", "Location of Fisheries Farm/Property", "evidence", "location"),
        NolField("4", "Nature/cause of loss", "evidence", "cause"),
        NolField("5", "Date/time of loss", "evidence", "first_reported"),
        NolField("6", "Total Area Affected", "evidence", "stated_area"),
        NolField("7", "Lot Number/s"),
        NolField("8", "Expected date of harvest (if applicable)"),
        NolField("9", "Extent of Damage", "evidence", "extent"),
        NolField("10", "Insurance Policy Number"),
        NolField("11", "Underwriter"),
        NolField("12", "Contact number"),
    ),
    deadline="pcic_fisheries_nl",
    later_deadlines=("pcic_fisheries_ci",),
    eligible_livelihood=("fish_pens_or_cages", "fishponds"),
    eligible_counts=("fish_cages_or_pens_damaged",),
    extent_livelihood=("fish_pens_or_cages", "fishponds", "fishing_gear", "fishing_boats"),
    extent_counts=("fish_cages_or_pens_damaged",),
    area_facts=("fishery_area_hectares",),
    area_unit="ha",
    required_evidence=("photo",),
    loss_bands=(
        "PCIC classifies the loss after verification by at least two adjusters (one from PCIC, "
        "one from the LGU fisheries personnel): total loss 90% and above; partial loss over 10% "
        "and below 90%; no loss 10% and below. This draft does not classify the loss."
    ),
    loss_bands_source="PCIC Fisheries Insurance general information, 2024 V4 (Sept 2024)",
    closing="Very truly yours, / Signature Over Printed Name of the Assured/Claimant",
)

COMMODITIES: dict[str, Commodity] = {FISHERIES.key: FISHERIES}


def commodity_for_kind(kind: str) -> Commodity | None:
    """The commodity a Draft.kind belongs to, or None for the other outputs."""
    return next((c for c in COMMODITIES.values() if c.kind == kind), None)


def check(commodity: Commodity) -> None:
    numbers = [f.number for f in commodity.fields]
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"{commodity.key}: duplicate field numbers")
    for f in commodity.fields:
        if f.mode not in MODES:
            raise ValueError(f"{commodity.key} field {f.number}: unknown mode {f.mode!r}")
        if (f.mode == "evidence") != (f.rule is not None) or (f.rule and f.rule not in RULES):
            raise ValueError(f"{commodity.key} field {f.number}: evidence needs a known rule")
    for key in (commodity.deadline, *commodity.later_deadlines):
        if key not in DEADLINES:
            raise ValueError(f"{commodity.key}: deadline {key!r} is not in the deadlines table")
    if not (commodity.eligible_livelihood or commodity.eligible_counts):
        raise ValueError(f"{commodity.key}: no eligibility rule")


for _commodity in COMMODITIES.values():
    check(_commodity)
