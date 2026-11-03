"""The public environment template stays complete and safe to copy."""

import re
from pathlib import Path

from app.config import Settings
from app.deploy_targets import TARGETS

ROOT = Path(__file__).resolve().parent.parent


def test_example_lists_settings_and_deploy_inputs_with_empty_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    entries = re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=([^\n]*)$", text, re.MULTILINE)
    names = {name for name, _ in entries}
    expected = {name.upper() for name in Settings.model_fields}
    expected |= {source for target in TARGETS.values() for source in target.sources.values()}
    expected |= {"MODAL_BIN", "MODAL_PYTHON", "ULAT_TARGET", "ULAT_WORKSPACE"}
    assert expected <= names
    assert all(not value.strip() for _, value in entries)


def test_copying_example_keeps_application_defaults(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    settings = Settings(_env_file=ROOT / ".env.example")
    defaults = Settings(_env_file=None)
    assert settings.model_dump() == defaults.model_dump()
