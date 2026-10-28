import io
import json
from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from app.assessment import AssessmentService
from app.config import Settings
from app.db import init_db, make_engine
from app.demo import DemoDirError, require_demo_dir
from app.demo.geo import pick_point
from app.demo.images import images_command, jobs_for
from app.demo.loader import CHANNEL, MANIFEST_NAME, load_command
from app.demo.scenario import LANDFALL_AT, load_scenario
from app.gazetteer import BARANGAYS, BY_NAME
from app.geo import barangay_at
from app.models import Report
from tests.test_assessment import FakeLLM, FakeSam

FIXTURE = Path(__file__).parent / "fixtures" / "demo_scenario_small.json"


def demo_settings(tmp_path, name="demo"):
    return Settings(
        _env_file=None,
        meta_api_key="test-meta-key",
        baseten_api_key="test-baseten-key",
        pseudonym_salt="test-salt",
        data_dir=tmp_path / name,
    )


def engine_for(settings):
    engine = make_engine(settings)
    init_db(engine)
    return engine


def gray_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (128, 128, 128)).save(buffer, "JPEG")
    return buffer.getvalue()


def write_fixture_images(data_dir: Path) -> None:
    for job in jobs_for(load_scenario(FIXTURE), data_dir / "images"):
        job.path.parent.mkdir(parents=True, exist_ok=True)
        job.path.write_bytes(gray_jpeg())


def assessor_for(settings, engine):
    return AssessmentService(settings, engine, FakeLLM(), FakeSam())


def reports(engine):
    with Session(engine) as session:
        return session.exec(select(Report).order_by(Report.id)).all()


# the DATA_DIR guard


def test_require_demo_dir_accepts_only_a_demo_dir(tmp_path):
    assert require_demo_dir(demo_settings(tmp_path), "load").name == "demo"
    with pytest.raises(DemoDirError, match=r"DATA_DIR=var/demo uv run python -m app.demo load"):
        require_demo_dir(demo_settings(tmp_path, "var"), "load")


def test_load_refuses_a_non_demo_data_dir(tmp_path, capsys):
    assert load_command(demo_settings(tmp_path, "var")) == 2
    assert "DATA_DIR=var/demo" in capsys.readouterr().out


def test_images_refuses_a_non_demo_data_dir(tmp_path, capsys):
    assert images_command(demo_settings(tmp_path, "var")) == 2
    assert "DATA_DIR=var/demo" in capsys.readouterr().out


# deterministic pins


def test_pick_point_is_deterministic_and_inside(tmp_path):
    first = pick_point("Malaban", "r01")
    assert pick_point("Malaban", "r01") == first
    assert pick_point("Malaban", "r02") != first
    assert barangay_at(*first) == BY_NAME["Malaban"]


def test_every_barangay_picks_a_point_inside_itself():
    assert len(BARANGAYS) == 24
    for barangay in BARANGAYS:
        assert barangay_at(*pick_point(barangay.name, "probe")) == barangay, barangay.name


# the loader


def test_loader_ingests_backdates_assesses_and_manifests(tmp_path):
    settings = demo_settings(tmp_path)
    engine = engine_for(settings)
    write_fixture_images(settings.data_dir)

    out = load_command(settings, scenario_path=FIXTURE, assessor=assessor_for(settings, engine))
    assert out == 0

    rows = reports(engine)
    assert len(rows) == 3
    assert {r.channel for r in rows} == {CHANNEL}
    assert [r.received_at for r in rows] == [
        LANDFALL_AT.replace(day=3, hour=19, minute=10),
        LANDFALL_AT.replace(day=4, hour=8, minute=10),
        LANDFALL_AT.replace(day=5, hour=9, minute=10),
    ]
    assert [r.barangay for r in rows] == ["Malaban", "Casile", None]
    assert [r.status for r in rows] == ["assessed"] * 3
    manifest = json.loads((settings.data_dir / MANIFEST_NAME).read_text())
    expected = {s.key: r.id for s, r in zip(load_scenario(FIXTURE), rows, strict=True)}
    assert manifest == expected


def test_loader_is_idempotent(tmp_path, capsys):
    settings = demo_settings(tmp_path)
    engine = engine_for(settings)
    write_fixture_images(settings.data_dir)
    kwargs = {"scenario_path": FIXTURE, "assessor": assessor_for(settings, engine)}

    assert load_command(settings, **kwargs) == 0
    capsys.readouterr()
    assert load_command(settings, **kwargs) == 0

    assert len(reports(engine)) == 3
    assert "(3 already loaded)" in capsys.readouterr().out


def test_loader_needs_images_first(tmp_path, capsys):
    settings = demo_settings(tmp_path)
    engine_for(settings)

    assert load_command(settings, scenario_path=FIXTURE, assessor=None) == 1

    out = capsys.readouterr().out
    assert "python -m app.demo images" in out
    assert len(reports(make_engine(settings))) == 0


def test_reset_deletes_only_the_demo_dir(tmp_path):
    settings = demo_settings(tmp_path)
    engine = engine_for(settings)
    (settings.data_dir / "sentinel.txt").write_text("wipe me")
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("mine")

    # Reset wipes the whole demo dir (images included), so the load stops there.
    assert (
        load_command(
            settings,
            reset=True,
            assume_yes=True,
            scenario_path=FIXTURE,
            assessor=assessor_for(settings, engine),
        )
        == 1
    )
    assert not (settings.data_dir / "sentinel.txt").exists()
    assert (other / "keep.txt").is_file()

    # Regenerate the images, then the load succeeds on the fresh dir.
    write_fixture_images(settings.data_dir)
    fresh = engine_for(settings)
    assert (
        load_command(settings, scenario_path=FIXTURE, assessor=assessor_for(settings, fresh)) == 0
    )
    assert (settings.data_dir / MANIFEST_NAME).is_file()
    assert (other / "keep.txt").is_file()


def test_reset_asks_first(tmp_path, capsys, monkeypatch):
    settings = demo_settings(tmp_path)
    engine_for(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "sentinel.txt").write_text("stay")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert load_command(settings, reset=True, scenario_path=FIXTURE) == 0

    assert (settings.data_dir / "sentinel.txt").is_file()
    assert "Cancelled" in capsys.readouterr().out


def test_images_command_reports_cost_and_waits(tmp_path, capsys, monkeypatch):
    settings = demo_settings(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert images_command(settings) == 0

    out = capsys.readouterr().out
    assert "to generate" in out and "$" in out
    assert list((settings.data_dir / "images").glob("*.jpg")) == []
