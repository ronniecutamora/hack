"""Repeatable typhoon demo scenario: synthetic reports fed through the real pipeline."""

from pathlib import Path

from app.config import Settings

SCENARIO_PATH = Path(__file__).parent.parent.parent / "data" / "demo" / "scenario.json"


class DemoDirError(ValueError):
    """DATA_DIR does not point at a demo database."""


def require_demo_dir(settings: Settings, command: str) -> Path:
    """The demo commands only touch a data dir named "demo" (default: var/demo).

    Returns the resolved data dir. Raises DemoDirError naming the fix otherwise.
    """
    data_dir = settings.data_dir.resolve()
    if data_dir.name != "demo":
        raise DemoDirError(
            f"Refusing: DATA_DIR is '{settings.data_dir}' (resolves to '{data_dir}'), "
            "not a demo database. Run with: "
            f"DATA_DIR=var/demo uv run python -m app.demo {command}"
        )
    return data_dir
