# (Project name TBD)

> An entry for the **Meta Global AI Developer Hackathon** (Oct 27 – Nov 4, 2026), built for the Philippines with Meta's Muse models.

**Status:** building; slices 1–4 (intake, assessment/mapping, incident coordination, abuse guards, RDANA draft and officer approval) implemented and locally tested. Typhoon and flood damage reporting for Philippine LGUs, plus crop and fisheries insurance claims: one agent turns photos, Taglish voice notes, and texts from barangays, farmers, and fisherfolk into an officer-approved RDANA report, a damaged-houses table, and PCIC Notices of Loss. Demo area: Biñan City, Laguna.

---

## The problem

_TBD: one paragraph on who suffers, how much, and why now, with sourced numbers._

## What it does

1. Residents send photos, voice notes, texts and location pins; the agent extracts damage facts and maps the report to a barangay.
2. Deterministic place/time/GPS filters find candidate incidents. A text-only Meta pair judge decides whether reports describe the same situation; uncertainty stays separate for officer review.
3. Officers read incident timelines, depth changes and the stored reasons for grouping. Reported totals explicitly warn about double-counting, including corrections.
4. The agent drafts each barangay's RDANA form and the city's damaged-houses table from that evidence; the officer checks every value against its linked reports, then approves or rejects the draft (see [Officer outputs](#officer-outputs)). PCIC Notice of Loss drafting is the next planned output.

## Meta models used

| Model | What it does in this project | Status |
|---|---|---|
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Turns Telegram voice notes (Tagalog, English, Taglish) into text, biased toward Biñan's 24 barangay names with `keywords` | In use |
| Muse Spark 1.3 (`muse-spark-1.3`) | The agent LLM (default): judges each photo as structured JSON (roof damage, floodwater, debris, fallen tree, tarpaulin, house count) and extracts report facts (flood depth, houses, evacuees, crops, needs); default incident pair judge; writes the RDANA summary description (1.4), checked against the evidence | In use |
| Muse Spark Open (`meta-models/muse-spark-open`, Baseten) | The same agent steps and incident pair judgments on open weights, photos included: set `AGENT_LLM=muse-spark-open` | In use |
| Muse Spark Open (`accounts/fireworks/models/muse-spark-open`, Fireworks) | Agent or independent pair judge via `muse-spark-open-fireworks` | Implemented; mocked HTTP verified, no live call in this slice |
| Muse Glimmer (`accounts/fireworks/models/muse-glimmer-30b`, Fireworks) | Pair-judge comparison via `muse-glimmer` | Implemented; serverless availability ⚠️ contradictory and unverified; explicit availability errors, no fallback |
| Muse Image (`muse-image-1.0`) | Synthetic, clearly labelled demo photos for the typhoon scenario (no real homes or people); every image is stamped "AI-generated (Muse Image) · synthetic demo" | In use (demo data) |
| SAM 3.1 (`sam-3.1`) | Measures what the LLM judges: counts roofs and measures floodwater from masks, drawn on the photo as evidence | In use |

Every LLM call goes through `AgentLLM`. `AGENT_LLM` selects fact extraction and photo judgment; `PAIR_JUDGE_LLM` independently selects the incident judge and defaults to `AGENT_LLM` when omitted. Both accept `muse-spark-1.3`, `muse-spark-open` (Baseten), `muse-spark-open-fireworks` or `muse-glimmer` (Fireworks). Provider configuration comes from the local [Baseten skill](.agents/skills/baseten/SKILL.md) and [Fireworks skill](.agents/skills/fireworks/SKILL.md), which cite the provider sources. No live provider compatibility or accuracy is claimed by mocked tests.

Incident candidates share a barangay and activity within `INCIDENT_WINDOW_DAYS` (14 by default). GPS within 500 m can add candidates when a barangay is missing, and distance vetoes distant pairs when both sides have points. Known different barangays never match. Only `same` at or above `PAIR_MERGE_THRESHOLD` (0.85 by default) merges; unsure and low-confidence results remain separate with `needs_officer`. These are configurable policy choices, not calibrated accuracy claims. Pair prompts use typed facts, regenerated summaries and photo captions, and redacted place details; they omit raw text, transcripts, reporter pseudonyms, images and free-form photo notes.

Grouping runs after successful assessment under the same per-report lock, without a database transaction across a model call. A changed late pin re-links the report; an empty old group is retired from the list while its audit remains readable. Unchanged grouping is a no-op. `AgentDecision` stores the actual judge model, compared incident/report, confidence and reason; pages do not regenerate explanations. Judge failures stay visible for officer review and can be retried explicitly. Existing slice-2 SQLite files receive grouping columns on startup without losing reports.

## Spam and fake reports

Ulat never sends aid or files a claim by itself: every output is a draft an officer approves, and PCIC
adjusters validate losses in the field. So the risks are **credit drain**, **officer overload**,
**poisoned incident numbers** and **insurance fraud**. The agent handles them in layers, without extra
model calls:

| Layer | What happens | Default |
|---|---|---|
| Rate limit | Each pseudonymous reporter is checked *before* a file is downloaded. Over the limit, the bot replies (Tagalog / English) to try later and to call 911 for emergencies | 20 reports per hour, 60 per 24 h (`REPORTS_PER_HOUR`, `REPORTS_PER_DAY`) |
| Spend cap | Model costs logged in the last 24 h are checked before every assessment. Over the cap, new reports are stored as `held` and assessed later by `python -m app.assessment --pending` | $10 (`DAILY_SPEND_CAP_USD`) |
| Block list | `python -m app.trust block <pseudonym-prefix>` (and `unblock`). Messages are still stored as `held` for the officer but never reach a model | — |
| Trust signals | Rule-based flags on each report, shown as red / amber / grey chips: **photo reused** by another reporter (SHA-256 or a 64-bit difference hash, so resized or recompressed copies match), **old photo** (EXIF capture time), **pin outside Biñan**, **pin and text name different barangays** | Photo older than 3 days (`PHOTO_MAX_AGE_DAYS`) |
| Corroboration | Each incident shows how many *different* reporters it has; one reporter is labelled **single source** | — |

Reporter ids and trust signals never enter a model prompt. Raw media of held reports follows the
retention rule below (spend-cap holds are kept up to `HELD_MEDIA_MAX_HOURS`, 72 h by default, and
the bot's `/start` notice says so).

**Limits:** one person with several Telegram accounts can still corroborate themselves; EXIF can be
edited or stripped (Telegram strips it from normal photos); AI-generated or staged photos are not
detected; the spend cap uses estimated costs, so in-flight work can overshoot it a little. The officer
and the PCIC adjuster remain the final check. A next step is verified reporters (barangay officials
enrolled by the CDRRMO).

## Officer outputs

The first officer output is a draft of the **RDANA form in force**: Annex I of NDRRMC Memorandum No. 107, s. 2021
(the 2021 RDANA SOP), "to be accomplished within the first 72 hours after a disaster". Ulat drafts one form per
barangay at `/drafts`, in the form's official order and numbering (its own numbering typos are kept and marked
"sic"), printable on A4. The form is defined once as data in [app/rdana_form.py](app/rdana_form.py); each field is
fixed (region, province, city), entered by the officer (event name, persons interviewed, overall assessment,
submitted by), filled by a deterministic rule over typed report facts, or written by the agent (the summary).

| What the draft fills | From |
|---|---|
| 1.1.2 type and 1.1.3 date/time of event | Reported hazards; the earliest report (the officer can override both; an event time in the future or more than 30 days before the earliest report is refused, and one after the earliest report is kept with a warning) |
| 1.2.4–1.2.7 barangay, sitio/purok, GPS, date/time of RDANA | The incidents, their redacted place details, the mean of report pins, the draft time |
| 1.4 summary description | The agent, plus the latest and maximum flood depth per incident as support data |
| 2.1, 2.3, 2.4, 2.9, 2.12, 2.15 | Families affected, families and persons evacuated, missing, injured, dead (totals only) |
| 3.3, 3.4, 4.2, 8.1, 8.2 | Damaged roads or bridges, transport or clearing needs, power (a note only), rescue requests (Water SRR only where rescue was asked for in a flood or storm-surge incident) |
| 10.1, 10.2, 10.3, 11.5, 12.5, 15.5 (sic) | Houses totally and partially damaged; shelter, food, water and medicine needs (no item is invented) |
| 18.1 | An evidence note on fish cages or pens and crops damaged |

Everything else, such as the male/female splits, accessibility, communications, evacuation centers, relief,
law and order and the overall assessment, is marked "— not in reports (for the RDANA team)". Missing evidence is
never turned into "No" or "0". Every filled value links to its incidents and reports, with the reporter count,
a "single source" chip and flagged-report chips. A report in an incident links to its place on the incident
timeline; any other report links to `/reports?around=<id>`, which shows the reports around it however old it is.

**Counting rule.** Reports in one incident describe the same houses and people, so their numbers are never added.
Per incident, the latest correction replaces earlier figures; otherwise, and for figures reported after that
correction, the highest reported value is used. Incident values are then summed across the barangay. Reports that
are not assessed yet, or have no barangay, are listed but not counted. Each count shows how much of it rests on one
reporter or on flagged reports. A value is single-source when fewer than two different reporters stated that value
or more (after the latest correction), so one reporter's 40 is not corroborated by two others' 2.

**Pending grouping review.** A report whose grouping waits for the officer (its incident is `needs_officer` after an
uncertain or failed duplicate check, it has a grouping error, or it was never grouped) may describe the same houses
as another report. Its figures are never added to a total: they form a separate bucket, counted as if all pending
reports were one incident, and are shown beside the confirmed figure ("pending grouping review: 5 (reports 4, 7,
9)"), in the CSV's pending columns and in a banner on the draft. So 5 houses reported three times while the pair
judge is down read as 0 confirmed and 5 pending, never 15. With only pending evidence a field shows "no confirmed
figure", not 0. Approving such a draft is allowed, but needs a written reason, and the audit records the pending
reports.

**The agent's summary (1.4).** One agent-LLM call per draft sees only redacted, typed facts per incident and the
computed counts: no reporter ids, raw messages, transcripts or names. The reply is accepted only if every report it
cites was in the input, it has at most four sentences, and every number passes an allowlist:

- A number must be a confirmed computed count from the form (after the counting rule, pending figures excluded) or
  the number of reports, incidents or reporters, and the words next to it must name exactly that one count: "4 houses
  partially damaged" passes only if 4 is that count, "4 houses damaged" is totally and partially damaged together,
  and "3 dead" fails when 3 is a families count. A number with no such label ("2 meters"), or one that could mean
  several counts ("2 people were trapped", "2 residents drowned"), is rejected: people need a qualifier (evacuated,
  dead, injured, missing). A single report's figure (such as one superseded by a correction), an id, or a sum the
  agent made up is never allowed.
- Spelled numbers are read whole ("twenty-five" is 25, "a dozen" is 12) and checked the same way; "one" is skipped
  only in "no one" and in "one of" when no count word follows it ("one of the residents died" claims a death). Vague amounts ("hundreds of", "dozens of") are
  rejected.
- Times (7:30, 7 pm), dates (month or weekday names, 10/28), ordinals (28th, twenty-eighth; "first" is allowed as
  plain English) and report or incident references ("report 12", "#12") are rejected: the form has its own fields
  for them.
- Place digits pass only inside a place written exactly as the reports give it ("Purok 3"); "Purok 5" fails when no
  report names it.

The prompt asks for counts as digits next to their words and for no dates or ids. A rejected reply is retried once
with the errors; after that, or when the model fails
or the daily spend cap is reached, a deterministic summary is used and the reason is stored on the draft and shown.

**Damaged-houses table.** `/damaged-houses` (and `.csv`, UTF-8 with a byte-order mark for spreadsheets) lists
Region / Province / City / Barangay / Total / Totally / Partially / evidence report ids, in the DSWD DROMIC
damaged-houses columns, per barangay with a city total, using the same counting rule, then how much of Total was
stated by only one reporter or comes from flagged reports, and the pending grouping-review figure with its report
ids. An unstated count is empty, not zero. Every CSV row starts with a Status column: `LIVE (NOT APPROVED)` for the
live table (also in its file name), or the snapshot's status and version (`APPROVED v3`, `DRAFT v2 (NOT APPROVED)`).
The officer can save the current table as a draft for approval; its values are then frozen.

**Approval and audit.** A draft is approved or rejected once (a reason is required to reject), with an optional
designation of the officer. Generating again creates version n+1 and marks the previous undecided version
`superseded`; approved and rejected versions stay exactly as decided. Every generate, supersede, approve and
reject writes an append-only `DraftEvent`, kept apart from the incident grouping audit (`AgentDecision`), which is
per report. SQLite triggers reject edits to decided drafts and to the audit; startup drops and recreates them, so a
changed definition reaches an existing database (a backfill that must update decided drafts runs in between). The
whole schema setup runs in one `BEGIN IMMEDIATE` transaction, so the bot and the web app can start together. The
`/drafts` list and the draft page show when new evidence arrived after a version was generated, with a button that
generates a new version keeping the officer's event details. Approval re-checks this inside its write transaction:
approving a stale version needs an explicit confirmation (otherwise 409), and the audit row records `stale`. A
draft generated before the pending grouping-review bucket existed always counts as stale, since its totals may
include pending figures. **There is no login yet** (planned):
anyone who can reach the web app can approve, so it must only listen on localhost. Every state-changing request
(generate, approve, reject) must be same-origin: when the browser sends an `Origin` or `Referer` header it must name
this host, or the app answers 403, so another site open in the officer's browser cannot post to it. The app also
answers only to the host names in `ALLOWED_HOSTS` (default `localhost,127.0.0.1`; other names get 400), so a DNS
rebinding site cannot pose as this host. These are cross-site guards, not authentication.

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
uv run uvicorn app.main:app --reload      # web app: /health, /reports, /incidents, /drafts, /damaged-houses (.csv), /media/<id> (localhost only: no login yet)
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

Model responses are cached on disk under `DATA_DIR/model_cache` when `MODEL_CACHE=true`, so repeated development runs do not spend credits. Raw photos, voice notes and photo overlays are deleted after `RAW_MEDIA_TTL_HOURS` (default 24), or up to `HELD_MEDIA_MAX_HOURS` (default 72 from media creation) while processing is paused by the spend cap. Blocked reporters use the normal TTL. Retention runs hourly and skips active assessments until processing finishes. Reports, transcripts and assessments are kept. The bot's bilingual `/start` privacy notice states both configured retention periods. Names that reporters say when introducing themselves ("Ako po si …") are redacted before any text reaches a model.

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
| 2026-10-28 | Demo reset preserves generated Muse Image photos while clearing databases, uploads and manifests; updated CLI messages and offline regression coverage |
| 2026-10-29 | Spam and fake-report guards (slice 3c): per-reporter rate limits checked before download, a rolling 24 h model-spend cap that holds reports for later, an officer block list, and additive database upgrades |
| 2026-10-29 | Trust signals: reused or near-duplicate photos across reporters (SHA-256 and difference hash), old EXIF photos, pins outside Biñan or contradicting the text, and per-incident reporter counts with a "single source" label, shown as dashboard chips |
| 2026-10-29 | Review fixes for slice 3c: held reports keep their media for at most 72 h, blocked reporters' media follows the normal 24 h rule, late pins and partial evidence are not lost, photo hashing stays out of database writes |
| 2026-10-29 | Slice 4: the 2021 RDANA form (NDRRMC Memo 107 s. 2021, Annex I) as data, with official numbering; per-barangay evidence counted without double counting (latest correction, else the maximum per incident, summed across incidents) with single-source and flagged shares |
| 2026-10-29 | RDANA draft filling: deterministic evidence rules for 24 of its 108 fields with incident and report links, the "not in reports" marker for the rest, and the agent's 1.4 summary validated against cited reports and numbers, with one retry and a recorded deterministic fallback under the spend cap |
| 2026-10-29 | Officer drafts: `/drafts` list and printable A4 form, approve or reject with a reason, versioning that supersedes undecided drafts, an append-only `DraftEvent` audit and database triggers that freeze decided drafts |
| 2026-10-29 | Damaged-houses table per barangay in the DROMIC columns, with a city total, evidence report ids, a CSV download and approvable frozen snapshots |
| 2026-10-29 | Review fix: the single-source share is per value, not per incident: a value counts as corroborated only when two different reporters stated it or more |
| 2026-10-29 | Review fix: startup drops and recreates every draft trigger, so changed definitions reach existing databases and a backfill can run between the two |
| 2026-10-29 | Review fix: figures from reports waiting for grouping review (`needs_officer`, grouping errors, never grouped) form a pending bucket beside each count and CSV row instead of being summed, with a draft banner and a required reason to approve |
| 2026-10-29 | Review fix: the 1.4 summary validator is an allowlist of the confirmed computed counts, each matched to the words next to it; spelled numbers are read whole; times, dates, ordinals, report ids, vague amounts and unquoted place numbers are rejected, and the prompt asks for digits |
| 2026-10-29 | Review fix: staleness is re-checked when a draft is approved: the draft page shows a new-evidence banner with a regenerate button, approving a stale version needs an explicit confirmation, and the audit records it |
| 2026-10-29 | Review fix: a same-origin check on every state-changing request (an Origin or Referer for another host gets 403) until the dashboard has a login |
| 2026-10-29 | Review fix: every damaged-houses CSV row starts with its status (LIVE (NOT APPROVED), or the snapshot's status and version) and the CSV adds single-source and flagged columns |
| 2026-10-29 | Review fix: the officer's event date and time is refused when it is in the future or more than 30 days before the earliest report, and flagged when it is after it |
| 2026-10-29 | Review fix: evidence links to reports outside an incident open /reports around that report, so they work past the 200-report page; trust-signal links use the same target |
| 2026-10-29 | Review fix (round 2): schema setup, including the trigger rebuild, runs in one BEGIN IMMEDIATE transaction, so the bot and the web app starting together cannot both drop the triggers |
| 2026-10-29 | Review fix (round 2): every number in the 1.4 summary needs a label naming exactly one computed count; unlabelled numbers, people with no qualifier and "one of" followed by a count word are rejected |
| 2026-10-29 | Review fix (round 2): open drafts generated before the pending grouping-review bucket count as stale, so approving one needs the confirmation and the page says why |
| 2026-10-29 | Review fix (round 2): a host allowlist (ALLOWED_HOSTS, default localhost and 127.0.0.1, enforced by Starlette TrustedHostMiddleware) stops DNS rebinding around the same-origin check |

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
  counting.py          Per-barangay evidence and the no-double-counting rule
  rdana_form.py        The 2021 RDANA form as data (sections, numbering, fill modes)
  rdana.py             Evidence rules that fill the form; rdana_summary.py: the validated 1.4 summary
  drafts.py            Versioned drafts, approve/reject, audit, damaged-houses table and CSV
  pair_judge.py        Text-only pair schema and redacted evidence prompts
  demo/                Synthetic demo: scenario, Muse Image photos, loader (python -m app.demo)
  privacy.py           HMAC pseudonymization of reporter ids
  audio.py             ffmpeg conversion and chunking for voice notes
  intake.py            Ingest text, photo, voice and location into reports
  retention.py         Purge raw media after the TTL
  bot.py               Telegram bot (python -m app.bot)
  main.py, templates/  FastAPI officer pages: reports, incidents, timelines, decision audit, drafts and media
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
