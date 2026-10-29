"""Repeatable typhoon demo scenario: synthetic reports fed through the real pipeline."""

from pathlib import Path

from app.config import Settings

SCENARIO_PATH = Path(__file__).parent.parent.parent / "data" / "demo" / "scenario.json"


class DemoDirError(ValueError):
    """DATA_DIR does not point at a demo database."""


def is_demo_dir_name(name: str) -> bool:
    """A demo data dir name: "demo", or a named variant such as "demo-replay"."""
    return name == "demo" or name.startswith("demo-")


def require_demo_dir(settings: Settings, command: str) -> Path:
    """The demo commands only touch a data dir named "demo" or "demo-<name>" (default: var/demo).

    Returns the resolved data dir. Raises DemoDirError naming the fix otherwise.
    """
    data_dir = settings.data_dir.resolve()
    if not is_demo_dir_name(data_dir.name):
        raise DemoDirError(
            f"Refusing: DATA_DIR is '{settings.data_dir}' (resolves to '{data_dir}'), "
            "not a demo database. Run with: "
            f"DATA_DIR=var/demo uv run python -m app.demo {command}"
        )
    return data_dir
