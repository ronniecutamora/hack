"""The RDANA form in force, as data: sections, numbering, labels and how each field is filled.

Source: NDRRMC Memorandum No. 107, s. 2021, Annex I, "RDANA FORM - To be accomplished within
the first 72 HOURS after a disaster" (PDF pages 13-16), transcribed by eye from the official
scan. The form's own numbering typos are kept as printed (`number`) and flagged `sic`; `key`
is the logical position used for anchors and rules.

Fill modes:
- fixed: the same for every Biñan draft (region, province, city);
- officer: only the officer can give it (event name, persons interviewed, overall assessment,
  submitted by); the event name, type and date can be entered when generating;
- evidence: a deterministic rule over report facts (`rule`, implemented in app.rdana);
- agent: the summary description, written by the agent LLM and validated;
- team: needs the RDANA team on site; never filled from reports.

Every field also says how the officer fills it in (app.fillable): its control, the paper form's
own options, a plain hint, whether only the office can answer it (`ask`, listed first in Simple
mode) and whether it is personal (typed for the printout only, never stored). Options the
transcription of a question leaves out are read from the same scan.
"""

from dataclasses import dataclass, replace

from app.fillable import (
    AGE,
    CHOICE,
    CLASSROOMS,
    COUNT,
    DAYS,
    EMAIL,
    LABEL_SOURCE,
    LONG,
    MULTI,
    PAST_DATETIME,
    PHONE,
    TEXT,
    Control,
    check_field,
)

FORM_ID = "rdana-2021-annex-i"
FORM_TITLE = "RDANA FORM"
FORM_SUBTITLE = "To be accomplished within the first 72 HOURS after a disaster"
FORM_SOURCE = "NDRRMC Memorandum No. 107, s. 2021, Annex I (2021 RDANA SOP)"
REGION, PROVINCE, CITY = "Region IV-A (CALABARZON)", "Laguna", "City of Biñan"
MODES = ("fixed", "officer", "evidence", "agent", "team")


@dataclass(frozen=True)
class FormField:
    key: str
    label: str
    mode: str = "team"
    rule: str | None = None
    value: str | None = None  # fixed value
    options: tuple[str, ...] = ()
    printed: str | None = None  # the number as printed when it differs from key (sic)
    required: bool = False
    heading: bool = False  # a sub-heading such as "1.1 EMERGENCY OPERATION"
    control: Control = TEXT
    ask: bool = False  # only the office can answer it: first in Simple mode's list
    personal: bool = False  # identifies a person: typed for the printout, never stored
    hint: str | None = None
    locked: bool = False  # set by the draft itself (its barangay), not typed
    help: str | None = None  # the "?" text: from HELP below
    help_source: str | None = None
    numbered: bool = True  # False under "Submitted by", which the form does not number

    @property
    def number(self) -> str:
        if not self.numbered:
            return ""
        return self.printed or self.key

    @property
    def editable(self) -> bool:
        return not (self.heading or self.locked or self.mode == "fixed")

    @property
    def sic(self) -> bool:
        return self.printed is not None


@dataclass(frozen=True)
class FormSection:
    number: str
    title: str
    fields: tuple[FormField, ...]
    note: str | None = None


def _h(key: str, label: str) -> FormField:
    return FormField(key, label, mode="team", heading=True)


YES_NO = ("Yes", "No")
YES_NO_UNKNOWN = ("Yes", "No", "Do not know")
PERCENT_BANDS = ("<25%", "26-50%", "51-75%", ">75%")


def _yes_no(key: str, label: str, mode: str = "team", **kw) -> FormField:
    return FormField(key, label, mode, options=YES_NO, control=CHOICE, **kw)


def _yes_no_unknown(key: str, label: str, mode: str = "team", **kw) -> FormField:
    return FormField(key, label, mode, options=YES_NO_UNKNOWN, control=CHOICE, **kw)


def _count(key: str, label: str, mode: str = "team", **kw) -> FormField:
    return FormField(key, label, mode, control=COUNT, **kw)


def _pick(key: str, label: str, options: tuple[str, ...], mode: str = "team", **kw) -> FormField:
    """A check-all-that-apply question."""
    return FormField(key, label, mode, options=options, control=MULTI, **kw)


SECTIONS: tuple[FormSection, ...] = (
    FormSection(
        "1",
        "PROFILE OF THE DISASTER & RDANA MISSION",
        (
            _h("1.1", "EMERGENCY OPERATION"),
            FormField(
                "1.1.1",
                "Name of Operation/Event",
                "officer",
                rule="event_name",
                ask=True,
                hint="The name your office uses for this response, e.g. Typhoon Kristine response.",
            ),
            FormField(
                "1.1.2",
                "Type of Disaster/Event",
                "evidence",
                rule="event_type",
                ask=True,
                hint="Filled with the hazards the reports name; change it to the event's own type.",
            ),
            FormField(
                "1.1.3",
                "Date and Time of Event",
                "evidence",
                rule="event_at",
                control=PAST_DATETIME,
                ask=True,
                hint="When the event struck. The reports only show when people first reported it.",
            ),
            _h("1.2", "RDANA MISSION"),
            FormField("1.2.1", "Region", "fixed", value=REGION, required=True),
            FormField("1.2.2", "Province", "fixed", value=PROVINCE, required=True),
            FormField("1.2.3", "City/Municipality", "fixed", value=CITY, required=True),
            FormField("1.2.4", "Barangay", "evidence", rule="barangay", required=True, locked=True),
            FormField("1.2.5", "Sitio/Purok", "evidence", rule="places"),
            FormField("1.2.6", "GPS Coordinate", "evidence", rule="gps"),
            FormField(
                "1.2.7",
                "Date and Time of RDANA",
                "evidence",
                rule="drafted_at",
                required=True,
                control=PAST_DATETIME,
                hint="When the assessment was made; filled with when this draft was made.",
            ),
            _h("1.3", "LOCAL AUTHORITIES/PERSONS INTERVIEWED"),
            *(
                FormField(
                    key,
                    label,
                    "officer",
                    control=control,
                    ask=True,
                    personal=True,
                )
                for key, label, control in (
                    ("1.3.1", "Name", TEXT),
                    ("1.3.2", "Age", AGE),
                    ("1.3.3", "Office/Organization", TEXT),
                    ("1.3.4", "Designation", TEXT),
                    ("1.3.5", "Phone number", PHONE),
                    ("1.3.6", "Email", EMAIL),
                )
            ),
            FormField(
                "1.4",
                "SUMMARY DESCRIPTION OF DISASTER / INCIDENT (IMPACT AND LOCATION)",
                "agent",
                rule="summary",
                control=LONG,
                hint="Written by Ulat from the reports; edit it freely.",
            ),
        ),
    ),
    FormSection(
        "2",
        "INITIAL IMPACT: (DEMOGRAPHICS)",
        (
            _count("2.1", "Affected Families", "evidence", rule="count:families_affected"),
            _count("2.2", "Affected Persons"),
            FormField(
                "2.2a",
                "Affected children",
                options=("Age 0-2", "Age 3-5", "Age 6-12", "Age 13-17"),
                hint="A number for each age group, e.g. Age 0-2: 4; Age 3-5: 6.",
            ),
            _count("2.2b", "PWD"),
            _count("2.2c", "Elderly"),
            _count(
                "2.3",
                "Displaced Families (Inside ECs)",
                "evidence",
                rule="count:families_evacuated",
            ),
            _count(
                "2.4", "Displaced Persons (Inside ECs)", "evidence", rule="count:persons_evacuated"
            ),
            _count("2.5", "Displaced Families (Outside ECs)"),
            _count("2.6", "Displaced Persons (Outside ECs)"),
            _count("2.7", "Missing (Male)"),
            _count("2.8", "Missing (Female)"),
            _count("2.9", "Missing (Total)", "evidence", rule="count:missing"),
            _count("2.10", "Injured (Male)"),
            _count("2.11", "Injured (Female)"),
            _count("2.12", "Injured (Total)", "evidence", rule="count:injured"),
            _count("2.13", "Dead (Male)"),
            _count("2.14", "Dead (Female)"),
            _count("2.15", "Dead (Total)", "evidence", rule="count:dead"),
        ),
        note=(
            "Depending on the type of disaster, BASELINE may be equal to AFFECTED; but in all "
            "situations, DISPLACED is a subset of AFFECTED; RETURNING is a subset of DISPLACED."
        ),
    ),
    FormSection(
        "3",
        "ACCESSIBILITY",
        (
            _yes_no("3.1", "Is the community accessible?"),
            _pick(
                "3.2",
                "How can the community be reached?",
                (
                    "Car or Bus",
                    "4WD or 6-10-Wheeler Trucks",
                    "Motorcycle",
                    "Foot",
                    "Boat",
                    "Airplane",
                    "Helicopter",
                    "Horse/cow/carabao",
                ),
            ),
            _yes_no(
                "3.3",
                "Are their road segments or bridges that are damaged?",
                "evidence",
                rule="roads",
            ),
            _pick(
                "3.3.1",
                "If yes, details",
                ("Partially passable", "Totally Unpassable", "Others"),
            ),
            _pick(
                "3.4",
                "What is the most immediate need on access?",
                (
                    "Transport",
                    "Debris clearing",
                    "Road repair",
                    "Bridge repair",
                    "Traffic Management",
                    "Early Warning Signs / Guide lights",
                    "Coordination with Port/RORO Operator",
                    "Coordination with CAAP / Airport Mgt.",
                    "Cash",
                    "Others",
                ),
                "evidence",
                rule="access_need",
            ),
        ),
    ),
    FormSection(
        "4",
        "POWER OR ELECTRICITY",
        (
            FormField(
                "4.1",
                "Is there electricity in the community?",
                options=(
                    "Yes",
                    "None (Totally, no power)",
                    "Partial (cite % without power)",
                    "Limited from (time) to",
                    "No power even before the disaster",
                ),
                control=CHOICE,
            ),
            _pick(
                "4.2",
                "Power damage",
                (
                    "Fallen electric posts",
                    "Power lines are cut",
                    "Damaged Power Plant",
                    "Fallen or damaged electric tower",
                    "Damaged transformers",
                    "Others",
                ),
                "evidence",
                rule="power",
            ),
            FormField(
                "4.3",
                "How many days from date of survey will the fuel stock in the community last?",
                control=DAYS,
            ),
            FormField(
                "4.4",
                "Urgent power needs",
                options=(
                    "Generators / Alternative Power Kit (Solar) Quantity",
                    "Gasoline for Generators Quantity",
                    "Diesel for Generators Quantity",
                ),
                hint="What is needed and how many, e.g. Generators: 2; Diesel: 40 L.",
            ),
        ),
    ),
    FormSection(
        "5",
        "COMMUNICATIONS",
        (
            _pick("5.1", "Telecom services operational", ("Smart/Sun", "Globe/TM", "Others")),
            _pick(
                "5.2",
                "Services operational",
                ("Radio - AM", "Radio - FM", "TV Free-Air", "TV Cable", "TV Satellite"),
            ),
            _pick(
                "5.3",
                "Alternative communications operational",
                (
                    "Satellite Phone",
                    "VHF Radio",
                    "UHF Radio",
                    "HF/SSB Radio",
                    "BGAN",
                    "V-SAT",
                ),
            ),
            _pick(
                "5.4",
                "Most immediate need on communication",
                (
                    "Communication to Family or Relatives",
                    "Communication to Responders / Government",
                    "Mobile Phone connection",
                    "Internet connection",
                    "Charging station",
                    "Cash",
                    "TV / RADIO service",
                    "Public Address System",
                    "Others",
                ),
                printed="5.3",
            ),
        ),
    ),
    FormSection(
        "6",
        "EVACUATION CENTER DETAILS",
        (
            _yes_no("6.1", "Is there an evacuation center in the community?"),
            _yes_no("6.2", "Is there a designated camp manager?"),
            FormField(
                "6.3",
                "Evacuation Center summary (Name, Address, GPS coordinates, "
                "Inside EC: # family, # person)",
                control=LONG,
                hint="One line per evacuation center.",
            ),
            _pick(
                "6.4",
                "Existing protection mechanisms",
                (
                    "Security Patrols Organized by the Affected Population",
                    "Police Presence / Patrols",
                    "VAWC Desk",
                    "Community Protection Groups",
                    "BCPC Desk",
                    "Women-Friendly Spaces",
                    "Church Groups",
                    "Child-Friendly Spaces",
                    "Others",
                    "No Protection Mechanism at all",
                ),
            ),
            _pick(
                "6.5",
                "Operational facilities in evacuation areas",
                (
                    "Lighting",
                    "Ventilation",
                    "Sleeping Areas with partition",
                    "Separate Toilets/Baths for Male and Female",
                    "Cooking Areas",
                    "Separate Toilets/Baths for PWDs",
                    "Washing Areas",
                    "Camp Management Team",
                    "Medical / Health Desk",
                    "Police / VAWC Desk",
                    "Marked breast-feeding Areas",
                    "Libreng Tawag / Phone recharging station",
                    "Others",
                ),
            ),
        ),
    ),
    FormSection(
        "7",
        "RELIEF ASSISTANCE",
        (
            _yes_no("7.1", "Has the community/EC received assistance?"),
            FormField(
                "7.2",
                "Assistance received (Name or organization, contact person, contact details, "
                "assistance given: particular / quantity, service date: start / end, "
                "# of families served)",
                control=LONG,
                hint="One line per organization. Use an office's contact details, not a person's.",
            ),
        ),
    ),
    FormSection(
        "8",
        "SEARCH-RESCUE-RETRIEVAL",
        (
            _yes_no_unknown(
                "8.1", "Is SEARCH-RESCUE-RETRIEVAL needed?", "evidence", rule="srr_needed"
            ),
            _pick(
                "8.2",
                "What SRR is needed?",
                (
                    "Search and Rescue",
                    "Mountain",
                    "Water",
                    "Aviation",
                    "Urban",
                    "Collapsed Structure",
                    "Maritime",
                    "Others",
                ),
                "evidence",
                rule="srr_type",
            ),
        ),
    ),
    FormSection(
        "9",
        "LAW and ORDER",
        (
            _yes_no_unknown("9.1", "Is LAW AND ORDER a problem?"),
            _pick(
                "9.2",
                "Threats",
                (
                    "Looting",
                    "Robbery",
                    "Banditry",
                    "Kidnapping",
                    "Human trafficking",
                    "Violence against children/women",
                    "Sexual abuse",
                    "Violence between groups/families",
                    "Juvenile delinquency",
                    "Others",
                ),
            ),
            _pick(
                "9.3",
                "Security actors present",
                (
                    "Barangay Tanod (BPSO)",
                    "Coast Guard",
                    "Police (PNP)",
                    "Other law enforcement units",
                    "Philippine Army/Air Force/Navy/Marine",
                    "Terrorist",
                    "Private armies",
                    "Extremists",
                    "Syndicates/Bandits/Pirates",
                    "Other Armed Groups",
                ),
            ),
        ),
    ),
    FormSection(
        "10",
        "SHELTER",
        (
            _count(
                "10.1",
                "Number and/or percentage of destroyed houses (irreparable)",
                "evidence",
                rule="count:houses_totally_damaged",
                options=PERCENT_BANDS,
            ),
            _count(
                "10.2",
                "Number and/or percentage of damaged houses (e.g. walls, roof and column "
                "collapsed, hanging wall) (repairable)",
                "evidence",
                rule="count:houses_partially_damaged",
                options=PERCENT_BANDS,
            ),
            _pick(
                "10.3",
                "Most immediate need on shelter",
                (
                    "Hammer/Nails/Saw",
                    "Galvanized iron sheets",
                    "Tarpaulins",
                    "Plastic sheeting",
                    "Tents",
                    "Sleeping Mat",
                    "Blanket",
                    "Mosquito Net",
                    "Cash",
                    "Lumber",
                    "Plywood",
                    "Others",
                ),
                "evidence",
                rule="need:shelter",
            ),
        ),
    ),
    FormSection(
        "11",
        "FOOD SECURITY",
        (
            _yes_no("11.1", "Access to food?"),
            _pick(
                "11.2",
                "Main sources of food",
                (
                    "Household food stocks",
                    "Household garden/farm",
                    "Local market",
                    "Local fisher folks/animal growers",
                    "Humanitarian aid",
                    "Other",
                ),
            ),
            _yes_no("11.3", "Local market operating?"),
            _yes_no("11.4", "Food warehouse in the area?"),
            _pick(
                "11.5",
                "Most immediate food need",
                ("Cooked food", "Food pack", "Rice", "Fresh produce", "Cash", "Others"),
                "evidence",
                rule="need:food",
            ),
        ),
    ),
    FormSection(
        "12",
        "WATER SUPPLY",
        (
            _yes_no("12.1", "Access to drinking water?"),
            _yes_no("12.2", "Access to domestic water?"),
            _pick(
                "12.3",
                "Primary drinking water source",
                (
                    "Open well",
                    "Bore hole/hand pump",
                    "Stream/river",
                    "Storage/collection container",
                    "Piped water system",
                    "Other",
                ),
            ),
            _yes_no("12.4", "Own water containers with a lid?"),
            _pick(
                "12.5",
                "Most immediate water needs",
                (
                    "Jerry cans",
                    "Bottled water",
                    "Water Distribution/Delivery",
                    "Water Purification Device",
                    "Cash",
                    "Others",
                ),
                "evidence",
                rule="need:water",
            ),
        ),
    ),
    FormSection(
        "13",
        "SANITATION",
        (
            _yes_no("13.1", "Access to functioning sanitary facilities?"),
            _yes_no("13.2", "Separate facilities for women and men?"),
            _yes_no("13.3", "Adequate personal hygiene supplies?", printed="14.3"),
            _pick(
                "13.4",
                "Most immediate need on sanitation",
                (
                    "Toilet facilities",
                    "Bathing Facilities",
                    "Hygiene kits",
                    "Dignity Kits",
                    "Water / Water Supply",
                    "Solid Waste Management",
                    "Information on Good Sanitary Practices",
                    "Over-all Cleanliness of the area",
                    "Cash",
                    "Others",
                ),
                printed="14.4",
            ),
        ),
    ),
    FormSection(
        "14",
        "HEALTH",
        (
            _yes_no_unknown("14.1", "Access to Health Services?"),
            _pick(
                "14.2",
                "Functional facilities/services",
                (
                    "Barangay health station",
                    "Hospital",
                    "Health desk",
                    "Mobile health units",
                    "Birthing facility",
                    "Others",
                ),
            ),
            _pick(
                "14.3",
                "Main health concerns",
                (
                    "Diarrhea/dehydration",
                    "Skin infections",
                    "Hypertension",
                    "Respiratory infection",
                    "Gastro-intestinal illnesses",
                    "Trauma",
                    "Others",
                ),
            ),
            FormField(
                "14.4",
                "Level of medicines/medical supplies",
                options=("Adequate", "Inadequate"),
                control=CHOICE,
            ),
            _pick(
                "14.5",
                "Most immediate need on health",
                (
                    "Mobile clinics",
                    "Health personnel",
                    "Medicines",
                    "Medical Supplies",
                    "Maternity and New born Kits",
                    "Reproductive Health Commodities",
                    "Supply of Blood",
                    "Cash",
                    "Others",
                ),
                "evidence",
                rule="need:medicine",
                printed="15.5",
            ),
        ),
    ),
    FormSection(
        "15",
        "NUTRITION",
        (
            _yes_no("15.1", "Information on exclusively breastfed infants?"),
            _yes_no("15.2", "Infant milk products distributed?"),
            _pick(
                "15.3",
                "Uninterrupted nutrition activities",
                (
                    "Vitamin A",
                    "Iron-Folic Acid",
                    "Micronutrient Powders",
                    "Moderate/severe acute malnutrition management",
                ),
            ),
            _pick(
                "15.4",
                "Most immediate need on nutrition",
                (
                    "Food",
                    "Food supplements",
                    "Bottled water",
                    "Management of Children with severe malnutrition",
                    "Cash",
                    "Others",
                ),
            ),
        ),
    ),
    FormSection(
        "16",
        "PROTECTION",
        (
            _yes_no_unknown(
                "16.1", "Cases of violence as a result of the disaster or displacement?"
            ),
            _pick(
                "16.2",
                "Vulnerable people present",
                (
                    "Unaccompanied/Separated MINORS/ORPHANS",
                    "Unaccompanied/Separated SENIORS",
                    "Sick",
                    "PWD",
                    "PWSN",
                    "Pregnant/Lactating",
                    "Child Headed",
                    "Single Adult Headed",
                    "Senior Headed",
                    "Ethnic/IP",
                ),
            ),
            _yes_no(
                "16.3", "Existing reporting mechanism for protection incidents?", printed="17.3"
            ),
            _pick(
                "16.4",
                "Most immediate need on protection essential services",
                (
                    "Referral",
                    "Police Presence",
                    "Local Gov't. Official's Presence",
                    "Social Services",
                    "Counseling / Debriefing",
                    "Replacement of identification documents",
                    "Others",
                ),
                printed="17.4",
            ),
        ),
    ),
    FormSection(
        "17",
        "EDUCATION",
        (
            FormField("17.1", "Classrooms used as evacuation centers", control=CLASSROOMS),
            _count("17.2", "Children staying in evacuation centers"),
            FormField("17.3", "Destroyed (irreparable) classrooms", control=CLASSROOMS),
            FormField("17.4", "Damaged (repairable) classrooms", control=CLASSROOMS),
            _pick(
                "17.5",
                "Most urgent educational needs",
                (
                    "Safe spaces to hold classes",
                    "Replacement of Children's School Supplies",
                    "Replacement of Learning Materials",
                    "Replacement of Teaching Materials",
                    "Repair of Damaged Buildings",
                    "Securing Water & Sanitation Facilities at Learning sites",
                    "Integrating affected children to other schools",
                    "Securing Light & Ventilation at Learning sites",
                    "Cash",
                    "Others",
                ),
                printed="117.5",
            ),
        ),
    ),
    FormSection(
        "18",
        "LIVELIHOOD/ EARLY RECOVERY",
        (
            FormField(
                "18.1",
                "What is the main source of livelihood?",
                "evidence",
                rule="livelihood",
                control=LONG,
                hint="Filled with the livelihood losses the reports name; give the main "
                "livelihood if you know it.",
            ),
            _pick(
                "18.2",
                "Most immediate need on livelihood/early recovery",
                ("Transport", "Cash for Work", "Debris clearance", "Cash", "Others"),
                "evidence",
                rule="livelihood_need",
            ),
        ),
    ),
    FormSection(
        "19",
        "COMMUNITY ENGAGEMENT",
        (
            _yes_no_unknown("19.1", "Does the community receive needed information?"),
            _pick(
                "19.2",
                "What affected people want to know most",
                (
                    "General situation",
                    "Situation at home",
                    "Information about family members",
                    "What aid is coming",
                    "Weather situation",
                    "Health advice",
                    "Security information",
                    "Other",
                ),
            ),
            _pick(
                "19.3",
                "Main sources of information",
                (
                    "Friends/neighbors/family",
                    "Community leader",
                    "Religious leader",
                    "Government official",
                    "Military official",
                    "TV (national/local)",
                    "Newspaper (national/local)",
                    "AM/FM radio",
                    "Aid worker",
                    "Community group",
                    "Social media",
                    "Others",
                ),
            ),
        ),
    ),
    FormSection(
        "20",
        "OVER-ALL ASSESSMENT (concluding observation of the enumerator)",
        (
            FormField(
                "20.1",
                "General situation",
                "officer",
                printed="21.1",
                options=(
                    "People facing serious problems",
                    "People will get sick and might even die",
                    "Many people have already died",
                ),
                control=CHOICE,
                ask=True,
                hint="Your overall judgment of the area, not the key informant's.",
            ),
            FormField(
                "20.2",
                "General justification for the answer",
                "officer",
                control=LONG,
                ask=True,
                hint="Two or three sentences on why, from what you saw and the reports.",
            ),
        ),
    ),
    FormSection(
        "",
        "Submitted by",
        tuple(
            FormField(
                f"submitted.{key}",
                label,
                "officer",
                ask=True,
                personal=personal,
                control=control,
                numbered=False,
            )
            for key, label, personal, control in (
                ("name", "NAME", True, TEXT),
                ("designation", "DESIGNATION", False, TEXT),
                ("organization", "ORGANIZATION", False, TEXT),
                ("team", "RDANA Team", False, TEXT),
                ("contact", "CONTACT #", True, PHONE),
                ("email", "EMAIL", True, EMAIL),
            )
        ),
    ),
)


# ---------- The "?" help of every field ----------
# What to enter and an example, in plain words, with its source. The SOP's body explains the
# procedure, not each field, so most help only says what the form's own label asks (with the
# Annex I page that prints it); a few fields cite the note or heading printed on the form, or
# the SOP section that governs them. Never an invented rule.
SOP = "NDRRMC Memo 107 s.2021, RDANA SOP"
# The first field printed on each page of Annex I (PDF pages of the SOP), in form order.
ANNEX_PAGES = (("1.1.1", 13), ("6.3", 14), ("13.2", 15), ("20.2", 16))
NOTE_2 = f"{SOP}, Annex I, PDF p. 13: the note under section 2"
HEADING_20 = f"{SOP}, Annex I, PDF p. 15: the heading of section 20"
REPORTING = f"{SOP}, sec. 7.5.2: completed forms go to the EOC"

# key: (help, source); None as the source means the form's own label.
HELP: dict[str, tuple[str, str | None]] = {
    "1.1.1": (
        "The name your office uses for this response, as on its other reports. Example: Typhoon "
        "Kristine response.",
        None,
    ),
    "1.1.2": (
        "The kind of disaster, in a few words. Example: Typhoon and flood. Ulat starts with the "
        "hazards the reports name.",
        None,
    ),
    "1.1.3": (
        "When the disaster struck, Philippine time. Example: 2026-10-24 22:00. The reports only "
        "show when people first reported it, which can be later.",
        None,
    ),
    "1.2.1": (
        "The region of the place assessed. The same for every Biñan form: Region IV-A "
        "(CALABARZON).",
        None,
    ),
    "1.2.2": ("The province of the place assessed. The same for every Biñan form: Laguna.", None),
    "1.2.3": (
        "The city or municipality of the place assessed. The same for every Biñan form: City of "
        "Biñan.",
        None,
    ),
    "1.2.4": (
        "The barangay this form covers. Set by the draft: there is one RDANA form per barangay.",
        None,
    ),
    "1.2.5": (
        "The sitio or purok assessed, as people there call it. Example: Purok 3, near the "
        "barangay hall.",
        None,
    ),
    "1.2.6": (
        "Where the assessment was made, as latitude, longitude. Example: 14.3512, 121.0900. "
        "Ulat starts with the middle of the reports' map pins.",
        None,
    ),
    "1.2.7": (
        "When the team made this RDANA (Rapid Damage Assessment and Needs Analysis), Philippine "
        "time. Example: 2026-10-26 09:30. The completed form then goes to the EOC (Emergency "
        "Operations Center).",
        REPORTING,
    ),
    "1.3.1": (
        "The full name of the official or resident you interviewed. Example: Juan dela Cruz.",
        None,
    ),
    "1.3.2": ("The age in years of the person you interviewed. Example: 52.", None),
    "1.3.3": (
        "The office or group of the person you interviewed. Example: Barangay Malaban council.",
        None,
    ),
    "1.3.4": ("The position of the person you interviewed. Example: Barangay captain.", None),
    "1.3.5": ("A phone number for the person you interviewed. Example: 0917 123 4567.", None),
    "1.3.6": ("An email address for the person you interviewed, if they have one.", None),
    "1.4": (
        "A few sentences on what happened and where: the damage, who was hit, which places. "
        "Ulat writes a first version from the reports; change it freely.",
        None,
    ),
    "2.1": (
        "How many families the disaster hit here. Example: 120. Families who had to leave home "
        "(displaced) are counted here too.",
        NOTE_2,
    ),
    "2.2": (
        "How many people the disaster hit here. Example: 540. People who had to leave home "
        "(displaced) are counted here too.",
        NOTE_2,
    ),
    "2.2a": (
        "How many of the affected people are children, by age group. Example: Age 0-2: 14; Age "
        "3-5: 22.",
        None,
    ),
    "2.2b": (
        "How many of the affected people are persons with disability (PWD). Example: 6.",
        None,
    ),
    "2.2c": ("How many of the affected people are elderly. Example: 31.", None),
    "2.3": (
        "Displaced families staying in evacuation centers (ECs). Example: 45. They are also "
        "counted in 2.1, affected families.",
        NOTE_2,
    ),
    "2.4": (
        "Displaced people staying in evacuation centers (ECs). Example: 180. They are also "
        "counted in 2.2, affected persons.",
        NOTE_2,
    ),
    "2.5": (
        "Displaced families staying outside evacuation centers, such as with relatives. "
        "Example: 12. Also counted in 2.1.",
        NOTE_2,
    ),
    "2.6": (
        "Displaced people staying outside evacuation centers, such as with relatives. Example: "
        "50. Also counted in 2.2.",
        NOTE_2,
    ),
    "2.7": (
        "How many males are missing. Example: 1. Write 0 only if you checked and none are; "
        "leave it empty if unknown.",
        None,
    ),
    "2.8": ("How many females are missing. Example: 0, if you checked and none are.", None),
    "2.9": ("How many people are missing in all: males and females together. Example: 1.", None),
    "2.10": ("How many males were injured. Example: 3.", None),
    "2.11": ("How many females were injured. Example: 2.", None),
    "2.12": ("How many people were injured in all: males and females together. Example: 5.", None),
    "2.13": ("How many males died. Example: 0, if you checked and none did.", None),
    "2.14": ("How many females died. Example: 0, if you checked and none did.", None),
    "2.15": ("How many people died in all: males and females together. Example: 0.", None),
    "3.1": ("Can people and vehicles get to the community now? Choose Yes or No.", None),
    "3.2": (
        "Tick every way the community can be reached now. Example: Foot and Boat, when the "
        "roads are under water.",
        None,
    ),
    "3.3": (
        "Are any roads or bridges damaged? Yes or No. Ulat starts with Yes when the reports say "
        "so.",
        None,
    ),
    "3.3.1": (
        "If roads or bridges are damaged: are they partly passable, not passable at all, or other?",
        None,
    ),
    "3.4": ("Tick the most urgent needs to reach the community. Example: Debris clearing.", None),
    "4.1": (
        "Is there electricity now? Choose one. Example: Partial, if only some puroks have power.",
        None,
    ),
    "4.2": ("Tick the damage to power you saw. Example: Fallen electric posts.", None),
    "4.3": (
        "How many days the fuel in the community will last, counted from the day of the survey. "
        "Example: 3.",
        None,
    ),
    "4.4": ("The power supplies needed and how many. Example: Generators: 2; Diesel: 40 L.", None),
    "5.1": ("Tick the mobile networks that work. Example: Globe/TM.", None),
    "5.2": ("Tick the radio and TV services that work. Example: Radio - AM.", None),
    "5.3": ("Tick the other ways to communicate that work. Example: VHF Radio.", None),
    "5.4": ("Tick the most urgent communication needs. Example: Charging station.", None),
    "6.1": ("Is there an evacuation center in the community? Yes or No.", None),
    "6.2": ("Does the evacuation center have a designated camp manager? Yes or No.", None),
    "6.3": (
        "One line per evacuation center: name, address, GPS, families and people inside. "
        "Example: Malaban Elementary School, 14.35, 121.09, 40 families, 160 people.",
        None,
    ),
    "6.4": (
        "Tick the protection against abuse or violence in the community or the centers. "
        "Example: VAWC Desk (Violence Against Women and Children).",
        None,
    ),
    "6.5": (
        "Tick the facilities that work in the evacuation areas. Example: Lighting, Cooking Areas.",
        None,
    ),
    "7.1": ("Has the community or evacuation center received help? Yes or No.", None),
    "7.2": (
        "One line per group that gave help: name, contact, what and how much, from and to which "
        "dates, families served. Give an office's contact, not a person's.",
        None,
    ),
    "8.1": (
        "Is search, rescue or retrieval needed? Yes, No or Do not know. Ulat starts with Yes "
        "when the reports ask for rescue.",
        None,
    ),
    "8.2": (
        "Tick the kinds of search and rescue needed. Example: Water, for people trapped by the "
        "flood.",
        None,
    ),
    "9.1": ("Is law and order a problem? Yes, No or Do not know.", None),
    "9.2": ("Tick the threats in the community. Example: Looting.", None),
    "9.3": (
        "Tick the security forces present. Example: Barangay Tanod (BPSO), Police (PNP).",
        None,
    ),
    "10.1": (
        "How many houses were destroyed beyond repair, and tick their share of all houses. "
        "Example: 8, and <25%.",
        None,
    ),
    "10.2": (
        "How many houses were damaged but can be repaired (walls, roofs), and tick their share "
        "of all houses. Example: 35, and 26-50%.",
        None,
    ),
    "10.3": (
        "Tick the most urgent shelter needs. Example: Tarpaulins, Galvanized iron sheets.",
        None,
    ),
    "11.1": ("Can people get food where they are now? Yes or No.", None),
    "11.2": ("Tick where people get food now. Example: Local market, Humanitarian aid.", None),
    "11.3": ("Is the local market open? Yes or No.", None),
    "11.4": ("Is there a food warehouse in the area? Yes or No.", None),
    "11.5": ("Tick the most urgent food needs. Example: Food pack, Rice.", None),
    "12.1": ("Can people get water to drink? Yes or No.", None),
    "12.2": ("Can people get water for washing and cooking? Yes or No.", None),
    "12.3": ("Tick where the drinking water comes from. Example: Piped water system.", None),
    "12.4": ("Do households have their own water containers with a lid? Yes or No.", None),
    "12.5": ("Tick the most urgent water needs. Example: Water Distribution/Delivery.", None),
    "13.1": ("Can people use working toilets? Yes or No.", None),
    "13.2": ("Are there separate toilets and baths for women and men? Yes or No.", None),
    "13.3": (
        "Do families have enough hygiene supplies, such as soap and sanitary pads? Yes or No.",
        None,
    ),
    "13.4": ("Tick the most urgent sanitation needs. Example: Hygiene kits.", None),
    "14.1": ("Can people get health services? Yes, No or Do not know.", None),
    "14.2": (
        "Tick the health facilities and services that work. Example: Barangay health station.",
        None,
    ),
    "14.3": (
        "Tick the main health problems. Example: Diarrhea/dehydration, Skin infections.",
        None,
    ),
    "14.4": ("Are the medicines and medical supplies enough? Adequate or Inadequate.", None),
    "14.5": ("Tick the most urgent health needs. Example: Medicines.", None),
    "15.1": ("Is there information on babies fed only breast milk? Yes or No.", None),
    "15.2": (
        "Have infant milk or baby bottles been handed out since the disaster? Yes or No.",
        None,
    ),
    "15.3": (
        "Tick the nutrition programs still running without a break. Example: Vitamin A.",
        None,
    ),
    "15.4": ("Tick the most urgent nutrition needs. Example: Food supplements.", None),
    "16.1": (
        "Are there cases of violence caused by the disaster or by people leaving home, reported "
        "or not? Yes, No or Do not know.",
        None,
    ),
    "16.2": (
        "Tick the vulnerable people present who need help. Example: Pregnant/Lactating, PWD.",
        None,
    ),
    "16.3": ("Is there a way to report abuse or other protection incidents? Yes or No.", None),
    "16.4": ("Tick the most urgent protection services needed. Example: Social Services.", None),
    "17.1": ("How many classrooms are used as evacuation centers. Example: 4.", None),
    "17.2": ("How many children stay in evacuation centers. Example: 60.", None),
    "17.3": ("How many classrooms were destroyed beyond repair. Example: 1.", None),
    "17.4": ("How many classrooms were damaged but can be repaired. Example: 3.", None),
    "17.5": (
        "Tick the most urgent school needs. Example: Replacement of Learning Materials.",
        None,
    ),
    "18.1": (
        "How most people here earn a living. Example: Fishing and fish pens on Laguna de Bay. "
        "Ulat starts with the livelihood losses the reports name.",
        None,
    ),
    "18.2": ("Tick the most urgent livelihood needs. Example: Cash for Work.", None),
    "19.1": (
        "Does the community get the information it needs from the government or aid groups? "
        "Yes, No or Do not know.",
        None,
    ),
    "19.2": ("Tick what the affected people most want to know. Example: What aid is coming.", None),
    "19.3": ("Tick where people get their information. Example: AM/FM radio, Social media.", None),
    "20.1": (
        "Your own conclusion as the assessor, from what you saw: do not ask the person you "
        "interviewed. Choose the one that fits best.",
        HEADING_20,
    ),
    "20.2": (
        "Why you chose that answer in 20.1, in two or three sentences, from what you saw and "
        "heard. Example: Many roofs blown off; families still in the school.",
        HEADING_20,
    ),
    "submitted.name": ("The full name of the person submitting this form.", None),
    "submitted.designation": ("The submitter's position. Example: CDRRMO Head.", None),
    "submitted.organization": ("The submitter's office. Example: Biñan City DRRM Office.", None),
    "submitted.team": (
        "The name or number of the RDANA team that made the assessment. Example: Team Alpha.",
        None,
    ),
    "submitted.contact": ("A phone number for the submitter. Example: 0917 123 4567.", None),
    "submitted.email": ("An email address for the submitter, ideally the office's.", None),
}


def _label_source(page: int) -> str:
    return f"{LABEL_SOURCE} ({SOP}, Annex I, PDF p. {page})"


def _with_help(sections: tuple[FormSection, ...]) -> tuple[FormSection, ...]:
    """The sections with each field's help and source from HELP (a missing one fails the check)."""
    starts, page, out = dict(ANNEX_PAGES), ANNEX_PAGES[0][1], []
    for section in sections:
        fields = []
        for f in section.fields:
            page = starts.get(f.key, page)
            if not f.heading and f.key in HELP:
                text, source = HELP[f.key]
                f = replace(f, help=text, help_source=source or _label_source(page))
            fields.append(f)
        out.append(replace(section, fields=tuple(fields)))
    return tuple(out)


SECTIONS = _with_help(SECTIONS)


def all_fields() -> list[FormField]:
    return [f for section in SECTIONS for f in section.fields]


def _check() -> None:
    keys = [f.key for f in all_fields()]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise ValueError(f"duplicate RDANA field keys: {sorted(duplicates)}")
    for f in all_fields():
        if f.mode not in MODES:
            raise ValueError(f"RDANA field {f.key}: unknown mode {f.mode!r}")
        if f.mode in ("evidence", "agent") and not f.rule:
            raise ValueError(f"RDANA field {f.key}: {f.mode} field needs a rule")
        if f.mode == "fixed" and not f.value:
            raise ValueError(f"RDANA field {f.key}: fixed field needs a value")
        if not f.heading:
            check_field(f, f"RDANA field {f.key}")
    stale = set(HELP) - set(keys)
    if stale:
        raise ValueError(f"help for fields the form does not have: {sorted(stale)}")


_check()
