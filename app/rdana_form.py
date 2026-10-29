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
"""

from dataclasses import dataclass

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

    @property
    def number(self) -> str:
        return self.printed or self.key

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

SECTIONS: tuple[FormSection, ...] = (
    FormSection(
        "1",
        "PROFILE OF THE DISASTER & RDANA MISSION",
        (
            _h("1.1", "EMERGENCY OPERATION"),
            FormField("1.1.1", "Name of Operation/Event", "officer", rule="event_name"),
            FormField("1.1.2", "Type of Disaster/Event", "evidence", rule="event_type"),
            FormField("1.1.3", "Date and Time of Event", "evidence", rule="event_at"),
            _h("1.2", "RDANA MISSION"),
            FormField("1.2.1", "Region", "fixed", value=REGION, required=True),
            FormField("1.2.2", "Province", "fixed", value=PROVINCE, required=True),
            FormField("1.2.3", "City/Municipality", "fixed", value=CITY, required=True),
            FormField("1.2.4", "Barangay", "evidence", rule="barangay", required=True),
            FormField("1.2.5", "Sitio/Purok", "evidence", rule="places"),
            FormField("1.2.6", "GPS Coordinate", "evidence", rule="gps"),
            FormField(
                "1.2.7", "Date and Time of RDANA", "evidence", rule="drafted_at", required=True
            ),
            _h("1.3", "LOCAL AUTHORITIES/PERSONS INTERVIEWED"),
            FormField("1.3.1", "Name", "officer"),
            FormField("1.3.2", "Age", "officer"),
            FormField("1.3.3", "Office/Organization", "officer"),
            FormField("1.3.4", "Designation", "officer"),
            FormField("1.3.5", "Phone number", "officer"),
            FormField("1.3.6", "Email", "officer"),
            FormField(
                "1.4",
                "SUMMARY DESCRIPTION OF DISASTER / INCIDENT (IMPACT AND LOCATION)",
                "agent",
                rule="summary",
            ),
        ),
    ),
    FormSection(
        "2",
        "INITIAL IMPACT: (DEMOGRAPHICS)",
        (
            FormField("2.1", "Affected Families", "evidence", rule="count:families_affected"),
            FormField("2.2", "Affected Persons"),
            FormField(
                "2.2a",
                "Affected children",
                options=("Age 0-2", "Age 3-5", "Age 6-12", "Age 13-17"),
            ),
            FormField("2.2b", "PWD"),
            FormField("2.2c", "Elderly"),
            FormField(
                "2.3",
                "Displaced Families (Inside ECs)",
                "evidence",
                rule="count:families_evacuated",
            ),
            FormField(
                "2.4", "Displaced Persons (Inside ECs)", "evidence", rule="count:persons_evacuated"
            ),
            FormField("2.5", "Displaced Families (Outside ECs)"),
            FormField("2.6", "Displaced Persons (Outside ECs)"),
            FormField("2.7", "Missing (Male)"),
            FormField("2.8", "Missing (Female)"),
            FormField("2.9", "Missing (Total)", "evidence", rule="count:missing"),
            FormField("2.10", "Injured (Male)"),
            FormField("2.11", "Injured (Female)"),
            FormField("2.12", "Injured (Total)", "evidence", rule="count:injured"),
            FormField("2.13", "Dead (Male)"),
            FormField("2.14", "Dead (Female)"),
            FormField("2.15", "Dead (Total)", "evidence", rule="count:dead"),
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
            FormField("3.1", "Is the community accessible?", options=YES_NO),
            FormField(
                "3.2",
                "How can the community be reached?",
                options=(
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
            FormField(
                "3.3",
                "Are their road segments or bridges that are damaged?",
                "evidence",
                rule="roads",
                options=YES_NO,
            ),
            FormField(
                "3.3.1",
                "If yes, details",
                options=("Partially passable", "Totally Unpassable", "Others"),
            ),
            FormField(
                "3.4",
                "What is the most immediate need on access?",
                "evidence",
                rule="access_need",
                options=(
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
            ),
            FormField(
                "4.2",
                "Power damage",
                "evidence",
                rule="power",
                options=(
                    "Fallen electric posts",
                    "Power lines are cut",
                    "Damaged Power Plant",
                    "Fallen or damaged electric tower",
                    "Damaged transformers",
                    "Others",
                ),
            ),
            FormField(
                "4.3",
                "How many days from date of survey will the fuel stock in the community last?",
            ),
            FormField(
                "4.4",
                "Urgent power needs",
                options=(
                    "Generators / Alternative Power Kit (Solar) Quantity",
                    "Gasoline for Generators Quantity",
                    "Diesel for Generators Quantity",
                ),
            ),
        ),
    ),
    FormSection(
        "5",
        "COMMUNICATIONS",
        (
            FormField(
                "5.1", "Telecom services operational", options=("Smart/Sun", "Globe/TM", "Others")
            ),
            FormField(
                "5.2",
                "Services operational",
                options=("Radio - AM", "Radio - FM", "TV Free-Air", "TV Cable", "TV Satellite"),
            ),
            FormField(
                "5.3",
                "Alternative communications operational",
                options=(
                    "Satellite Phone",
                    "VHF Radio",
                    "UHF Radio",
                    "HF/SSB Radio",
                    "BGAN",
                    "V-SAT",
                ),
            ),
            FormField(
                "5.4",
                "Most immediate need on communication",
                printed="5.3",
                options=(
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
            ),
        ),
    ),
    FormSection(
        "6",
        "EVACUATION CENTER DETAILS",
        (
            FormField("6.1", "Is there an evacuation center in the community?", options=YES_NO),
            FormField("6.2", "Is there a designated camp manager?", options=YES_NO),
            FormField(
                "6.3",
                "Evacuation Center summary (Name, Address, GPS coordinates, "
                "Inside EC: # family, # person)",
            ),
            FormField("6.4", "Existing protection mechanisms"),
            FormField("6.5", "Operational facilities in evacuation areas"),
        ),
    ),
    FormSection(
        "7",
        "RELIEF ASSISTANCE",
        (
            FormField("7.1", "Has the community/EC received assistance?", options=YES_NO),
            FormField(
                "7.2",
                "Assistance received (Name or organization, contact person, contact details, "
                "assistance given: particular / quantity, service date: start / end, "
                "# of families served)",
            ),
        ),
    ),
    FormSection(
        "8",
        "SEARCH-RESCUE-RETRIEVAL",
        (
            FormField(
                "8.1",
                "Is SEARCH-RESCUE-RETRIEVAL needed?",
                "evidence",
                rule="srr_needed",
                options=YES_NO_UNKNOWN,
            ),
            FormField(
                "8.2",
                "What SRR is needed?",
                "evidence",
                rule="srr_type",
                options=(
                    "Search and Rescue",
                    "Mountain",
                    "Water",
                    "Aviation",
                    "Urban",
                    "Collapsed Structure",
                    "Maritime",
                    "Others",
                ),
            ),
        ),
    ),
    FormSection(
        "9",
        "LAW and ORDER",
        (
            FormField("9.1", "Is LAW AND ORDER a problem?", options=YES_NO_UNKNOWN),
            FormField(
                "9.2",
                "Threats",
                options=(
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
            FormField("9.3", "Security actors present"),
        ),
    ),
    FormSection(
        "10",
        "SHELTER",
        (
            FormField(
                "10.1",
                "Number and/or percentage of destroyed houses (irreparable)",
                "evidence",
                rule="count:houses_totally_damaged",
                options=("<25%", "26-50%", "51-75%", ">75%"),
            ),
            FormField(
                "10.2",
                "Number and/or percentage of damaged houses (e.g. walls, roof and column "
                "collapsed, hanging wall) (repairable)",
                "evidence",
                rule="count:houses_partially_damaged",
                options=("<25%", "26-50%", "51-75%", ">75%"),
            ),
            FormField(
                "10.3",
                "Most immediate need on shelter",
                "evidence",
                rule="need:shelter",
                options=(
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
            ),
        ),
    ),
    FormSection(
        "11",
        "FOOD SECURITY",
        (
            FormField("11.1", "Access to food?"),
            FormField("11.2", "Main sources of food"),
            FormField("11.3", "Local market operating?"),
            FormField("11.4", "Food warehouse in the area?"),
            FormField("11.5", "Most immediate food need", "evidence", rule="need:food"),
        ),
    ),
    FormSection(
        "12",
        "WATER SUPPLY",
        (
            FormField("12.1", "Access to drinking water?"),
            FormField("12.2", "Access to domestic water?"),
            FormField("12.3", "Primary drinking water source"),
            FormField("12.4", "Own water containers with a lid?"),
            FormField("12.5", "Most immediate water needs", "evidence", rule="need:water"),
        ),
    ),
    FormSection(
        "13",
        "SANITATION",
        (
            FormField("13.1", "Access to functioning sanitary facilities?"),
            FormField("13.2", "Separate facilities for women and men?"),
            FormField("13.3", "Adequate personal hygiene supplies?", printed="14.3"),
            FormField("13.4", "Most immediate need on sanitation", printed="14.4"),
        ),
    ),
    FormSection(
        "14",
        "HEALTH",
        (
            FormField("14.1", "Access to Health Services?"),
            FormField("14.2", "Functional facilities/services"),
            FormField("14.3", "Main health concerns"),
            FormField("14.4", "Level of medicines/medical supplies"),
            FormField(
                "14.5",
                "Most immediate need on health",
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
            FormField("15.1", "Information on exclusively breastfed infants?"),
            FormField("15.2", "Infant milk products distributed?"),
            FormField(
                "15.3",
                "Uninterrupted nutrition activities",
                options=(
                    "Vitamin A",
                    "Iron-Folic Acid",
                    "Micronutrient Powders",
                    "Moderate/severe acute malnutrition management",
                ),
            ),
            FormField("15.4", "Most immediate need on nutrition"),
        ),
    ),
    FormSection(
        "16",
        "PROTECTION",
        (
            FormField("16.1", "Cases of violence as a result of the disaster or displacement?"),
            FormField("16.2", "Vulnerable people present"),
            FormField(
                "16.3", "Existing reporting mechanism for protection incidents?", printed="17.3"
            ),
            FormField(
                "16.4", "Most immediate need on protection essential services", printed="17.4"
            ),
        ),
    ),
    FormSection(
        "17",
        "EDUCATION",
        (
            FormField("17.1", "Classrooms used as evacuation centers"),
            FormField("17.2", "Children staying in evacuation centers"),
            FormField("17.3", "Destroyed (irreparable) classrooms"),
            FormField("17.4", "Damaged (repairable) classrooms"),
            FormField("17.5", "Most urgent educational needs", printed="117.5"),
        ),
    ),
    FormSection(
        "18",
        "LIVELIHOOD/ EARLY RECOVERY",
        (
            FormField(
                "18.1", "What is the main source of livelihood?", "evidence", rule="livelihood"
            ),
            FormField(
                "18.2",
                "Most immediate need on livelihood/early recovery",
                "evidence",
                rule="livelihood_need",
                options=("Transport", "Cash for Work", "Debris clearance", "Cash", "Others"),
            ),
        ),
    ),
    FormSection(
        "19",
        "COMMUNITY ENGAGEMENT",
        (
            FormField("19.1", "Does the community receive needed information?"),
            FormField("19.2", "What affected people want to know most"),
            FormField("19.3", "Main sources of information"),
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
            ),
            FormField("20.2", "General justification for the answer", "officer"),
        ),
    ),
    FormSection(
        "",
        "Submitted by",
        tuple(
            FormField(f"submitted.{key}", label, "officer")
            for key, label in (
                ("name", "NAME"),
                ("designation", "DESIGNATION"),
                ("organization", "ORGANIZATION"),
                ("team", "RDANA Team"),
                ("contact", "CONTACT #"),
                ("email", "EMAIL"),
            )
        ),
    ),
)


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


_check()
