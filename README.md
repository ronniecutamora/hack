# (Project name TBD)

> An entry for the **Meta Global AI Developer Hackathon** (Oct 27 – Nov 4, 2026), built for the Philippines with Meta's Muse models.

**Status:** building; slices 1–3a (intake, assessment/mapping, incident coordination) implemented and locally tested. Typhoon and flood damage reporting for Philippine LGUs, plus crop and fisheries insurance claims: one agent turns photos, Taglish voice notes, and texts from barangays, farmers, and fisherfolk into an officer-approved RDANA report, a damaged-houses table, and PCIC Notices of Loss. Demo area: Biñan City, Laguna.

---

## The problem

_TBD: one paragraph on who suffers, how much, and why now, with sourced numbers._

## What it does

1. Residents send photos, voice notes, texts and location pins; the agent extracts damage facts and maps the report to a barangay.
2. Deterministic place/time/GPS filters find candidate incidents. A text-only Meta pair judge decides whether reports describe the same situation; uncertainty stays separate for officer review.
3. Officers read incident timelines, depth changes and the stored reasons for grouping. Reported totals explicitly warn about double-counting, including corrections.
4. RDANA, damaged-houses tables and PCIC Notice of Loss drafting remain planned outputs; this slice supplies their shared incident evidence.

## Meta models used

| Model | What it does in this project | Status |
|---|---|---|
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Turns Telegram voice notes (Tagalog, English, Taglish) into text, biased toward Biñan's 24 barangay names with `keywords` | In use |
| Muse Spark 1.3 (`muse-spark-1.3`) | The agent LLM (default): judges each photo as structured JSON (roof damage, floodwater, debris, fallen tree, tarpaulin, house count) and extracts report facts (flood depth, houses, evacuees, crops, needs); default incident pair judge | In use |
| Muse Spark Open (`meta-models/muse-spark-open`, Baseten) | The same agent steps and incident pair judgments on open weights, photos included: set `AGENT_LLM=muse-spark-open` | In use |
| Muse Spark Open (`accounts/fireworks/models/muse-spark-open`, Fireworks) | Agent or independent pair judge via `muse-spark-open-fireworks` | Implemented; mocked HTTP verified, no live call in this slice |
| Muse Glimmer (`accounts/fireworks/models/muse-glimmer-30b`, Fireworks) | Pair-judge comparison via `muse-glimmer` | Implemented; serverless availability ⚠️ contradictory and unverified; explicit availability errors, no fallback |
| Muse Image (`muse-image-1.0`) | Synthetic, clearly labelled demo photos for the typhoon scenario (no real homes or people); every image is stamped "AI-generated (Muse Image) · synthetic demo" | In use (demo data) |
| SAM 3.1 (`sam-3.1`) | Measures what the LLM judges: counts roofs and measures floodwater from masks, drawn on the photo as evidence | In use |

Every LLM call goes through `AgentLLM`. `AGENT_LLM` selects fact extraction and photo judgment; `PAIR_JUDGE_LLM` independently selects the incident judge and defaults to `AGENT_LLM` when omitted. Both accept `muse-spark-1.3`, `muse-spark-open` (Baseten), `muse-spark-open-fireworks` or `muse-glimmer` (Fireworks). Provider configuration comes from the local [Baseten skill](.agents/skills/baseten/SKILL.md) and [Fireworks skill](.agents/skills/fireworks/SKILL.md), which cite the provider sources. No live provider compatibility or accuracy is claimed by mocked tests.

Incident candidates share a barangay and activity within `INCIDENT_WINDOW_DAYS` (14 by default). GPS within 500 m can add candidates when a barangay is missing, and distance vetoes distant pairs when both sides have points. Known different barangays never match. Only `same` at or above `PAIR_MERGE_THRESHOLD` (0.85 by default) merges; unsure and low-confidence results remain separate with `needs_officer`. These are configurable policy choices, not calibrated accuracy claims. Pair prompts use typed facts, regenerated summaries and photo captions, and redacted place details; they omit raw text, transcripts, reporter pseudonyms, images and free-form photo notes.

Grouping runs after successful assessment under the same per-report lock, without a database transaction across a model call. A changed late pin re-links the report; an empty old group is retired from the list while its audit remains readable. Unchanged grouping is a no-op. `AgentDecision` stores the actual judge model, compared incident/report, confidence and reason; pages do not regenerate explanations. Judge failures stay visible for officer review and can be retried explicitly. Existing slice-2 SQLite files receive grouping columns on startup without losing reports.

## Demo

- Video (3 min): _TBD_
- Live app: _TBD_
- X post: _TBD_

## Getting started

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `ffmpeg` (voice notes are converted to WAV; macOS: `brew install ffmpeg`).

```bash
uv sync
cp .env.example .env   # add META_API_KEY, TELEGRAM_BOT_TOKEN, PSEUDONYM_SALT (openssl rand -hex 32); BASETEN_API_KEY for Baseten; FIREWORKS_API_KEY for Fireworks
# AGENT_LLM=muse-spark-1.3 (default); optional independent PAIR_JUDGE_LLM
uv run python -m app.bot                  # Telegram intake bot (long polling)
uv run uvicorn app.main:app --reload      # web app: /health, /reports, /incidents, /incidents/<id>, /media/<id> (localhost only: no login yet)
uv run pytest                             # tests (all HTTP is mocked, no API calls)
uv run ruff check . && uv run ruff format --check .
```

Other commands:

```bash
# Assess reports again (the bot does this in the background); --llm compares backends
uv run python -m app.assessment --report 12 | --pending | --all [--llm muse-spark-open]
# Group existing assessed reports; unchanged inputs are a no-op
uv run python -m app.incidents --all
uv run python -m app.incidents --report 12 --retry-failed
# Synthetic 64-pair judge eval (prompts before uncached paid calls; --yes skips confirmation)
uv run python -m scripts.eval_dedupe --judge muse-spark-1.3
uv run python -m scripts.eval_dedupe --judge muse-spark-open
uv run python -m scripts.eval_dedupe --judge muse-spark-open-fireworks
uv run python -m scripts.eval_dedupe --judge muse-glimmer
# Optional first positional argument: a labelled pairs.json OUTSIDE the public repo
# --llm is an alias for --judge; P/R/F1 use the production merge threshold.
# Synthetic demo: a typhoon scenario in a separate database (var/demo)
DATA_DIR=var/demo uv run python -m app.demo images   # Muse Image photos; shows the cost and asks first
DATA_DIR=var/demo uv run python -m app.demo load     # runs every report through the real pipeline
DATA_DIR=var/demo uv run uvicorn app.main:app        # dashboard on the demo data
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
| 2026-10-28 | Fixes from a code review of slice 2: invalid model replies are no longer cached; place names that are also surnames, words or saints need a place cue ("sa", "taga", ...) and skip chapels and schools; the transcript cleanup keeps numbers, "Soro soro" and "kanya kanya"; untranscribed voice notes and interrupted runs stay pending; oversized photos are handled and Pillow runs off the event loop; name redaction covers titles ("Kap. …") and "my name is" / "ang pangalan ko" |
| 2026-10-28 | Shared OpenAI-compatible client for structured JSON, validation before caching and length retries; Baseten remains a thin provider configuration |
| 2026-10-28 | Fireworks Spark Open and Glimmer provider configurations, independently selected pair judge, and mocked structured-output/auth/cache/retry checks; no live availability claims |
| 2026-10-28 | Incident grouping after assessment: place/time/GPS candidates, text-only pair judge, immutable decisions, officer-review uncertainty, deterministic rollups and idempotent late-pin re-linking; SQLite upgrade and rebuild CLI |
| 2026-10-28 | Officer incident list and chronological evidence timelines with persisted grouping explanations, incident links from reports, and synthetic-demo badges |
| 2026-10-28 | Cached pair-judge eval CLI with cost confirmation, precision/recall/F1 and review-rate reporting, plus 64 synthetic labelled pairs (paraphrased places and near-identical hard negatives; exact string matching scores 62.5%) and fake-judge verification; source lint/format excludes the read-only provider skill references; no paid eval run |
| 2026-10-28 | Fixes from a review of the incident work: a grouping error no longer marks the assessment failed; a "different" verdict moves on to older incidents; names after ni/nina/kay/kina and titles are redacted from places; the client refactor keeps the old cache keys and accepts complete answers that end at the token limit |
| 2026-10-28 | Synthetic demo scenario: a Muse Image client, 17 Taglish reports over two weeks after landfall (a street flooding and receding, a correction, an ambiguous and a placeless report, a fisherfolk's broken fish cages), labelled synthetic photos, and a loader into a separate demo database through the real intake and assessment pipeline |
| 2026-10-28 | Demo geolocation fix: ambiguous text or transcript stays unresolved despite a single model-suggested barangay; valid pins and photo GPS retain precedence, with offline regression tests |
| 2026-10-28 | Demo place anchors: optional shared places keep deterministic report pins within 30 m of one anchor inside their barangay; scenario validation requires each series to share a place and barangay, with offline geometry and intake tests |
| 2026-10-28 | Demo place descriptions: varied Taglish names one synthetic Malaban corner, one Casile purok and one Poblacion evacuation court across follow-ups; the ankle-to-waist-to-ankle story and unpinned two-barangay report are regression-tested |
| 2026-10-28 | Demo fisherfolk correction: both reports now name and pin De La Paz with a shared synthetic lakeshore fish-pen place; the photo prompt is unchanged and scenario checks cover the location |

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
.agents/skills/        Provider notes for coding agents: baseten, fireworks, modal (SKILL.md each)
README.md              This file
pyproject.toml         Python project and tool config (uv)
.env.example           Names of the environment variables (no secrets)
app/
  config.py            Settings from env/.env
  cache.py             Content-hash disk cache for model responses
  meta/                Clients: transcribe.py, spark.py, sam.py, spark_open.py, fireworks.py;
                       openai_compatible.py (shared chat), llm.py (AgentLLM switch),
                       schema.py (output validation), base.py, factory.py
  models.py, db.py     SQLModel tables including Incident and immutable AgentDecision
  gazetteer.py         Biñan's 24 barangays: names, aliases, matching in text, transcription keywords
  geo.py, data/        Point-in-barangay lookup on COD-AB boundaries; photo EXIF GPS
  transcript_cleanup.py  Removes repeated-word artifacts from transcripts
  facts.py, photo.py   Fact extraction; photo judgment, SAM measurement and evidence overlay
  assessment.py        Assesses a report and maps it to a barangay (python -m app.assessment)
  incidents.py         Candidates, deterministic rollups, audited grouping and rebuild CLI
  pair_judge.py        Text-only pair schema and redacted evidence prompts
  demo/                Synthetic demo: scenario, Muse Image photos, loader (python -m app.demo)
  privacy.py           HMAC pseudonymization of reporter ids
  audio.py             ffmpeg conversion and chunking for voice notes
  intake.py            Ingest text, photo, voice and location into reports
  retention.py         Purge raw media after the TTL
  bot.py               Telegram bot (python -m app.bot)
  main.py, templates/  FastAPI officer pages: reports, incidents, timelines, decision audit and media
  evaluation.py        WER, Cohen's kappa and F1 helpers
scripts/
  eval_transcribe.py   WER on your own recordings (outside the repo)
  eval_vision.py       Photo judge and SAM vs hand labels, per agent LLM
  eval_facts.py        Fact extraction from voice notes vs hand labels, per agent LLM
  eval_dedupe.py       Cached pair-judge P/R/F1, review rate and per-pair misses
data/demo/scenario.json  The synthetic typhoon scenario (no real people)
tests/                 Mocked-HTTP tests and synthetic dedupe_pairs.json (64 labelled pairs)
```

## Team

- Ronnie Cutamora

## License

_TBD (choose before submission; the repo must be public)._
