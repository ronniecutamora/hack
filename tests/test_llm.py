import pytest
from pydantic import ValidationError

from app.config import Settings
from app.meta.base import ModelOutputError
from app.meta.factory import make_agent_llm
from app.meta.schema import validate
from app.meta.spark import SparkClient
from app.meta.spark_open import SparkOpenClient

FACTS = {
    "type": "object",
    "properties": {
        "count": {"type": ["integer", "null"], "minimum": 0},
        "depth": {"type": "string", "enum": ["knee", "waist"]},
        "hazards": {"type": "array", "items": {"type": "string", "enum": ["flood", "wind"]}},
        "ok": {"type": "boolean"},
        "share": {"type": "number", "minimum": 0, "maximum": 100},
    },
    "required": ["count", "depth", "hazards", "ok", "share"],
    "additionalProperties": False,
}
GOOD = {"count": 3, "depth": "knee", "hazards": ["flood"], "ok": True, "share": 12.5}


def test_valid_output_passes_including_nulls_and_whole_floats():
    validate(FACTS, GOOD)
    validate(FACTS, {**GOOD, "count": None})
    validate(FACTS, {**GOOD, "count": 4.0})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"count": -1}, r"\$\.count: -1 is below 0"),
        ({"count": 2.5}, r"\$\.count: expected integer/null"),
        ({"count": True}, r"\$\.count: expected integer/null"),
        ({"depth": "ankle"}, r"\$\.depth: 'ankle' is not one of"),
        ({"hazards": ["flood", "fire"]}, r"\$\.hazards\[1\]"),
        ({"ok": "yes"}, r"\$\.ok: expected boolean"),
        ({"share": 150}, r"\$\.share: 150 is above 100"),
        ({"extra": 1}, r"unexpected extra"),
    ],
)
def test_invalid_output_names_the_field(change, message):
    with pytest.raises(ModelOutputError, match=message):
        validate(FACTS, {**GOOD, **change})


def test_missing_required_field():
    bad = dict(GOOD)
    del bad["ok"]
    with pytest.raises(ModelOutputError, match="missing ok"):
        validate(FACTS, bad)


@pytest.mark.parametrize(
    ("name", "client", "model"),
    [
        ("muse-spark-1.3", SparkClient, "muse-spark-1.3"),
        ("muse-spark-open", SparkOpenClient, "meta-models/muse-spark-open"),
    ],
)
def test_agent_llm_setting_picks_the_backend(settings, name, client, model):
    llm = make_agent_llm(settings.model_copy(update={"agent_llm": name}))
    assert isinstance(llm, client)
    assert llm.model == model


def test_spark_13_is_the_default_and_unknown_names_are_refused():
    assert Settings(_env_file=None).agent_llm == "muse-spark-1.3"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_llm="gpt-4")
