"""Exercise the local webhook and dashboard with synthetic, cache-only model replies.

This starts an isolated loopback server and never contacts Telegram or a model provider. The
fixture cache uses the real clients' cache-only path; its responses are synthetic test data,
not recorded model accuracy. Requires the development dependencies and ffmpeg.
"""

import io
import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from PIL import Image
from sqlmodel import Session, select

from app import bot
from app.cache import ResponseCache
from app.config import Settings
from app.demo.images import jobs_for
from app.demo.loader import load_command
from app.demo.scenario import load_scenario
from app.main import create_app
from app.models import Media, ModelCall, Report, Reporter
from app.reporter_summary import Acknowledgments
from app.telegram_webhook import SECRET_HEADER, webhook_path
from tests.fake_telegram import FakeTelegram, text_update
from tests.test_assessment import facts
from tests.test_incidents import DIFFERENT
from tests.test_photo import JUDGMENT
from tests.test_rdana import cite_all
from tests.test_sam import STREAM

SCENARIO = Path(__file__).resolve().parent.parent / "tests/fixtures/demo_scenario_small.json"
SECRET = "smoke" * 8
TOKEN = "123456:synthetic-smoke-token"
PASSCODE = "synthetic-smoke-passcode"


class FixtureCache(ResponseCache):
    """Populate a disposable cache with synthetic replies, then use the real cache-only path."""

    def get(self, model: str, payload: Any) -> Any | None:
        hit = super().get(model, payload)
        if hit is not None:
            return hit
        if model == "sam-3.1":
            reply = {"text": STREAM}
        elif model == "muse-voice-transcribe-1.0":
            reply = {"transcript": "Baha sa Malaban", "audioDurationMs": 1000, "turns": []}
        elif model == "muse-spark-1.3":
            schema = payload["schema_name"]
            if schema == "DamageReportFacts":
                value = facts(hazards=["flood"], flood_depth="knee")
            elif schema == "DamageJudgement":
                value = JUDGMENT
            elif schema == "SameIncident":
                value = DIFFERENT
            elif schema == "RdanaSummary":
                value = cite_all(json.loads(payload["user_text"]))
            else:
                raise AssertionError(f"No synthetic cached fixture for schema {schema}")
            reply = {"choices": [{"message": {"content": json.dumps(value)}}]}
        else:
            raise AssertionError(f"No synthetic cached fixture for model {model}")
        self.put(model, payload, reply)
        return super().get(model, payload)


class LocalTelegram(FakeTelegram):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files

    def _result(self, name: str, params: dict[str, Any]) -> Any:
        if name == "getFile":
            file_id = params["file_id"]
            if file_id not in self.files:
                raise AssertionError(f"Unknown synthetic file: {file_id}")
            return {"file_id": file_id, "file_unique_id": file_id, "file_path": file_id}
        return super()._result(name, params)

    async def do_request(self, url: str, method: str, request_data=None, **kwargs):
        file_id = url.rsplit("/", 1)[-1]
        if "/file/bot" in url and file_id in self.files:
            return 200, self.files[file_id]
        return await super().do_request(url, method, request_data=request_data, **kwargs)


def media_files(folder: Path) -> dict[str, bytes]:
    photo = io.BytesIO()
    Image.new("RGB", (1000, 500), "gray").save(photo, "JPEG")
    voice, video = folder / "voice.ogg", folder / "video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "1",
            "-threads",
            "1",
            str(voice),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=8",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            "-threads",
            "1",
            str(video),
        ],
        check=True,
    )
    return {
        "photo.jpg": photo.getvalue(),
        "voice.ogg": voice.read_bytes(),
        "video.mp4": video.read_bytes(),
    }


def cache_only(client: Any, cache: FixtureCache) -> None:
    client.cache = cache
    client.cache_only = True


def update(kind: str, number: int, files: dict[str, bytes]) -> dict[str, Any]:
    payload = text_update("Baha sa Malaban", update_id=number, user_id=4200 + number)
    message = payload["message"]
    if kind == "text":
        return payload
    del message["text"]
    file_id = {"photo": "photo.jpg", "voice": "voice.ogg", "video": "video.mp4"}[kind]
    item = {"file_id": file_id, "file_unique_id": file_id, "file_size": len(files[file_id])}
    if kind == "photo":
        item.update(width=1000, height=500)
        message[kind] = [item]
        message["caption"] = "Baha sa Malaban"
    else:
        item["duration"] = 1 if kind == "voice" else 2
        if kind == "video":
            item.update(width=160, height=90, mime_type="video/mp4")
            message["caption"] = "Baha sa Malaban"
        else:
            item["mime_type"] = "audio/ogg"
        message[kind] = item
    return payload


def wait_until(predicate, description: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {description}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ulat-smoke-") as temp:
        folder = Path(temp)
        settings = Settings(
            _env_file=None,
            data_dir=folder / "demo",
            meta_api_key="synthetic",
            telegram_mode="webhook",
            telegram_bot_token=TOKEN,
            telegram_webhook_secret=SECRET,
            pseudonym_salt="synthetic-salt",
            allowed_hosts=["127.0.0.1", "localhost"],
            officer_passcode=PASSCODE,
            session_secret="s" * 40,
            video_keyframes=2,
        )
        files = media_files(folder)
        telegram = LocalTelegram(files)
        application = bot.build_application(settings, request=telegram)
        application.bot_data["acks"] = Acknowledgments(quiet_s=0)
        assessor = application.bot_data["assessor"]
        transcriber = application.bot_data["intake"].transcriber
        cache = FixtureCache(settings.cache_dir)
        for client in (assessor.llm, assessor.sam, assessor.incidents.judge, transcriber):
            cache_only(client, cache)
        for job in jobs_for(load_scenario(SCENARIO), settings.data_dir / "images"):
            job.path.parent.mkdir(parents=True, exist_ok=True)
            job.path.write_bytes(files["photo.jpg"])
        if load_command(settings, scenario_path=SCENARIO, assessor=assessor) != 0:
            raise AssertionError("Synthetic demo failed to load")
        engine = application.bot_data["intake"].engine
        server_app = create_app(
            settings,
            telegram_application=application,
            summary_llm=assessor.llm,
            pair_judge=assessor.incidents.judge,
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(server_app, log_level="error", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            wait_until(lambda: server.started, "the local server")
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
            ) as http:
                login = http.post("/login", data={"passcode": PASSCODE}, follow_redirects=False)
                assert login.status_code == 303, login.text
                for i, kind in enumerate(("text", "photo", "voice", "video"), 1):
                    response = http.post(
                        webhook_path(SECRET),
                        json=update(kind, i, files),
                        headers={SECRET_HEADER: SECRET},
                    )
                    assert response.status_code == 200, (kind, response.status_code, response.text)

                def completed():
                    with Session(engine) as session:
                        reports = session.exec(
                            select(Report).where(Report.channel == "telegram")
                        ).all()
                        return len(reports) == 4 and all(r.status == "assessed" for r in reports)

                wait_until(completed, "four assessed webhook reports")
                with Session(engine) as session:
                    reports = session.exec(select(Report).where(Report.channel == "telegram")).all()
                    media = session.exec(select(Media)).all()
                    reporters = {r.id: r.pseudonym for r in session.exec(select(Reporter)).all()}
                    by_sender = {reporters[r.reporter_id]: r for r in reports}
                    intake = application.bot_data["intake"]
                    text, photo, voice, video = [
                        by_sender[intake.pseudonym(4200 + i)] for i in range(1, 5)
                    ]
                    assert text.text == "Baha sa Malaban"
                    assert voice.transcript == "Baha sa Malaban"
                    assert any(
                        m.report_id == photo.id and m.kind == "photo" and m.video_duration_s is None
                        for m in media
                    )
                    assert any(
                        m.report_id == video.id
                        and m.kind == "photo"
                        and m.video_duration_s is not None
                        for m in media
                    )
                for route, expected in (("/", "How Ulat works"), ("/review", "<h1>Review</h1>")):
                    response = http.get(route)
                    assert (
                        response.status_code == 200 and expected.lower() in response.text.lower()
                    ), (route, response.status_code, response.text[:300])
                switch = http.get("/accuracy?mode=technical", follow_redirects=False)
                assert switch.status_code == 303
                accuracy = http.get("/accuracy")
                assert accuracy.status_code == 200 and "How accurate is the agent?" in accuracy.text
                assert "Real typhoon photos" in accuracy.text
                response = http.post(
                    "/drafts", data={"barangay": "Malaban"}, follow_redirects=False
                )
                assert response.status_code == 303, response.text
                draft = http.get(response.headers["location"])
                assert draft.status_code == 200 and "Malaban" in draft.text
                # The print view is embedded in this same draft response and selected by print CSS.
                assert 'class="paper paper--rdana print-only"' in draft.text
                assert "Rapid Damage" in draft.text
                with Session(engine) as session:
                    calls = session.exec(select(ModelCall)).all()
                    assert calls and all(call.cached and call.cost_estimate == 0 for call in calls)
                print(
                    "SMOKE PASS: demo; text/photo/voice/video webhooks; Home, Review, draft, "
                    "print markup, Accuracy; all model calls cached; $0"
                )
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            sock.close()
            engine.dispose()
            if thread.is_alive():
                raise RuntimeError("Local smoke server did not stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
