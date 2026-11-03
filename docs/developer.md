# Developer guide

## Demo

- **Live officer dashboard:** <https://ronniecutamora47--ulat.modal.run> (production, on Modal since 2026-10-30).
  It needs the officer passcode: judges get it in the submission's private judging notes. It starts with the
  synthetic typhoon scenario below (no real people), plus whatever has been sent to the bot since.
- **Telegram bot:** [@UlatPH_bot](https://t.me/UlatPH_bot). A prototype, not run or monitored by Biñan City: don't
  use it for emergencies, **call 911**. Real messages are processed by Meta's models; raw photos and voice notes are
  deleted after 24 hours (at most 72 while processing is paused by the spend cap).
- Demo video: [YouTube link: added at submission]
- X post: _TBD_

### Detailed demo path

1. Open the dashboard and sign in with the passcode (initials are optional). Home opens in the **Simple** view: the
   officer's to-do list, most urgent first, with a short "How Ulat works" guide. Then switch to **Technical** from
   the account menu (top right, View), or open any page with `?mode=technical`: the overview shows the scenario's
   incidents, the review queue, drafts, the filing deadline, a map and "Meta models at work" (the last 24 hours, so
   it fills up once you send a report).
2. Open **Incidents** and the Malaban flood: its timeline shows the water rising from ankle- to waist-deep and going
   down again, the agent's reasons for grouping each report, and (Technical view) an **agent trace** per report
   (which Meta model ran each step, its time and cost).
3. Open **Drafts**: generate the RDANA draft for a barangay (one Spark 1.3 call writes its 1.4 summary; every number is
   checked against the evidence) and approve or reject it. Then generate the San Antonio fish-pen **Notice of
   Loss**: a red "PHOTO REUSED" banner links the De La Paz report that sent the same photo first. The filing clocks
   count from when the scenario was seeded, so by now they may read "overdue".
4. Message @UlatPH_bot from your phone, for example `Baha dito sa Malaban, hanggang tuhod, 3 bahay nasira`, or a
   photo, short video, audio file, voice note or location pin. It answers "received", then a summary of what the agent understood; the report
   appears on the open dashboard live, mapped to its barangay. Reply to the summary to correct it.
5. **Review** lists what the agent could not settle (an ambiguous place, a reused photo, a fallen tree to confirm);
   **Accuracy** (Technical view) charts how often each model is right on the labeled sets: the roof-damage confusion
   matrix against hand labels, F1 per flag, the duplicate check, the not-a-report screen and Voice Transcribe.

The synthetic scenario ([data/demo/scenario.json](../data/demo/scenario.json), no real people) has 18 Taglish reports by default (20 when rice is enabled)
over two weeks after a fictional typhoon: a Malaban street flooding ankle-, knee-, then waist-deep and receding; a
Casile roof count corrected from five houses to three; an evacuation center in Poblacion; an ambiguous report naming
two barangays and one with no place at all; a reporter who introduces himself as "Juan dela Cruz" (redacted); a
De La Paz fisherfolk's broken fish pens and a follow-up asking for nets and bamboo, which should become one incident
with one Notice of Loss; and a new reporter in San Antonio who claims fish-pen damage with the De La Paz photo
(`"photo_from": "r05"`, the same file byte for byte), so the reused-photo warning shows in red on that Notice of
Loss. The loader assesses each report as it arrives, as the bot does, so only the later copy is flagged.
The fish-pen reports come late in the scenario (De La Paz on day 13 afternoon, its follow-up on day 14 morning, the
San Antonio copy on day 14 afternoon). `--anchor-now` moves every scenario time so the latest report arrives an hour
before the load, so both Notices of Loss show a live "due in" countdown: about 47 hours for San Antonio and about 22
hours for De La Paz, first reported the afternoon before. The command refuses to anchor on top of an existing load;
use it with `--reset`. On a deploy, `uv run python -m app.deploy reseed --target staging` replaces the demo with a
fresh anchored load and keeps every real report ([deploy/README.md](../deploy/README.md), "Before a demo"). A reset deletes the demo databases, uploads and manifest but keeps the generated images and the
model cache (`var/demo/model_cache`), so a reload of the same scenario reuses the paid photo judgments and SAM calls.

## Getting started

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `ffmpeg` (voice notes are converted to WAV; macOS: `brew install ffmpeg`).

```bash
uv sync
cp .env.example .env   # add META_API_KEY, TELEGRAM_BOT_TOKEN, PSEUDONYM_SALT (openssl rand -hex 32); BASETEN_API_KEY for Baseten; FIREWORKS_API_KEY for Fireworks
# AGENT_LLM=muse-spark-1.3 (default); optional independent PAIR_JUDGE_LLM
uv run python -m app.bot                  # Telegram intake bot (long polling; use your own bot's token: @UlatPH_bot is on the production webhook, and polling refuses to start while a webhook is set)
uv run python -m app.bot_profile --dry-run  # bot name, descriptions and commands: print what would change
uv run python -m app.bot_profile --photo    # apply only what differs, and upload assets/brand/ulat-avatar.png
uv run uvicorn app.main:app --reload      # web app: / (Home: to-do list, or the overview in Technical mode), /help, /reports/set-aside, /review, /incidents, /reports, /map, /drafts, /damaged-houses (.csv), /accuracy, /media/<id>, /health, live updates at /events (no login on localhost unless OFFICER_PASSCODE is set)
uv run python -m scripts.smoke            # isolated local webhook + dashboard, synthetic cached fixtures, no model spend
uv run pytest                             # tests (all HTTP is mocked, no API calls)
uv run ruff check . && uv run ruff format --check .
```

The smoke test creates a temporary demo and loopback server, sends synthetic webhook updates,
and checks the draft's embedded print markup. Its model clients are cache-only and use synthetic
fixtures, so it spends nothing and does not establish live provider or browser print fidelity.

Other commands:

```bash
# Assess reports again (the bot does this in the background); --llm compares backends
uv run python -m app.assessment --report 12 | --pending | --all [--llm muse-spark-open]
# Screen stored messages with the free not-a-report rule only (no model call; idempotent)
uv run python -m app.assessment --screen
# Group existing assessed reports; unchanged inputs are a no-op
uv run python -m app.incidents --all
uv run python -m app.incidents --report 12 --retry-failed
# Synthetic 64-pair judge eval (prompts before uncached paid calls; --yes skips confirmation)
uv run python -m scripts.eval_dedupe --judge muse-spark-1.3
uv run python -m scripts.eval_dedupe --judge muse-spark-open
uv run python -m scripts.eval_dedupe --judge muse-spark-open-fireworks
uv run python -m scripts.eval_dedupe --judge muse-glimmer
# Optional first positional argument: a labeled pairs.json OUTSIDE the public repo
# --llm is an alias for --judge; P/R/F1 use the production merge threshold.
# Synthetic demo: a typhoon scenario in a separate database (var/demo)
DATA_DIR=var/demo uv run python -m app.demo images   # Muse Image photos; shows the cost and asks first
DATA_DIR=var/demo uv run python -m app.demo load     # runs every report through the real pipeline
DATA_DIR=var/demo uv run python -m app.demo load --reset --anchor-now  # fresh load (keeps images and model cache), times end an hour ago
DATA_DIR=var/demo uv run uvicorn app.main:app        # dashboard on the demo data
uv run python -m app.demo replay [--speed 7200] [--reset]  # live replay into var/demo-replay at real time (~3 min); refuses uncached paid calls unless --allow-paid
DATA_DIR=var/demo-replay uv run uvicorn app.main:app # dashboard that fills up as the replay sends reports
# Evals on your own labeled data (folders OUTSIDE this repo; each prints its cost first)
uv run python -m scripts.eval_transcribe /path/to/recordings   # WER (raw and cleaned) and place names, per setting
uv run python -m scripts.eval_vision /path/to/photos --sam [--llm muse-spark-open]  # labels.csv + images/
uv run python -m scripts.make_vision_set /path/to/vision-synthetic   # 40 labeled Muse Image photos from data/eval/vision_scenes.json (~$0.40, idempotent)
# Record a full run in data/eval/results.json (evaluation guide: uv run python -m app.eval_results; add --headlines, --models or --comparison for the README)
uv run python -m scripts.eval_vision /path/to/vision-synthetic --sam --record data/eval/results.json --dataset synthetic-40 --out out.json
uv run python -m scripts.eval_crosscheck /path/to/vision-synthetic --results out.json --model muse-spark-1.3 --record data/eval/results.json --dataset synthetic-40   # free: reuses the run's outputs
uv run python -m scripts.eval_dedupe --judge muse-spark-open --record data/eval/results.json
uv run python -m scripts.eval_screen --spend-cap 0.30 --record data/eval/results.json   # 60 synthetic messages; --cached-only never pays
uv run python -m scripts.eval_transcribe /path/to/test-audio --only 3-10 --record data/eval/results.json --dataset voice-notes-8
# Paid eval scripts log every call to DATA_DIR/eval-spend.db, print the cost of the uncached calls
# (a free cache-only dry run) and refuse to pass --spend-cap (default $5.00)
uv run python -m scripts.eval_facts /path/to/recordings [--llm muse-spark-open]     # facts.json + audio
# Rebuild Biñan's street and place names from OpenStreetMap (free; --raw reuses a saved response)
uv run python -m scripts.build_places [--save-raw overpass.json | --raw overpass.json]
# Delete expired raw media once (the bot and a running web app are not required)
uv run python -m app.retention
```

Model responses are cached on disk under `DATA_DIR/model_cache` when `MODEL_CACHE=true`, so repeated development runs do not spend credits. Raw photos, voice notes and photo overlays are deleted after `RAW_MEDIA_TTL_HOURS` (default 24), or up to `HELD_MEDIA_MAX_HOURS` (default 72 from media creation) while processing is paused by the spend cap. Blocked reporters use the normal TTL. Retention runs hourly and skips the media of a report while it is being assessed (for at most an hour). The synthetic demo scenario's media (channel `demo`) is exempt: its photos are generated by Muse Image and stamped as such and its reporters are invented, so it holds no personal data, and a deployed demo would otherwise show "deleted after the retention period" on every demo photo the day after seeding. Every real channel is purged. Reports, transcripts and assessments are kept. The bot's bilingual `/start` privacy notice states both configured retention periods. Names that reporters say when introducing themselves ("Ako po si …") are redacted before any text reaches a model.

## Deploy

Ulat runs on [Modal](https://modal.com) as **one always-on container** (commands and the production checklist:
[deploy/README.md](../deploy/README.md)):

1. One FastAPI process serves the officer dashboard and, with `TELEGRAM_MODE=webhook`, the Telegram bot: Telegram
   posts to a secret-checked `/telegram/webhook/…` route, and the bot, its assessor and the retention purge run in
   the same event loop. `min_containers=1`, `max_containers=1`: one SQLite writer, no cold starts.
2. The live database is on the container's disk; a Modal Volume holds media, the model cache and database
   snapshots, taken with SQLite's backup API every 2 minutes and at shutdown, and restored at start.
3. A writer lease on the volume stops a restarting container from restoring before the old one's last snapshot.
4. Staging (`ulat-staging`, @UlatPH_dev_bot) and production (`ulat`, @UlatPH_bot) have separate apps, secrets,
   volumes and pseudonym salts. A fresh volume seeds the synthetic demo through the real pipeline.
5. The webhook is registered by an explicit command, never at startup, and local polling refuses to start while
   a webhook is set, so the two never fight over the bot. Production has run on the webhook since 2026-10-30; no
   bot runs on the developer's machine.

- **Cost:** about $2.65 per week for the container (0.25 CPU, 512 MiB, always on, at Modal's list rates of
  $0.0000131 per core-second and $0.00000222 per GiB-second: [modal.com/pricing](https://modal.com/pricing)), plus
  model calls, capped at $10 a day on the public instance (`DAILY_SPEND_CAP_USD`) on top of the per-reporter
  limits.
- **Login:** every page and photo needs the officer passcode (report photos and texts are personal data under
  RA 10173). Only `/health`, `/login`, `/logout`, the static files and the webhook are open. The passcode opens a signed, HttpOnly,
  Secure, SameSite=Lax cookie for 12 hours; wrong passcodes are limited to 10 per 15 minutes per client address.
  Initials typed at login prefill the review forms; with a shared passcode they are self-declared.
- **Durability and loss window:** orderly shutdown attempts a final snapshot; a staging stop test preserved its
  two test reports ([details](../deploy/README.md#durability)). An unexpected crash can lose writes since the last
  successful snapshot, including updates acknowledged but still queued. The bot requests a snapshot after handling
  each update, and periodic snapshots run every 2 minutes, but failures or delays can extend the loss window.
  Retention keeps the newest 15 snapshots plus one a day for a week.
- **Judges:** see [Demo](#demo) for the URL, the bot and what to try; the passcode is in the submission's private
  judging notes. Staging (`ulat-staging`, the dev bot) is for testing only and is not a public entry point.

## Repo structure

```
AGENTS.md              Instructions for AI coding agents (Muse Code, Claude Code, …)
.agents/skills/        Provider notes for coding agents: baseten, fireworks, modal (SKILL.md each)
README.md              Short guide for judges
docs/                  How Ulat works, evaluation, and the developer guide
CREDITS.md             Third-party credits and license terms
LICENSE                MIT License for the project's own code
pyproject.toml         Python project and tool config (uv)
.env.example           Names of the environment variables (no secrets)
app/
  config.py            Settings from env/.env
  auth.py              Officer login: passcode, signed session cookie, deny-by-default gate, login rate limit
  telegram_webhook.py  Webhook mode: the bot inside the web app, secret-checked update route
  snapshots.py         Local SQLite snapshotted to a volume: restore, periodic backup, retention, writer lease
  deploy.py            Deploy CLI (python -m app.deploy); deploy_targets.py: staging and production as data
  seed.py              Seeds a fresh deploy with the synthetic demo through the real pipeline
  logs.py              Logging with every configured secret masked
  cache.py             Content-hash disk cache for model responses
  meta/                Clients: transcribe.py, spark.py, sam.py, spark_open.py, fireworks.py;
                       openai_compatible.py (shared chat), llm.py (AgentLLM switch),
                       schema.py (output validation), base.py, factory.py
  models.py, db.py     SQLModel tables including Incident, immutable AgentDecision and OfficerAction
  trace.py             Agent trace context: the pipeline steps, the Meta model families, the call-to-report link
  agent_trace.py       Each report's agent trace (model, rule and officer steps) and a draft's model calls
  accuracy.py          The /accuracy page from data/eval/results.json: tables, captions, provenance
  gazetteer.py         Biñan's 24 barangays: names, aliases, matching in text, transcription keywords (with
                       the OpenStreetMap names of data/binan_places.json, up to Voice Transcribe's 500)
  geo.py, data/        Point-in-barangay lookup on COD-AB boundaries; photo EXIF GPS
  transcript_cleanup.py  Removes repeated-word artifacts from transcripts
  facts.py, photo.py   Fact extraction; photo judgment, SAM measurement and evidence overlay
  crosscheck.py        Spark and SAM check each other on every photo: the rules, as data, and their trust signals
  assessment.py        Assesses a report and maps it to a barangay (python -m app.assessment)
  incidents.py         Candidates, deterministic rollups, audited grouping and rebuild CLI
  counting.py          Per-barangay evidence and the no-double-counting rule
  review.py            Officer review queue and audited overrides (merge, keep, place, trust, trees, block)
  rdana_form.py        The 2021 RDANA form as data (sections, numbering, fill modes)
  rdana.py             Evidence rules that fill the form; rdana_summary.py: the validated 1.4 summary
  nol_form.py, nol.py  PCIC claim forms as commodity data (fisheries Notice of Loss, rice Claim for
                       Indemnity) and the deterministic filling
  pcic_lines.py        Which PCIC claims an office drafts (data/pcic_lines.json, PCIC_COMMODITIES)
  drafts.py            Versioned drafts, approve/reject, audit, damaged-houses table and CSV
  deadlines.py         Filing deadlines from data/deadlines.json (with sources) and countdowns
  pair_judge.py        Text-only pair schema and redacted evidence prompts
  demo/                Synthetic demo: scenario, Muse Image photos, loader (python -m app.demo)
  privacy.py           HMAC pseudonymization of reporter ids
  audio.py             ffmpeg conversion and chunking for voice notes
  intake.py            Ingest text, photo, voice and location into reports
  retention.py         Purge raw media after the TTL
  bot.py               Telegram bot (python -m app.bot)
  reporter_summary.py  The reporter's "here's what we understood" reply and its in-memory acknowledgments
  bot_profile.py       The bot's public profile (name, descriptions, commands) as data; idempotent CLI that applies it
  main.py, templates/  FastAPI officer pages: reports, incidents, timelines, decision audit, drafts and media
  nav.py               The dashboard's navigation destinations (bar, rail, drawer and the More sheet)
  live.py              Live updates: one database watcher per process and the server-sent event streams
  dashboard.py         Read models shared by the officer pages (overview figures, active incidents)
  mapdata.py           Map features: incident markers (mean GPS or approximate barangay center), flagged outside pins
  static/              Dashboard stylesheet (css/app.css, Material 3 tokens), script (js/app.js, live updates and
                       in-app navigation), favicon and vendor/ (pinned browser libraries, the Roboto font and the
                       icon sprite, with their licenses)
  evaluation.py        WER, place names heard, Cohen's kappa and F1 helpers
scripts/
  smoke.py             Offline webhook and dashboard smoke test with cached synthetic replies
  build_icons.py       Builds the Material Symbols icon sprite from a list of icon names
  build_places.py      Rebuilds app/data/binan_places.json from OpenStreetMap (Overpass API, idempotent)
  eval_transcribe.py   Voice Transcribe WER and place names per setting, on recordings outside the repo
  eval_vision.py       Photo judge and SAM vs hand labels, per agent LLM
  eval_crosscheck.py   How often Spark and SAM disagree and whom the hand label sides with (free, from eval_vision's outputs)
  eval_crop.py         Rice-field judge and SAM field/water masks vs hand labels, per agent LLM
  eval_facts.py        Fact extraction from voice notes vs hand labels, per agent LLM
  eval_dedupe.py       Cached pair-judge P/R/F1, review rate and per-pair misses
deploy/                Modal app (modal_app.py) and the deploy guide (README.md)
assets/brand/          Bot avatar (branding only)
data/demo/scenario.json  The synthetic typhoon scenario (no real people)
tests/                 Mocked-HTTP tests and synthetic dedupe_pairs.json (64 labeled pairs)
```
