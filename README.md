# (Project name TBD)

> An entry for the **Meta Global AI Developer Hackathon** (Oct 27 – Nov 4, 2026), built for the Philippines with Meta's Muse models.

**Status:** building; slice 1 (intake) and slice 2 (assessment and mapping) done. Typhoon and flood damage reporting for Philippine LGUs, plus crop and fisheries insurance claims: one agent turns photos, Taglish voice notes, and texts from barangays, farmers, and fisherfolk into an officer-approved RDANA report, a damaged-houses table, and PCIC Notices of Loss. Demo area: Biñan City, Laguna.

---

## The problem

_TBD: one paragraph on who suffers, how much, and why now, with sourced numbers._

## What it does

_TBD: the core flow in 3–5 steps._

## Meta models used

| Model | What it does in this project | Status |
|---|---|---|
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Turns Telegram voice notes (Tagalog, English, Taglish) into text, biased toward Biñan's 24 barangay names with `keywords` | In use |
| Muse Spark 1.3 (`muse-spark-1.3`) | The agent LLM (default): judges each photo as structured JSON (roof damage, floodwater, debris, fallen tree, tarpaulin, house count) and extracts report facts (flood depth, houses, evacuees, crops, needs) | In use |
| Muse Spark Open (`meta-models/muse-spark-open`, Baseten) | The same agent steps on open weights, photos included: set `AGENT_LLM=muse-spark-open` | In use |
| SAM 3.1 (`sam-3.1`) | Measures what the LLM judges: counts roofs and measures floodwater from masks, drawn on the photo as evidence | In use |

Every agent LLM call (photo judgment, fact extraction) goes through one interface, so the whole agent runs on either Spark 1.3 or Spark Open. Each call is logged with the model that served it.

## Demo

- Video (3 min): _TBD_
- Live app: _TBD_
- X post: _TBD_

## Getting started

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `ffmpeg` (voice notes are converted to WAV; macOS: `brew install ffmpeg`).

```bash
uv sync
cp .env.example .env   # add META_API_KEY, TELEGRAM_BOT_TOKEN, PSEUDONYM_SALT (openssl rand -hex 32); BASETEN_API_KEY for Spark Open
# AGENT_LLM=muse-spark-1.3 (default) or muse-spark-open (open weights on Baseten)
uv run python -m app.bot                  # Telegram intake bot (long polling)
uv run uvicorn app.main:app --reload      # web app: /health, /reports and /media/<id> (localhost only: no login yet)
uv run pytest                             # tests (all HTTP is mocked, no API calls)
uv run ruff check . && uv run ruff format --check .
```

Other commands:

```bash
# Assess reports again (the bot does this in the background); --llm compares backends
uv run python -m app.assessment --report 12 | --pending | --all [--llm muse-spark-open]
# Evals on your own labelled data (folders OUTSIDE this repo; each prints its cost first)
uv run python -m scripts.eval_transcribe /path/to/recordings --keywords barangays   # WER, raw and cleaned
uv run python -m scripts.eval_vision /path/to/photos --sam [--llm muse-spark-open]  # labels.csv + images/
uv run python -m scripts.eval_facts /path/to/recordings [--llm muse-spark-open]     # facts.json + audio
# Delete expired raw media once (the bot and a running web app are not required)
uv run python -m app.retention
```

Model responses are cached on disk under `DATA_DIR/model_cache` when `MODEL_CACHE=true`, so repeated development runs do not spend credits. Raw photos, voice notes and photo overlays are deleted after `RAW_MEDIA_TTL_HOURS` (default 24); reports, transcripts and assessments are kept. Names that reporters say when introducing themselves ("Ako po si …") are redacted before any text reaches a model.

## Built during the hackathon

Everything in this repository was created during the hackathon (Oct 27 – Nov 4, 2026). This log records what was built and when.

| Date | What was built |
|---|---|
| 2026-10-26 | Repo scaffold: AGENTS.md, README.md |
| 2026-10-27 | Project scope: added floods and PCIC crop/fisheries Notices of Loss as outputs of the reporting agent; chose Biñan, Laguna as the demo area and Python/FastAPI as the stack |
| 2026-10-27 | Slice 1: Python/uv project; clients for Voice Transcribe, Spark 1.3, SAM 3.1 and Spark Open with a shared response cache and call log; SQLite storage with pseudonymous reporters; intake service and Telegram bot for text, photo, voice and location; raw-media retention purge; `/health` and `/reports` pages; transcription WER eval script; mocked-HTTP tests |
| 2026-10-27 | Fixes from the first live bot test: voice notes were lost to a SQLite lock (the report is now committed before transcription, and WAL lets the bot and web app share the database); longer Telegram timeouts, retries for flaky Telegram connections, and a safe error reply; retention also deletes orphaned raw files |
| 2026-10-28 | Slice 2: Biñan barangay gazetteer and boundaries (OCHA COD-AB) with point-in-polygon and name matching; transcription keywords and repeated-word cleanup; one agent-LLM interface that runs every step on Spark 1.3 or open-weight Spark Open (`AGENT_LLM`), with schema validation; photo judgment, SAM roof and floodwater measurement and evidence overlays; fact extraction with name redaction; background assessment of every report with barangay mapping from pin, photo GPS or named place; reports page with evidence; vision and fact-extraction evals for both LLMs |

### Third-party code and assets

Licenses read from each installed package's metadata. No third-party source code is copied into this repository; the libraries are installed dependencies (see `pyproject.toml` and `uv.lock`). The one third-party data file is listed under Data below.

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
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | Photo EXIF GPS and SAM mask overlays |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | Tests (dev) |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Apache-2.0 | Async tests (dev) |
| [respx](https://github.com/lundberg/respx) | BSD-3-Clause | HTTP mocking in tests (dev) |
| [Ruff](https://github.com/astral-sh/ruff) | MIT | Lint and format (dev) |
| [FFmpeg](https://ffmpeg.org) (external program, not bundled) | LGPL/GPL depending on build | Voice note conversion to WAV |

Data:

| Name | License | Used for |
|---|---|---|
| [OCHA COD-AB Philippines](https://data.humdata.org/dataset/cod-ab-phl) admin level 4 (sources: PSA, NAMRIA), v03, valid on 2025-02-13 | [CC BY-IGO](https://creativecommons.org/licenses/by/3.0/igo/) | Barangay names, codes and boundaries for Biñan in `app/data/binan_barangays.geojson` (24 polygons, clipped and simplified to about 3 m). OCHA notes the boundaries are indicative, not official |

## Repo structure

```
AGENTS.md              Instructions for AI coding agents (Muse Code, Claude Code, …)
README.md              This file
pyproject.toml         Python project and tool config (uv)
.env.example           Names of the environment variables (no secrets)
app/
  config.py            Settings from env/.env
  cache.py             Content-hash disk cache for model responses
  meta/                Clients: transcribe.py, spark.py, sam.py, spark_open.py; llm.py (AgentLLM switch),
                       schema.py (output validation), base.py, factory.py
  models.py, db.py     SQLModel tables (Reporter, Report, Media, ModelCall, Assessment, PhotoAssessment)
  gazetteer.py         Biñan's 24 barangays: names, aliases, matching in text, transcription keywords
  geo.py, data/        Point-in-barangay lookup on COD-AB boundaries; photo EXIF GPS
  transcript_cleanup.py  Removes repeated-word artifacts from transcripts
  facts.py, photo.py   Fact extraction; photo judgment, SAM measurement and evidence overlay
  assessment.py        Assesses a report and maps it to a barangay (python -m app.assessment)
  privacy.py           HMAC pseudonymization of reporter ids
  audio.py             ffmpeg conversion and chunking for voice notes
  intake.py            Ingest text, photo, voice and location into reports
  retention.py         Purge raw media after the TTL
  bot.py               Telegram bot (python -m app.bot)
  main.py, templates/  FastAPI app with /health, /reports and /media/<id>
  evaluation.py        WER, Cohen's kappa and F1 helpers
scripts/
  eval_transcribe.py   WER on your own recordings (outside the repo)
  eval_vision.py       Photo judge and SAM vs hand labels, per agent LLM
  eval_facts.py        Fact extraction from voice notes vs hand labels, per agent LLM
tests/                 Mocked-HTTP tests
```

## Team

- Ronnie Cutamora

## License

_TBD (choose before submission; the repo must be public)._
