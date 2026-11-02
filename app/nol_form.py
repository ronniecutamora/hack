"""PCIC Notices of Loss as data: one entry per insured commodity.

Each commodity says once which PCIC form it drafts (fields in the form's own order and
numbering) and the PCIC office it is addressed to (fixed for every Biñan draft), which
deadlines-table rows set its filing clock, which typed report facts make an
incident eligible, the evidence the adjuster needs, and how each field is filled:

- assured: only the assured fisherfolk or farmer can give it (name, address, policy, lot,
  harvest date, underwriter, contact);
- evidence: a deterministic rule over the incident's report facts (`rule`, in app.nol).

Every field is also an input for the officer (app.fillable): its control, a plain hint, whether
it is asked first in Simple mode (`ask`) and whether it is personal. Personal fields (the
assured's name, address, lot and policy numbers, contact number) are typed for the printout
only and never stored; the others are saved as the officer's entries on a new version.

The drafting, deadline and approval code reads these entries and never names a commodity, so
adding one (crops later, once their PCIC deadlines are verified for this project) is one more
entry here plus its tests. Only fisheries is in scope for the hackathon.
"""

import re
from dataclasses import dataclass

from app.deadlines import DEADLINES
from app.fillable import DATE as DATE_CONTROL
from app.fillable import (
    HECTARES,
    LONG,
    PAST_DATE,
    PAST_DATETIME,
    PHONE,
    TEXT,
    Control,
    check_field,
)

MODES = ("assured", "evidence")
RULES = ("location", "cause", "first_reported", "stated_area", "extent")
KIND_PREFIX = "pcic_nl_"
# The SAM 3.1 concepts the pipeline prompts for, and the measurement keys each one yields.
SAM_CONCEPTS = {
    "roof": ("roof_count",),
    "floodwater": ("floodwater_present", "floodwater_fraction"),
}


PRINTED_ONLY = "Printed only, never saved. Type it to print, or the assured writes it on the paper."


@dataclass(frozen=True)
class NolField:
    number: str  # as printed; the letter's own blanks (date, type of policy) use a word
    label: str
    mode: str = "assured"
    rule: str | None = None
    control: Control = TEXT
    ask: bool = False
    personal: bool = False
    hint: str | None = None
    options: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.number

    @property
    def editable(self) -> bool:
        return True


def _personal(number: str, label: str, control: Control = TEXT) -> NolField:
    return NolField(number, label, ask=True, personal=True, hint=PRINTED_ONLY, control=control)


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
    # The preamble with each blank as {field key}, for the printout.
    preamble_form: str
    letter_fields: tuple[NolField, ...]  # the letter's own blanks: its date, the type of policy
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
    # The SAM measurements the annex shows (keys of SAM_CONCEPTS), and what to keep in mind
    # when reading them for this commodity.
    sam_concepts: tuple[str, ...]
    sam_caveat: str
    loss_bands: str
    loss_bands_source: str
    closing: str

    @property
    def preamble_parts(self) -> list[tuple[str, bool]]:
        """The printed preamble as (text, False) and (field key, True) pieces."""
        pieces = re.split(r"\{([^}]+)\}", self.preamble_form)
        return [(piece, i % 2 == 1) for i, piece in enumerate(pieces) if piece]

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
    preamble_form=(
        "This is to file a NOTICE OF LOSS with respect to the {policy_type} with Insurance "
        "Policy No. {10} issued by PCIC, with the following particulars:"
    ),
    letter_fields=(
        NolField(
            "date",
            "Date",
            control=PAST_DATE,
            ask=True,
            hint="The day the notice is signed and filed.",
        ),
        NolField(
            "policy_type",
            "Type of Policy",
            ask=True,
            hint="As on the policy, e.g. Fish Cage Insurance or Milkfish Insurance.",
        ),
    ),
    fields=(
        _personal("1", "Name of Assured Fisherfolk/Fish Grower"),
        _personal("2", "Address of Assured"),
        NolField(
            "3",
            "Location of Fisheries Farm/Property",
            "evidence",
            "location",
            control=LONG,
        ),
        NolField("4", "Nature/cause of loss", "evidence", "cause"),
        NolField(
            "5",
            "Date/time of loss",
            "evidence",
            "first_reported",
            control=PAST_DATETIME,
            ask=True,
            hint="When the loss happened. The reports only show when it was first reported.",
        ),
        NolField(
            "6",
            "Total Area Affected",
            "evidence",
            "stated_area",
            control=HECTARES,
            ask=True,
            hint="In hectares, from the assured or the farm plan; never measured from photos.",
        ),
        _personal("7", "Lot Number/s"),
        NolField(
            "8",
            "Expected date of harvest (if applicable)",
            control=DATE_CONTROL,
            ask=True,
            hint="Leave empty if there was no stock to harvest.",
        ),
        NolField("9", "Extent of Damage", "evidence", "extent", control=LONG),
        _personal("10", "Insurance Policy Number"),
        NolField(
            "11",
            "Underwriter",
            ask=True,
            hint="The PCIC underwriter or agent named on the policy, or the office.",
        ),
        _personal("12", "Contact number", PHONE),
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
    # Roofs are not fisheries evidence; a fish-pen photo would just read "0 roofs".
    sam_concepts=("floodwater",),
    sam_caveat=(
        'SAM 3.1 is prompted with "floodwater", which also covers the lake or pond water '
        "around pens and cages, so the share of water in a photo is not the extent of "
        "flooding or of the loss."
    ),
    loss_bands=(
        "PCIC classifies the loss after verification by at least two adjusters (one from PCIC, "
        "one from the LGU fisheries personnel): total loss 90% and above; partial loss over 10% "
        "and below 90%; no loss 10% and below. This draft does not classify the loss."
    ),
    loss_bands_source="PCIC Fisheries Insurance general information, 2024 V4 (Sept 2024)",
    closing="Very truly yours, / Signature Over Printed Name of the Assured/Claimant",
)

COMMODITIES: dict[str, Commodity] = {FISHERIES.key: FISHERIES}


def all_fields(commodity: Commodity) -> list[NolField]:
    """The letter's blanks, then the numbered fields: the order of the paper form."""
    return [*commodity.letter_fields, *commodity.fields]


def commodity_for_kind(kind: str) -> Commodity | None:
    """The commodity a Draft.kind belongs to, or None for the other outputs."""
    return next((c for c in COMMODITIES.values() if c.kind == kind), None)


def check(commodity: Commodity) -> None:
    numbers = [f.number for f in (*commodity.letter_fields, *commodity.fields)]
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"{commodity.key}: duplicate field numbers")
    for f in (*commodity.letter_fields, *commodity.fields):
        check_field(f, f"{commodity.key} field {f.number}")
        if f.mode not in MODES:
            raise ValueError(f"{commodity.key} field {f.number}: unknown mode {f.mode!r}")
        if (f.mode == "evidence") != (f.rule is not None) or (f.rule and f.rule not in RULES):
            raise ValueError(f"{commodity.key} field {f.number}: evidence needs a known rule")
    keys = {f.key for f in all_fields(commodity)}
    for piece, is_key in commodity.preamble_parts:
        if is_key and piece not in keys:
            raise ValueError(f"{commodity.key}: preamble blank {piece!r} is not a field")
    for key in (commodity.deadline, *commodity.later_deadlines):
        if key not in DEADLINES:
            raise ValueError(f"{commodity.key}: deadline {key!r} is not in the deadlines table")
    for concept in commodity.sam_concepts:
        if concept not in SAM_CONCEPTS:
            raise ValueError(f"{commodity.key}: unknown SAM concept {concept!r}")
    if not (commodity.eligible_livelihood or commodity.eligible_counts):
        raise ValueError(f"{commodity.key}: no eligibility rule")


for _commodity in COMMODITIES.values():
    check(_commodity)
