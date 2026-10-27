# (Project name TBD)

> An entry for the **Meta Global AI Developer Hackathon** (Oct 27 – Nov 4, 2026), built for the Philippines with Meta's Muse models.

**Status:** building; slice 1 (scaffold and intake) done. Typhoon and flood damage reporting for Philippine LGUs, plus crop and fisheries insurance claims: one agent turns photos, Taglish voice notes, and texts from barangays, farmers, and fisherfolk into an officer-approved RDANA report, a damaged-houses table, and PCIC Notices of Loss. Demo area: Biñan City, Laguna.

---

## The problem

_TBD: one paragraph on who suffers, how much, and why now, with sourced numbers._

## What it does

_TBD: the core flow in 3–5 steps._

## Meta models used

| Model | What it does in this project | Status |
|---|---|---|
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Turns Telegram voice notes (Tagalog, English, Taglish) into text for the report | Client ready, slice 1 (wired into intake) |
| Muse Spark 1.3 (`muse-spark-1.3`) | Judges each photo as structured JSON (roof damage level, flood, debris, tarpaulin, house count) | Client ready, slice 1 |
| SAM 3.1 (`sam-3.1`) | Locates and measures what Spark describes: roof and floodwater masks, boxes, counts, area fractions | Client ready, slice 1 |
| Muse Spark Open (`meta-models/muse-spark-open`, Baseten) | Open-weight model for the coordination agent steps | Client ready, slice 1 |

## Demo

- Video (3 min): _TBD_
- Live app: _TBD_
- X post: _TBD_

## Getting started

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `ffmpeg` (voice notes are converted to WAV; macOS: `brew install ffmpeg`).

```bash
uv sync
cp .env.example .env   # add META_API_KEY, TELEGRAM_BOT_TOKEN, PSEUDONYM_SALT (openssl rand -hex 32); BASETEN_API_KEY if used
uv run python -m app.bot                  # Telegram intake bot (long polling)
uv run uvicorn app.main:app --reload      # web app: /health and /reports
uv run pytest                             # tests (all HTTP is mocked, no API calls)
uv run ruff check . && uv run ruff format --check .
```

Other commands:

```bash
# Word error rate on your own recordings (folder OUTSIDE this repo: clip.ogg + clip.txt pairs)
uv run python -m scripts.eval_transcribe /path/to/recordings
# Delete expired raw media once (the bot and a running web app are not required)
uv run python -m app.retention
```

Model responses are cached on disk under `DATA_DIR/model_cache` when `MODEL_CACHE=true`, so repeated development runs do not spend credits. Raw photos and voice notes are deleted after `RAW_MEDIA_TTL_HOURS` (default 24); reports and transcripts are kept.

## Built during the hackathon

Everything in this repository was created during the hackathon (Oct 27 – Nov 4, 2026). This log records what was built and when.

| Date | What was built |
|---|---|
| 2026-10-26 | Repo scaffold: AGENTS.md, README.md |
| 2026-10-27 | Project scope: added floods and PCIC crop/fisheries Notices of Loss as outputs of the reporting agent; chose Biñan, Laguna as the demo area and Python/FastAPI as the stack |
| 2026-10-27 | Slice 1: Python/uv project; clients for Voice Transcribe, Spark 1.3, SAM 3.1 and Spark Open with a shared response cache and call log; SQLite storage with pseudonymous reporters; intake service and Telegram bot for text, photo, voice and location; raw-media retention purge; `/health` and `/reports` pages; transcription WER eval script; mocked-HTTP tests |

### Third-party code and assets

Licenses read from each installed package's metadata. No third-party source code or assets are copied into this repository; these are installed dependencies (see `pyproject.toml` and `uv.lock`).

| Name | License | Used for |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | Web app |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server |
| [SQLModel](https://github.com/fastapi/sqlmodel) | MIT | SQLite tables |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | MIT | Configuration from env/.env |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause | HTTP client for the model APIs |
| [Jinja2](https://github.com/pallets/jinja) | BSD-3-Clause | Server-rendered pages |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | LGPL-3.0-only | Telegram bot (used unmodified, as a library) |
| [jiwer](https://github.com/jitsi/jiwer) | Apache-2.0 | Word error rate for the transcription eval |
| [meta-sam-parser](https://pypi.org/project/meta-sam-parser/) | SAM License (Meta) | Official parser for SAM 3.1 output |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | Tests (dev) |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Apache-2.0 | Async tests (dev) |
| [respx](https://github.com/lundberg/respx) | BSD-3-Clause | HTTP mocking in tests (dev) |
| [Ruff](https://github.com/astral-sh/ruff) | MIT | Lint and format (dev) |
| [FFmpeg](https://ffmpeg.org) (external program, not bundled) | LGPL/GPL depending on build | Voice note conversion to WAV |

## Repo structure

```
AGENTS.md              Instructions for AI coding agents (Muse Code, Claude Code, …)
README.md              This file
pyproject.toml         Python project and tool config (uv)
.env.example           Names of the environment variables (no secrets)
app/
  config.py            Settings from env/.env
  cache.py             Content-hash disk cache for model responses
  meta/                Clients: transcribe.py, spark.py, sam.py, spark_open.py (+ base.py, factory.py)
  models.py, db.py     SQLModel tables (Reporter, Report, Media, ModelCall) and SQLite setup
  privacy.py           HMAC pseudonymization of reporter ids
  audio.py             ffmpeg conversion and chunking for voice notes
  intake.py            Ingest text, photo, voice and location into reports
  retention.py         Purge raw media after the TTL
  bot.py               Telegram bot (python -m app.bot)
  main.py, templates/  FastAPI app with /health and /reports
  evaluation.py        WER helpers
scripts/
  eval_transcribe.py   WER on a folder of your own recordings (outside the repo)
tests/                 Mocked-HTTP tests
```

## Team

- Ronnie Cutamora

## License

_TBD (choose before submission; the repo must be public)._
