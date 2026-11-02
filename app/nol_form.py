"""PCIC claim forms as data: one entry per insured commodity.

Each commodity says once which PCIC form it drafts (fields in the form's own order and
numbering) and what that form is called (fisheries: the Notice of Loss, FIP CAS Form No. 1;
rice: the Claim for Indemnity, RC-CAS-01, since rice has no separate Notice of Loss), the PCIC
office it is addressed to (fixed for every Biñan draft), which deadlines-table rows set its
filing clock, which typed report facts make an incident eligible, the evidence the adjuster
needs, how its paper is laid out, and how each field is filled:

- assured: only the assured fisherfolk or farmer can give it (name, address, policy, lot,
  harvest date, underwriter, contact);
- evidence: a deterministic rule over the incident's report facts (`rule`, in app.nol).

Every field is also an input for the officer (app.fillable): its control, a plain hint, whether
it is asked first in Simple mode (`ask`) and whether it is personal. Personal fields (the
assured's name, address, lot and policy numbers, contact number) are typed for the printout
only and never stored; the others are saved as the officer's entries on a new version.

The drafting, deadline and approval code reads these entries and never names a commodity, so
adding one (corn later) is one more entry here plus its tests. Which commodities an office
handles is data too (app.pcic_lines), each with its source.
"""

import re
from dataclasses import dataclass, replace

from app.deadlines import DEADLINES
from app.fillable import (
    CHOICE,
    HECTARES,
    LABEL_SOURCE,
    LONG,
    PAST_DATE,
    PAST_DATETIME,
    PERCENT,
    PHONE,
    TEXT,
    Control,
    check_field,
)
from app.fillable import DATE as DATE_CONTROL

MODES = ("assured", "evidence")
RULES = (
    "location",
    "cause",
    "first_reported",
    "stated_area",
    "extent",
    "insured",
    "stage",
    "stated_percent",
)
KIND_PREFIX = "pcic_nl_"  # Draft.kind of a Notice of Loss; a commodity may use its own prefix
# The SAM 3.1 concepts the pipeline prompts for, and the measurement keys each one yields.
SAM_CONCEPTS = {
    "roof": ("roof_count",),
    "floodwater": ("floodwater_present", "floodwater_fraction"),
}


# Where each field's "?" help comes from: the form's own label, or the PCIC document page
# that explains it.
BROCHURE = "PCIC Fisheries Insurance general information, 2024 V4 (Sept 2024)"
NL_LABEL = f"{LABEL_SOURCE} (PCIC FIP CAS Form No. 1, Notice of Loss, Rev. 2022/May)"
APPLICATION = "PCIC FIP-UPI-01, Application for Fisheries Insurance, Rev. 2025/Sep"
FARM_PLAN = "PCIC FIP UPI Form No. 2, Fisheries Farm Plan and Budget"
RICE_BROCHURE = "PCIC Rice Crop Insurance general information, revised April 2024 (V12)"
CI_LABEL = f"{LABEL_SOURCE} (PCIC RC-CAS-01, Claim for Indemnity, Rev. 2017/FEB)"
CROP_APPLICATION = "PCIC RC-UPI-01, Application for Crop Insurance, Rev. 2025/Sep"


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
    help: str | None = None  # the "?" text: what to enter, with an example
    help_source: str | None = None
    # The words the paper prints at the blank, under its line, where they are not the label:
    # the type-of-policy blank prints "(Type of Policy i.e. ...)".
    paper_label: str | None = None
    # A bilingual form (RC-CAS-01) numbers each section from 1 and prints the Filipino label in
    # italics after the English one, and a unit after some blanks ("ha. (ektarya)").
    section: str | None = None
    label_fil: str | None = None
    paper_unit: tuple[str, str] | None = None  # (unit, its Filipino gloss)

    @property
    def key(self) -> str:
        return f"{self.section}.{self.number}" if self.section else self.number

    @property
    def editable(self) -> bool:
        return True


def _personal(
    number: str,
    label: str,
    help: str,
    help_source: str,
    control: Control = TEXT,
    **paper: str,
) -> NolField:
    return NolField(
        number,
        label,
        ask=True,
        personal=True,
        control=control,
        help=help,
        help_source=help_source,
        **paper,
    )


@dataclass(frozen=True)
class Office:
    """The PCIC office a Notice of Loss is addressed to, with where that was read."""

    name: str
    number: str  # the form's "Regional Office No. ____"
    addressee: str
    address: str
    email: str
    phone: str
    source: str


@dataclass(frozen=True)
class PaperSection:
    """A numbered section of a bilingual claim form: "I. BASIC INFORMATION (MGA ...)"."""

    numeral: str  # as printed, with its period: "I."
    title: str
    title_fil: str  # printed in italics after the title


@dataclass(frozen=True)
class ClaimPaper:
    """How a bilingual claim form (RC-CAS-01) prints, beyond its fields: every string as the
    paper has it. The fields print under their section (NolField.section)."""

    title_fil: str  # "(PAGHAHABOL BAYAD)" under the title
    to: tuple[str, ...]  # the lines after "TO :"
    # The opening lines, each as (text, italic) pieces: one line mixes italic and upright.
    request: tuple[tuple[tuple[str, bool], ...], ...]
    sections: tuple[PaperSection, ...]
    sketch: PaperSection  # the location sketch plan, a table written by hand
    sketch_note: str  # printed in italics under the sketch title
    sketch_lots: int
    sketch_rows: tuple[tuple[str, str], ...]  # (direction, Filipino) rows of the table
    short_lines: tuple[str, ...]  # field keys whose rule stops short of the right margin
    write_in: tuple[str, ...]  # options printed with a blank to write on: "( ) Others: ____"
    thanks: str
    closing: str
    signature: str
    signature_fil: str


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
    # What the form is called on screen (Technical mode and headings), and in Simple mode.
    document: str = "Notice of Loss"
    plain: str = "Notice of Loss"
    claimant: str = "assured"  # who files it: "the fisherfolk must file it"
    claimants: str = "assured"  # who files them, in lists: "fisherfolk", "rice farmers"
    documents: str = "Notices of Loss"  # the document, plural
    # The "What's this?" entry for the form: its key, term and text ({window} is the deadline).
    glossary: tuple[str, str, str] | None = None
    kind_prefix: str = KIND_PREFIX
    # Also eligible: a report whose typed fact equals a value (("crop", "rice"),).
    eligible_facts: tuple[tuple[str, str], ...] = ()
    eligible_words: str = ""  # how Drafts says the eligible damage; default: the livelihoods
    percent_facts: tuple[str, ...] = ()  # the "stated_percent" rule reads these
    stage_fact: str | None = None  # the "stage" rule reads it
    stage_labels: tuple[tuple[str, str], ...] = ()  # (fact value, how the form says it)
    cause_detail: tuple[tuple[str, str], ...] = ()  # (listed fact value, words after the cause)
    cause_detail_fact: str | None = None
    insured_option: str | None = None  # the "insured" rule ticks this option
    signer: str = "1"  # the personal field whose name prints over the signature line
    claim_paper: ClaimPaper | None = None  # a bilingual claim form; None: the letter layout
    # Paper blanks written by hand, said on screen ("Section III, the sketch ...").
    by_hand: str | None = None

    @property
    def preamble_parts(self) -> list[tuple[str, bool]]:
        """The printed preamble as (text, False) and (field key, True) pieces."""
        pieces = re.split(r"\{([^}]+)\}", self.preamble_form)
        return [(piece, i % 2 == 1) for i, piece in enumerate(pieces) if piece]

    @property
    def kind(self) -> str:
        """The Draft.kind of this commodity's claim drafts."""
        return self.kind_prefix + self.key

    @property
    def plain_title(self) -> str:
        """Simple mode's name with the commodity unless it says it: "Notice of Loss
        (fisheries)", "rice claim"."""
        return self.plain if self.name in self.plain else f"{self.plain} ({self.name})"

    @property
    def eligibility(self) -> str:
        """What a report must state damage to, in words: "fish pens or cages, or fishponds"."""
        words = [w.replace("_", " ") for w in self.eligible_livelihood]
        return self.eligible_words or ", or ".join(words)


PCIC_REGION_IV = Office(
    name="PCIC Regional Office No. IV",
    number="IV",
    addressee="THE REGIONAL MANAGER",
    address="2/F Rizal Commercial Center, J.P. Rizal St. corner MH Del Pilar, Calamba City, Laguna",
    email="ro4@pcic.gov.ph",
    phone="(049) 530-0935 / (049) 536-2129",
    source=BROCHURE,
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
            help=(
                "The day the assured signs this notice. Example: 2026-10-25. PCIC (Philippine "
                "Crop Insurance Corporation) needs it soon after the loss and before harvest: "
                "the deadline is on Drafts."
            ),
            help_source=f"{BROCHURE}, p. 1: Notice of Loss",
            paper_label="Date",
        ),
        NolField(
            "policy_type",
            "Type of Policy",
            ask=True,
            hint="As on the policy, e.g. Fish Cage Insurance or Milkfish Insurance.",
            help=(
                "The insurance named on the assured's PCIC policy. Example: Fish Cage "
                "Insurance or Milkfish Insurance, the two the form itself gives."
            ),
            help_source=NL_LABEL,
            paper_label="(Type of Policy i.e. Milkfish Insurance, Fish Cage Insurance)",
        ),
    ),
    fields=(
        _personal(
            "1",
            "Name of Assured Fisherfolk/Fish Grower",
            "The assured is the fisherfolk or fish grower the policy insures. Write their full "
            "name as on the policy. Example: Juan dela Cruz.",
            f"{BROCHURE}, p. 1: Object of insurance",
        ),
        _personal(
            "2",
            "Address of Assured",
            "Where the assured lives: house number and street or sitio, barangay, city, "
            "province. Example: Purok 2, Malaban, Biñan, Laguna.",
            f"{APPLICATION}, A.3 Address",
        ),
        NolField(
            "3",
            "Location of Fisheries Farm/Property",
            "evidence",
            "location",
            control=LONG,
            help=(
                "Where the fish cage, pen or pond is. Example: Fish pen off Barangay Malaban, "
                "Laguna de Bay. Ulat starts with the place the reports give."
            ),
            help_source=NL_LABEL,
        ),
        NolField(
            "4",
            "Nature/cause of loss",
            "evidence",
            "cause",
            help=(
                "What caused the loss, in a few words. Example: Typhoon winds and flood tore "
                "the pen nets. Losses from disease, pests, theft or an oil spill are not "
                "covered."
            ),
            help_source=f"{BROCHURE}, p. 1: Excluded risks",
        ),
        NolField(
            "5",
            "Date/time of loss",
            "evidence",
            "first_reported",
            control=PAST_DATETIME,
            ask=True,
            hint="When the loss happened. The reports only show when it was first reported.",
            help=(
                "When the loss happened, Philippine time. Example: 2026-10-24 23:30. The "
                "reports only show when it was first reported, which can be later."
            ),
            help_source=NL_LABEL,
        ),
        NolField(
            "6",
            "Total Area Affected",
            "evidence",
            "stated_area",
            control=HECTARES,
            ask=True,
            hint="In hectares, from the assured or the farm plan; never measured from photos.",
            help=(
                "The area of the cage, pen or pond hit, in hectares. Example: 0.5. Take it "
                "from the assured or the insured area in the farm plan; Ulat never measures "
                "it from photos."
            ),
            help_source=f"{FARM_PLAN}: Area Insured (has.)",
        ),
        _personal(
            "7",
            "Lot Number/s",
            "The lot number or numbers of the farm, as on the assured's papers. Example: Lot 12.",
            NL_LABEL,
        ),
        NolField(
            "8",
            "Expected date of harvest (if applicable)",
            control=DATE_CONTROL,
            ask=True,
            hint="Leave empty if there was no stock to harvest.",
            help=(
                "When the stock was due for harvest, as in the farm plan. Example: 2026-12-15. "
                "A loss after that date, or after harvest, is not covered."
            ),
            help_source=f"{BROCHURE}, p. 1: Excluded risks",
        ),
        NolField(
            "9",
            "Extent of Damage",
            "evidence",
            "extent",
            control=LONG,
            help=(
                "How much was lost, in plain words. Example: Two of three cages torn; most "
                "tilapia escaped. Adjusters from PCIC and the LGU check the loss before PCIC "
                "pays (the indemnity)."
            ),
            help_source=f"{BROCHURE}, p. 1: Adjustment and settlement of claims",
        ),
        _personal(
            "10",
            "Insurance Policy Number",
            "The number on the assured's PCIC insurance policy. Copy it exactly from the policy.",
            NL_LABEL,
        ),
        NolField(
            "11",
            "Underwriter",
            ask=True,
            hint="The PCIC underwriter or agent named on the policy, or the office.",
            help=(
                "Who issued the policy: the PCIC office or the agent named on it. Example: "
                "PCIC Regional Office IV."
            ),
            help_source=NL_LABEL,
        ),
        _personal(
            "12",
            "Contact number",
            "A phone number where PCIC can reach the assured. Example: 0917 123 4567.",
            NL_LABEL,
            PHONE,
        ),
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
    loss_bands_source=BROCHURE,
    # As printed, one line per " / ": the closing, then the caption under the signature line.
    closing="Very truly yours, / Signature Over Printed Name / of the Assured/Claimant",
    claimant="fisherfolk",
    claimants="fisherfolk",
    glossary=(
        "nol",
        "Notice of Loss",
        "The form an insured fisherfolk sends to PCIC (the Philippine Crop Insurance "
        "Corporation) to say their fish cage or pen was damaged. It is due within {window} of "
        "the damage, so Ulat counts down the time. Ulat drafts it with the photos as evidence; "
        "the fisherfolk files it.",
    ),
)

# Rice has no Notice of Loss: the farmer's first filing is the Claim for Indemnity, RC-CAS-01
# (PCIC Citizen's Charter 2026, rice and corn checklist; rice brochure, "Claim for Indemnity").
# Addressed to the Claims and Adjustment Division (CAD) of the PCIC Regional Office.
PCIC_REGION_IV_CAD = replace(
    PCIC_REGION_IV, addressee="The Chief CAD", source=f"{RICE_BROCHURE}, p. 2: Regional Offices"
)
RICE_STAGES = (
    ("seedling", "Seedling"),
    ("vegetative", "Vegetative (tillering)"),
    ("reproductive", "Reproductive (booting to flowering)"),
    ("ripening", "Ripening (milk to mature grain)"),
)
RICE = Commodity(
    key="rice",
    name="rice",
    title="CLAIM FOR INDEMNITY",
    form_id="RC-CAS-01",
    revision="Rev. 2017/FEB",
    form_source="PCIC, RC-CAS-01, Claim for Indemnity (rice and corn), Rev. 2017/FEB",
    form_url="https://pcic.gov.ph/wp-content/uploads/2024/02/RC-CAS-01_Claim-for-Indemnity.pdf",
    office=PCIC_REGION_IV_CAD,
    preamble=(
        "Please send your Team of Adjusters to assess damage of my insured crop. Hereunder are "
        "the basic information needed by your office:"
    ),
    preamble_blanks=(),
    preamble_form="",
    letter_fields=(
        NolField(
            "date",
            "Date",
            control=PAST_DATE,
            ask=True,
            hint="The day the farmer signs the claim.",
            help=(
                "The day the farmer signs this claim. Example: 2026-10-25. It must reach PCIC "
                "(Philippine Crop Insurance Corporation) soon after the loss and before the "
                "harvest: the deadline is on Drafts."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Claim for Indemnity",
            paper_label="DATE",
            label_fil="PETSA",
        ),
    ),
    fields=(
        _personal(
            "1",
            "Name of Farmer-Assured",
            "The farmer the policy insures (the assured). Write their full name as on the "
            "Certificate of Insurance Cover (CIC). Example: Juan dela Cruz.",
            f"{RICE_BROCHURE}, p. 1: Object of insurance",
            section="I",
            label_fil="Pangalan ng Magsasaka",
        ),
        _personal(
            "2",
            "Address",
            "Where the farmer lives: house number and street or sitio, barangay, town, "
            "province. Example: Purok 2, Barangay San Isidro, Laguna.",
            f"{CROP_APPLICATION}, A.3 Address",
            section="I",
            label_fil="Tirahan",
        ),
        _personal(
            "3",
            "Cell Phone Number",
            "A phone number where PCIC can reach the farmer. Example: 0917 123 4567.",
            CI_LABEL,
            PHONE,
            section="I",
            label_fil="Numero ng Telepono",
        ),
        NolField(
            "4",
            "Location of Farm",
            "evidence",
            "location",
            control=LONG,
            section="I",
            label_fil="Lugar ng Saka",
            help=(
                "Where the insured rice field is: sitio, barangay, town. Example: Sitio Ilaya, "
                "near the irrigation canal. Ulat starts with the place the reports give."
            ),
            help_source=f"{CROP_APPLICATION}, B.1 Farm Location/LSP",
        ),
        NolField(
            "5",
            "Insured Crops",
            "evidence",
            "insured",
            control=CHOICE,
            options=("Palay", "Corn"),
            section="I",
            label_fil="Pananim na Ipinaseguro",
            help=(
                "The crop the policy insures: Palay (rice) or Corn. Ulat ticks Palay because "
                "the reports are about rice; check it against the CIC."
            ),
            help_source=CI_LABEL,
        ),
        NolField(
            "6",
            "Area Insured",
            control=HECTARES,
            ask=True,
            section="I",
            label_fil="Luwang/Sukat ng Bukid na Ipinaseguro",
            paper_unit=("ha.", "ektarya"),
            hint="In hectares, as on the CIC.",
            help=(
                "The insured area in hectares, as on the CIC or the application. Example: 1.5. "
                "The amount of cover is set per hectare."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Amount of cover",
        ),
        NolField(
            "7",
            "Variety Planted",
            ask=True,
            section="I",
            label_fil="Binhing Itinanim",
            hint="The rice variety in the field, e.g. NSIC Rc 222.",
            help=(
                "The rice variety planted. Example: NSIC Rc 222. Insurable varieties are those "
                "accredited by the National Seed Industry Council or endorsed by the "
                "Municipal Agriculturist."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Insurable rice varieties",
        ),
        NolField(
            "8",
            "Actual Date of Planting",
            control=PAST_DATE,
            ask=True,
            section="I",
            label_fil="Aktwal na Petsa ng Pagkakatanim",
            hint="The day the field was direct seeded or transplanted.",
            help=(
                "The day the rice was direct seeded or transplanted. Example: 2026-08-10. Cover "
                "starts from that day (or the CIC date, whichever is later)."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Period of cover",
        ),
        _personal(
            "9",
            "CIC Number",
            "The number of the Certificate of Insurance Cover (CIC) PCIC issued for this "
            "farm. Copy it exactly from the CIC.",
            f"{RICE_BROCHURE}, p. 1: Period of cover",
            section="I",
            label_fil="Numero ng CIC",
        ),
        NolField(
            "10",
            "Underwriter/Cooperative",
            ask=True,
            section="I",
            label_fil="Pangalan ng Ahente o Kooperatiba",
            hint="The PCIC agent, cooperative or lender named on the CIC.",
            help=(
                "Who handled the insurance: the PCIC accredited underwriting agent, the "
                "cooperative or the lending institution named on the CIC. Example: a farmers' "
                "cooperative."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Where to file an application",
        ),
        NolField(
            "11",
            "Program",
            control=CHOICE,
            options=(
                "Regular",
                "Sikat Saka",
                "RSBSA",
                "APCP-CAP-PBD",
                "Punla",
                "Cooperate Rice Farming",
                "Others",
            ),
            ask=True,
            section="I",
            label_fil="Programa",
            hint="As on the CIC; RSBSA is the subsidized cover.",
            help=(
                "The program behind the insurance, as on the CIC. RSBSA: subsidized cover for "
                "farmers in the Registry System for Basic Sectors in Agriculture. Regular: the "
                "farmer paid the premium."
            ),
            help_source=f"{CROP_APPLICATION}, D.1 Source of Premium",
        ),
        NolField(
            "1",
            "Cause of Loss",
            "evidence",
            "cause",
            section="II",
            label_fil="Sanhi ng Pinsala",
            help=(
                "What caused the loss. Example: Typhoon flood submerged the field. Typhoon, "
                "flood, drought and other natural disasters are insured risks."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Types of insurance cover",
        ),
        NolField(
            "2",
            "Date of Loss Occurrence",
            "evidence",
            "first_reported",
            control=PAST_DATE,
            ask=True,
            section="II",
            label_fil="Petsa ng Pinsala",
            hint="When the loss happened. The reports only show when it was first reported.",
            help=(
                "The day the loss happened. Example: 2026-10-24. The reports only show when it "
                "was first reported, which can be later. The filing deadline runs from the loss."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Claim for Indemnity",
        ),
        NolField(
            "3",
            "Age/Stage of Cultivation at time of loss",
            "evidence",
            "stage",
            ask=True,
            section="II",
            label_fil="Edad ng Pananim ng Mapinsala",
            hint="Days after planting, or the stage (e.g. flowering).",
            help=(
                "How old the rice was when the loss happened: days after planting, or the "
                "stage. Example: 75 days, flowering. PCIC bases the indemnity on the stage of "
                "cultivation at the time of loss."
            ),
            help_source=f"{RICE_BROCHURE}, p. 2: Amount of Indemnity",
        ),
        NolField(
            "4",
            "Area Damaged",
            "evidence",
            "stated_area",
            control=HECTARES,
            ask=True,
            section="II",
            label_fil="Luwang o Sukat ng Napinsalang Bahagi",
            paper_unit=("ha.", "ektarya"),
            hint="In hectares, from the farmer; never measured from photos.",
            help=(
                "The damaged part of the insured field, in hectares. Example: 1. Take it from "
                "the farmer; Ulat never measures it from photos. PCIC's adjusters verify it."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Claim adjustment and settlement",
        ),
        NolField(
            "5",
            "Extent/Degree of Damage",
            "evidence",
            "stated_percent",
            control=PERCENT,
            ask=True,
            section="II",
            label_fil="Tindi o Porsyento ng Pinsala",
            paper_unit=("%", "porsyento"),
            hint="The farmer's estimate of the share lost; the adjusters decide.",
            help=(
                "The share of the crop lost, as the farmer estimates it. Example: 60. PCIC's "
                "adjusters decide: 90% and above is a total loss, over 10% a partial loss, "
                "10% or less no loss."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Loss categories",
        ),
        NolField(
            "6",
            "Expected Date of Harvest",
            control=DATE_CONTROL,
            ask=True,
            section="II",
            label_fil="Tinatayang Petsa ng Pagpapagapas o Pag-ani",
            hint="The claim must reach PCIC before this day.",
            help=(
                "When the rice was due for harvest. Example: 2026-11-20. The claim must reach "
                "PCIC before the scheduled harvest, even if that is sooner than the deadline."
            ),
            help_source=f"{RICE_BROCHURE}, p. 1: Claim for Indemnity",
        ),
    ),
    deadline="pcic_rice_ci",
    later_deadlines=(),
    eligible_livelihood=(),
    eligible_counts=(),
    extent_livelihood=(),
    extent_counts=(),
    area_facts=("crop_area_hectares",),
    area_unit="",  # the paper prints "ha. (ektarya)" after the blank
    required_evidence=("photo",),
    sam_concepts=("floodwater",),
    sam_caveat=(
        'SAM 3.1 is prompted with "floodwater", which also covers water standing in a '
        "normally irrigated paddy, so the share of water in a photo is not the extent of the "
        "flood or of the loss."
    ),
    loss_bands=(
        "PCIC classifies the loss after claims adjustment and verification by two PCIC "
        "authorized insurance adjusters (one if there are not enough): total loss 90% and "
        "above; partial loss more than 10% and below 90%; no loss 10% or less. This draft does "
        "not classify the loss."
    ),
    loss_bands_source=RICE_BROCHURE,
    closing=(
        "Thank you. / Very truly yours, / Signature over Printed Name of Assured Farmer-Claimant"
    ),
    document="Claim for Indemnity",
    plain="rice claim",
    claimant="farmer",
    claimants="rice farmers",
    documents="Claims for Indemnity",
    glossary=(
        "rice_claim",
        "Claim for Indemnity (rice)",
        "The form an insured rice farmer sends to PCIC (the Philippine Crop Insurance "
        "Corporation) after a typhoon or flood, asking for adjusters to assess the damage. "
        "Rice has no separate Notice of Loss. It is due within {window} of the loss and before "
        "the harvest; the farmer files it.",
    ),
    kind_prefix="pcic_ci_",
    eligible_facts=(("crop", "rice"),),
    eligible_words="a rice crop",
    percent_facts=("crop_damage_percent",),
    stage_fact="crop_stage",
    stage_labels=RICE_STAGES,
    cause_detail_fact="crop_damage",
    cause_detail=(("submerged", "rice submerged"), ("lodged", "rice lodged (flattened)")),
    insured_option="Palay",
    signer="I.1",
    claim_paper=ClaimPaper(
        title_fil="(PAGHAHABOL BAYAD)",
        to=("The Chief CAD", "PCIC-RO"),
        request=(
            (("Please send your Team of Adjusters to assess damage of my insured crop.", False),),
            (
                (
                    "(Mangyaring magpadala kayo ng tagapag-imbistiga upang tasahin ang naging "
                    "pinsala ng aking",
                    True,
                ),
                (" pananim", False),
            ),
            (("Hereunder are the basic information needed by your office:", False),),
            (("Narito ang mga kinakailangang tala ng iyong tanggapan:)", True),),
        ),
        sections=(
            PaperSection("I.", "BASIC INFORMATION", "MGA PANGUNAHING IMPORMASYON"),
            PaperSection("II.", "DAMAGE INDICATORS", "MGA IMPORMASYON TUNGKOL SA PINSALA"),
        ),
        sketch=PaperSection(
            "III.",
            "LOCATION SKETCH PLAN OF DAMAGED INSURED CROPS (LSP)",
            "KROKIS NG BUKID NG MGA NASALANTANG NAKASEGURONG PANANIM",
        ),
        sketch_note="Isulat ang pangalan ng mga may-ari/nagsasaka ng karatig na sakahan",
        sketch_lots=4,
        sketch_rows=(
            ("North", "Hilaga"),
            ("South", "Timog"),
            ("East", "Silangan"),
            ("West", "Kanluran"),
        ),
        short_lines=("I.7", "I.8", "I.9", "II.1", "II.2"),
        write_in=("Others",),
        thanks="Thank you.",
        closing="Very truly yours,",
        signature="Signature over Printed Name of Assured Farmer-Claimant",
        signature_fil="Lagda sa ibabaw ng Pangalan ng Magsasakang Nakaseguro",
    ),
    by_hand=(
        "Section III, the sketch of the damaged lots with the names of the neighboring "
        "farmers, is written by hand on the printout: Ulat never stores other people's names."
    ),
)

# Every commodity Ulat can draft for; which ones an office handles is app.pcic_lines.
COMMODITIES: dict[str, Commodity] = {c.key: c for c in (FISHERIES, RICE)}


def all_fields(commodity: Commodity) -> list[NolField]:
    """The letter's blanks, then the numbered fields: the order of the paper form."""
    return [*commodity.letter_fields, *commodity.fields]


def commodity_for_kind(kind: str) -> Commodity | None:
    """The commodity a Draft.kind belongs to, or None for the other outputs."""
    return next((c for c in COMMODITIES.values() if c.kind == kind), None)


def check(commodity: Commodity) -> None:
    keys = [f.key for f in (*commodity.letter_fields, *commodity.fields)]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{commodity.key}: duplicate field numbers")
    for f in (*commodity.letter_fields, *commodity.fields):
        check_field(f, f"{commodity.key} field {f.number}")
        if f.mode not in MODES:
            raise ValueError(f"{commodity.key} field {f.number}: unknown mode {f.mode!r}")
        if (f.mode == "evidence") != (f.rule is not None) or (f.rule and f.rule not in RULES):
            raise ValueError(f"{commodity.key} field {f.number}: evidence needs a known rule")
    keys = set(keys)
    if commodity.signer not in keys:
        raise ValueError(f"{commodity.key}: the signer {commodity.signer!r} is not a field")
    paper = commodity.claim_paper
    sections = {s.numeral.rstrip(".") for s in paper.sections} if paper else set()
    for f in commodity.fields:
        if (f.section is not None) != bool(paper) or (f.section and f.section not in sections):
            raise ValueError(f"{commodity.key} field {f.key}: section does not match the paper")
    if paper and not set(paper.short_lines) <= keys:
        raise ValueError(f"{commodity.key}: a short line is not a field")
    for piece, is_key in commodity.preamble_parts:
        if is_key and piece not in keys:
            raise ValueError(f"{commodity.key}: preamble blank {piece!r} is not a field")
    for key in (commodity.deadline, *commodity.later_deadlines):
        if key not in DEADLINES:
            raise ValueError(f"{commodity.key}: deadline {key!r} is not in the deadlines table")
    for concept in commodity.sam_concepts:
        if concept not in SAM_CONCEPTS:
            raise ValueError(f"{commodity.key}: unknown SAM concept {concept!r}")
    if not (commodity.eligible_livelihood or commodity.eligible_counts or commodity.eligible_facts):
        raise ValueError(f"{commodity.key}: no eligibility rule")
    rules = {f.rule for f in commodity.fields}
    needs = {
        "stage": commodity.stage_fact,
        "stated_percent": commodity.percent_facts,
        "insured": commodity.insured_option,
    }
    for rule, data in needs.items():
        if rule in rules and not data:
            raise ValueError(f"{commodity.key}: the {rule!r} rule needs its commodity data")
    insured = [f for f in commodity.fields if f.rule == "insured"]
    if any(commodity.insured_option not in f.options for f in insured):
        raise ValueError(f"{commodity.key}: the insured option is not one of the field's options")
    if len({c.kind for c in COMMODITIES.values()}) != len(COMMODITIES):
        raise ValueError("two commodities share a draft kind")


for _commodity in COMMODITIES.values():
    check(_commodity)
