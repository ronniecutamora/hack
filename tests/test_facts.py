import pytest

from app.facts import FACTS_SCHEMA, extract_facts, redact_self_introductions
from app.meta.schema import validate


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        (
            "Ako po si Lando Maligsi, corn farmer sa Barangay Daplisaya.",
            "Ako po si [pangalan], corn farmer sa Barangay Daplisaya.",
        ),
        (
            "Si Nena Salindap po ito ng Barangay Luntianhibla.",
            "Si [pangalan] po ito ng Barangay Luntianhibla.",
        ),
        ("ako si rodel pansilaw, rice farmer", "ako si [pangalan], rice farmer"),
        (
            "Update po. Si Mila Dalisban po, livestock raiser",
            "Update po. Si [pangalan] po, livestock raiser",
        ),
        (
            "Barangay official po ito ng Barangay Sinagpulo.",
            "Barangay official po ito ng Barangay Sinagpulo.",
        ),
        ("Baha na po hanggang tuhod", "Baha na po hanggang tuhod"),
    ],
)
def test_self_introduced_names_are_redacted(text, redacted):
    assert redact_self_introductions(text) == redacted


def test_schema_requires_every_field_and_allows_unknown_counts():
    assert set(FACTS_SCHEMA["required"]) == set(FACTS_SCHEMA["properties"])
    assert FACTS_SCHEMA["properties"]["houses_totally_damaged"]["type"] == ["integer", "null"]


class FakeLLM:
    model = "fake-llm"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


async def test_extract_facts_sends_redacted_text_with_the_schema():
    reply = {key: None for key in FACTS_SCHEMA["properties"]}
    reply.update(
        hazards=["flood"],
        flood_depth="knee",
        crop="none",
        infrastructure=[],
        needs=["water"],
        is_correction=False,
        summary_en="23 houses flooded knee-deep.",
        houses_flooded=23,
    )
    validate(FACTS_SCHEMA, reply)
    llm = FakeLLM(reply)

    facts = await extract_facts(llm, "Ako po si Juan Dela Cruz. May 23 bahay na binaha.")

    assert facts == reply
    (call,) = llm.calls
    assert "Juan" not in call["user_text"] and "[pangalan]" in call["user_text"]
    assert call["schema"] is FACTS_SCHEMA and call.get("image") is None


async def test_repeated_list_values_are_kept_once_in_order():
    reply = {key: None for key in FACTS_SCHEMA["properties"]}
    reply.update(needs=["clearing", "food", "clearing", "clearing"], hazards=["flood"])
    facts = await extract_facts(FakeLLM(reply), "may baha")
    assert facts["needs"] == ["clearing", "food"]
