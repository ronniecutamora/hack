# (Project name TBD)

> An entry for the **Meta Global AI Developer Hackathon** (Oct 27 – Nov 4, 2026), built for the Philippines with Meta's Muse models.

**Status:** building; slices 1–4 (intake, assessment/mapping, incident coordination, abuse guards, RDANA draft and officer approval) implemented and locally tested; slice 5 (fisheries Notice of Loss) implemented and tested with fake model clients. Typhoon and flood damage reporting for Philippine LGUs, plus fisheries insurance claims: one agent turns photos, Taglish voice notes, and texts from barangays and fisherfolk into an officer-approved RDANA report, a damaged-houses table, and a PCIC Notice of Loss for each fish cage or pen loss. Crop insurance (rice, corn) is later work. Demo area: Biñan City, Laguna.

---

## The problem

_TBD: one paragraph on who suffers, how much, and why now, with sourced numbers._

## What it does

1. Residents send photos, voice notes, texts and location pins; the agent extracts damage facts and maps the report to a barangay. Reporters often send a photo and then type its words: a text within 2 minutes of the same reporter's caption-less photo, with nothing in between, becomes that photo's caption (the bot answers "Idinugtong sa iyong larawan. / Added to your photo."), and the photo is re-assessed and re-grouped with it, like a late location pin. Photos sent as one Telegram album (one `media_group_id`) are one report: one "received" reply, every photo assessed, and a text after a caption-less album captions all of it.
2. Deterministic place/time/GPS filters find candidate incidents. A text-only Meta pair judge decides whether reports describe the same situation; uncertainty stays separate for officer review. Every candidate is judged, not just the first, so a correction ("mali po… dalawang bahay lang, hindi tatlo") lands in the incident that holds the figure it corrects.
3. Officers read incident timelines, depth changes and the stored reasons for grouping, and resolve what the agent could not settle in a review queue: merge, keep separate, set the place, check trust signals, confirm fallen trees, block a reporter (see [Officer review](#officer-review)). Reported totals explicitly warn about double-counting, including corrections.
4. The agent drafts each barangay's RDANA form and the city's damaged-houses table from that evidence; the officer checks every value against its linked reports, then approves or rejects the draft (see [Officer outputs](#officer-outputs)).
5. For each incident of fish cage, fish pen or fishpond damage, the agent drafts the fisherfolk's PCIC Notice of Loss with an evidence annex for the adjuster and counts down its 2-day filing deadline; the officer approves it for filing, and the assured files it (see [Insurance: fisheries Notice of Loss](#insurance-fisheries-notice-of-loss)). Crop insurance is later work.

Every report on `/reports` and on its incident timeline has an expandable **agent trace**: each pipeline step in order (transcribe, read the facts, judge each photo, SAM roofs and floodwater, find the barangay, check trust signals, group into an incident) with the Meta model that ran it, its latency, whether it came from the cache and its estimated cost, next to the one-line result it produced ("roof damage: partial", "2 roofs found", "barangay Malaban (photo GPS)", "different from incident #2, 96% → kept apart, in incident #3"). Deterministic steps are labelled "Rule, no model", officer overrides follow as "Officer" steps, and the totals give the report's model time and cost. The trace shows only derived results the dashboard already shows, never prompts, replies or the officer's notes. An RDANA draft page lists the model calls that wrote its summary. Reports assessed before 2026-10-30 say "trace not recorded". How often each model is right is on the dashboard's `/accuracy` page (see [How well it works](#how-well-it-works)).

## Meta models used

| Model | What it does in this project | Status |
|---|---|---|
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Turns Telegram voice notes (Tagalog, English, Taglish) into text, biased toward Biñan's 24 barangay names with `keywords` | In use |
| Muse Spark 1.3 (`muse-spark-1.3`) | The agent LLM (default): judges each photo as structured JSON (roof damage, floodwater, debris, fallen tree, tarpaulin, house count) and extracts report facts (flood depth, houses, evacuees, crops, needs); default incident pair judge; writes the RDANA summary description (1.4), checked against the evidence | In use |
| Muse Spark Open (`meta-models/muse-spark-open`, Baseten) | The same agent steps and incident pair judgments on open weights, photos included: set `AGENT_LLM=muse-spark-open` | In use |
| Muse Spark Open (`accounts/fireworks/models/muse-spark-open`, Fireworks) | Agent or independent pair judge via `muse-spark-open-fireworks` | Implemented; mocked HTTP verified, no live call in this slice |
| Muse Glimmer (`accounts/fireworks/models/muse-glimmer-30b`, Fireworks) | Pair-judge comparison via `muse-glimmer` | Implemented; serverless availability ⚠️ contradictory and unverified; explicit availability errors, no fallback |
| Muse Image (`muse-image-1.0`) | Synthetic, clearly labelled demo photos for the typhoon scenario and the 40-photo synthetic eval set (no real homes or people); every image is stamped "AI-generated (Muse Image) · synthetic demo" | In use (demo and eval data) |
| SAM 3.1 (`sam-3.1`) | Measures what the LLM judges: counts roofs and measures floodwater from masks, drawn on the photo as evidence | In use |

Every LLM call goes through `AgentLLM`. `AGENT_LLM` selects fact extraction and photo judgment; `PAIR_JUDGE_LLM` independently selects the incident judge and defaults to `AGENT_LLM` when omitted. Both accept `muse-spark-1.3`, `muse-spark-open` (Baseten), `muse-spark-open-fireworks` or `muse-glimmer` (Fireworks). Provider configuration comes from the local [Baseten skill](.agents/skills/baseten/SKILL.md) and [Fireworks skill](.agents/skills/fireworks/SKILL.md), which cite the provider sources. No live provider compatibility or accuracy is claimed by mocked tests.

Incident candidates share a barangay and activity within `INCIDENT_WINDOW_DAYS` (14 by default). GPS within 500 m can add candidates when a barangay is missing, and distance vetoes distant pairs when both sides have points. Known different barangays never match. The judge compares the report with every candidate up to `MAX_JUDGED_CANDIDATES` (4 by default), and only `same` at or above `PAIR_MERGE_THRESHOLD` (0.85 by default) is accepted. When several candidates are accepted, the report joins an incident that is not waiting for officer review; a correction joins the incident whose reports state the figure it corrects; then the higher confidence wins, and the merge decision's stored reason says why it beat the others. An unsure or low-confidence result does not stop the other comparisons: the report stays separate with `needs_officer` only when no candidate is accepted, and a judge failure merges nothing. One rule runs before the judge: a correction ("mali po yung sinabi ko kanina…") joins the candidate incident where the same pseudonymous reporter stated a figure it corrects (the most recent such report), with no model call and a logged reason such as "Correction by the same reporter of the houses partially damaged figure in report 8". These are configurable policy choices, not calibrated accuracy claims. Pair prompts use typed facts, regenerated summaries and photo captions, and redacted place details; they omit raw text, transcripts, reporter pseudonyms, images and free-form photo notes.

Grouping runs after successful assessment under the same per-report lock, without a database transaction across a model call. A changed late pin re-links the report; an empty old group is retired from the list while its audit remains readable. Unchanged grouping is a no-op. `AgentDecision` stores the actual judge model, compared incident/report, confidence and reason; pages do not regenerate explanations. Judge failures stay visible for officer review and can be retried explicitly. Existing slice-2 SQLite files receive grouping columns on startup without losing reports.

### The reporter's side (Telegram bot)

Ulat is a hackathon prototype that Biñan's disaster office has not adopted, and the bot says so. Its public profile
(`app/bot_profile.py`, applied with `python -m app.bot_profile`) reads, verbatim:

- **Name:** Ulat · Bagyo at Baha (prototype)
- **Short description:** Prototype: mag-report ng pinsala ng bagyo at baha. / Prototype damage reports, not official.
  Emergency? Call 911.
- **Description:** Prototype para sa Biñan, hindi pa opisyal o binabantayan ng LGU: magpadala ng larawan, voice note,
  text o location ng pinsala. / A prototype for Biñan's disaster office, not yet official or monitored by the city:
  send photos, voice notes, text or location of the damage. — Hindi ito emergency hotline. Para sa rescue, tumawag sa
  911. / Not an emergency line. For rescue, call 911. — (the `/start` privacy line)

`/start` and `/help` carry the same prototype notice. A test keeps every public text free of claims that reports
reach the City DRRM Office.

**"Here's what we understood."** When a report's assessment finishes, the bot answers in the same chat with what the
agent took from it, built from the stored facts with no extra model call: the barangay (or "hindi pa alam ang lugar /
place not identified yet: send a location pin"), the flood depth, the counts and the needs, ending "Mali? I-reply ang
tamang detalye. / Wrong? Reply with the correct details." The place detail goes through the same redaction as every
output, so the reply never repeats a name or phone number, not even the reporter's own. A report must stay quiet for
15 seconds after its assessment before the summary goes out, so a photo, its pin and its caption get one summary,
not three; held, blocked and failed reports get no facts back (only the earlier "received"). A reply to a summary
within 24 hours is stored as a correction of that report (`Report.corrects_report_id`, the same reporter only) and
joins its incident by rule, even when it names no place ("dalawa lang po"). **Privacy:** the chat id is never stored;
it lives only in the bot process's memory (inside the message being answered), so after a restart pending summaries
are skipped and replies to older summaries are ordinary reports. Rate limits are unchanged.

## How well it works

Measured on labelled sets with real (cached) model calls, not mocks. Every number below is generated from [data/eval/results.json](data/eval/results.json), which also stores each run's dataset provenance, model id and backend, date, spend and code commit; a test keeps this table identical to the file. Re-run with the commands under "Getting started". The officer dashboard renders the same file at `/accuracy` ("How accurate is the agent?", in the header and linked from the overview): one table per task and data set with Spark 1.3 and Spark Open side by side, n, data provenance, run date, code commit and spend, weak scores marked, and a plain-language caption on every score saying what it means and what it does not (SAM's outlines are evidence, not the loss figure; fallen trees need confirmation); a missing or broken file shows an empty state.

<!-- eval-results:start (generated by: uv run python -m app.eval_results) -->

**Photo judge** (the agent LLM judges each photo; SAM 3.1 finds roofs and floodwater):

| Model | Dataset | n | Roof damage κ | Floodwater F1 | Debris F1 | Tarpaulin F1 | Fallen tree F1 | SAM roof F1 | SAM floodwater F1 | Date |
|---|---|---|---|---|---|---|---|---|---|---|
| muse-spark-1.3 | Real typhoon photos (real) | 25 | 0.66 | 1.00 | 0.89 | 0.80 | 0.57 | 1.00 | 1.00 | 2026-10-30 |
| muse-spark-open | Real typhoon photos (real) | 25 | 0.56 | 1.00 | 0.92 | 0.92 | 0.33 | 1.00 | 1.00 | 2026-10-30 |
| muse-spark-1.3 | Synthetic Biñan-style photos (synthetic) | 40 | 0.84 | 0.90 | 0.79 | 0.91 | 1.00 | 0.96 | 0.44 | 2026-10-30 |
| muse-spark-open | Synthetic Biñan-style photos (synthetic) | 40 | 0.79 | 0.95 | 0.76 | 1.00 | 1.00 | 0.96 | 0.44 | 2026-10-30 |

**Incident pair judge** ("is this new report the same incident?", merge threshold 0.85; below it, or unsure, goes to the officer):

| Model | Dataset | n | Precision | Recall | F1 | Officer review rate | Date |
|---|---|---|---|---|---|---|---|
| muse-spark-1.3 | Synthetic report pairs (synthetic) | 64 | 1.00 | 1.00 | 1.00 | 0% | 2026-10-30 |
| muse-spark-open | Synthetic report pairs (synthetic) | 64 | 1.00 | 1.00 | 1.00 | 0% | 2026-10-30 |

<!-- eval-results:end -->

Read these numbers with their caveats:

- **The real set is not Biñan.** It is 25 Wikimedia Commons photos of typhoons Haiyan (2013), Ketsana/Ondoy (2009) and Rai/Odette (2021), mostly severe damage, hand-labelled by one person for the 2026-10-27 kill test (credits under "Third-party code and assets"). Its judge and SAM replies were cached from the 2026-10-28 run with the same prompts, so these rows cost nothing here and repeat that run's roof-damage κ (0.66 and 0.56). The same prompt scored κ 0.78 on 2026-10-27: with 25 photos, one changed answer moves κ by about 0.05.
- **The synthetic set is synthetic.** Its 40 photos are Muse Image renders of Biñan-style scenes from [a scene table](data/eval/vision_scenes.json), stamped as AI-generated. Their labels were designed per scene and then checked against every image: 3 labels changed (a roof is visible on the far shore of three fish-pen photos) and 4 borderline ones were kept and noted. Accuracy on renders is not accuracy on residents' phone photos.
- **The pair set is synthetic and too easy to separate the models.** Both score 1.00 on 64 hand-written pairs. Real reports will be messier; the officer-review path for unsure and low-confidence pairs stays.
- **Fallen trees are the weak spot on real photos** (F1 0.57 on Spark 1.3, 0.33 on Spark Open), as the 2026-10-27 kill test found. The dashboard therefore asks for confirmation of fallen trees.
- **SAM calls the lake "floodwater".** On the synthetic lakeshore scenes, SAM's floodwater precision is 0.28: Laguna de Bay itself, fish-pen water and an irrigated paddy are masked as floodwater. For a lakeshore city, the SAM floodwater mask is evidence for the officer; the LLM's judgment (floodwater F1 0.90 / 0.95 there) decides.
- **Spark Open is close to Spark 1.3** on photos and equal on pairs, which is what the open-weight mode needs.
- Spend for all of these runs, including the 40 generated images: $1.93 at list prices, from the eval call log.

## Spam and fake reports

Ulat never sends aid or files a claim by itself: every output is a draft an officer approves, and PCIC
adjusters validate losses in the field. So the risks are **credit drain**, **officer overload**,
**poisoned incident numbers** and **insurance fraud**. The agent handles them in layers, without extra
model calls:

| Layer | What happens | Default |
|---|---|---|
| Rate limit | Each pseudonymous reporter is checked *before* a file is downloaded. Over the limit, the bot replies (Tagalog / English) to try later and to call 911 for emergencies. A late pin or caption that joins the sender's last report is not a new report and is not limited | 20 reports per hour, 60 per 24 h (`REPORTS_PER_HOUR`, `REPORTS_PER_DAY`) |
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
| 18.1, 18.2 | An evidence note on livelihood damage (fish pens or cages, fishponds, fishing gear or boats, crops, livestock; with a count only when one is stated); "Others" when reports ask for livelihood materials such as nets, bamboo or boats |

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

### Insurance: fisheries Notice of Loss

The second officer output is a draft **PCIC Notice of Loss, Fisheries** (FIP CAS Form No. 1, REV. 2022/MAY) for
each incident whose reports state fish pen, fish cage or fishpond damage, with or without a count. The `/drafts`
page lists those incidents with their filing clocks and a button per incident; the incident page shows the same
clock as a chip. The draft is deterministic (no model call) and uses the same versioning, staleness, approval,
audit and same-origin rules as the RDANA drafts.

| Field (form numbering) | Filled with |
|---|---|
| Regional Office, addressee | Fixed: PCIC Regional Office No. IV, Calamba City (address, email and phone from the PCIC Fisheries Insurance brochure 2024 V4) |
| 3 Location of farm | Barangay, city and province; the redacted place details; the mean of the report pins |
| 4 Nature/cause of loss | The hazards as reported (flood, strong wind, …) |
| 5 Date/time of loss | The earliest report in the incident, labelled "first reported": the loss may be earlier |
| 6 Total area affected | Only an area a report states; never derived from photos or SAM masks |
| 9 Extent of damage | The livelihood damage and counts as reported, and the photos with the agent's judgments and SAM 3.1 measurements, labelled "evidence for the adjuster, not the loss figure". The PCIC loss bands (total 90% and above, partial over 10% and below 90%) are shown; the agent never classifies the loss |
| Left for the assured | Type of policy and policy number (preamble), 1 name, 2 address, 7 lot number, 8 expected harvest date, 10 policy number, 11 underwriter, 12 contact number, date and signature. Ulat stores no names, addresses or phone numbers |

An **evidence annex** for the PCIC adjuster lists every report in the incident: its time, links to the photo and the
SAM overlay (until retention deletes the raw files), the photo judgments and the floodwater measurement (roof counts
are hidden for fisheries, and a caveat says SAM's "floodwater" also covers the lake or pond water around pens), an
excerpt of the message or voice note with names and contact details (phone numbers, emails, @handles, Facebook or
Messenger account names) removed, and its trust chips. A photo another reporter already sent (`photo_reused`) is a
red chip linking the earlier report and a red banner on the draft: one photo cannot prove two losses. Single-source
incidents, missing photos and grouping still pending officer review are marked too.

**Deadlines.** Every deadline comes from one table, [app/data/deadlines.json](app/data/deadlines.json), each row with
its source; adding a row (crops later) is data only.

| Deadline | Window | Counted from | Source |
|---|---|---|---|
| PCIC fisheries Notice of Loss | 2 calendar days | occurrence of loss | [PCIC Fisheries Insurance general information, 2024 V4](https://pcic.gov.ph/wp-content/uploads/2024/09/Fisheries-Insurance-2024-V4.pdf) |
| PCIC fisheries Claim for Indemnity | 7 calendar days | occurrence of loss | same brochure |
| Completed RDANA forms to the EOC | 72 hours | deployment at the assessment site | [NDRRMC Memorandum No. 107, s. 2021](https://ndrrmc.gov.ph/wp-content/uploads/2026/05/09-22-2021-NDRRMC-Memorandum-No.-107-s-2021-NDRRMC-Rapid-Damage-Assessment-Needs-Analysis-RDANA-Standard-Operating-Procedures.pdf), section 7.5.2 |

⚠️ Whether PCIC relaxed the fisheries deadlines for declared calamities after 2024 is not confirmed; the 2024 brochure
is the newest official text found. The clock runs from the first report (the earliest the loss can be known to have
happened) in Philippine time, and "within N calendar days" is read as N × 24 hours, the earliest reading, so the clock
never shows more time than the assured may have. It shows "due in 5h 3m" or "overdue by 2 days" and keeps counting
while the page is open. The draft also shows the PCIC Citizen's Charter 2026 fallback: a first notice may be sent by
text message, official email or official FB Messenger with the assured's name and address, the type of insurance,
and the date and cause of loss; the written Notice of Loss must follow.

**Approved for filing, not filed.** Approval does not stop the clock: the approved draft says "approved for filing,
not filed". Ulat never sends anything to PCIC; the assured (or the City Agriculturist's office on their behalf)
files it.

**Commodities as data.** The form is defined once in [app/nol_form.py](app/nol_form.py) as a commodity entry: its
PCIC form fields and numbering, the deadline-table rows it uses, the facts that make an incident eligible, the
evidence it requires, and how each field is filled (`assured` or an `evidence` rule; the office is fixed). The
drafting, deadline and approval code never names a commodity; a test adds a dummy commodity with no code change.
Only fisheries is built: crop insurance (rice, corn, high-value crops) is later work, one more entry once its PCIC
deadlines are verified for Biñan.

### Officer dashboard

The officer works in one server-rendered dashboard (FastAPI, Jinja, HTMX; no build step, every page usable with
JavaScript off). It updates live: the web app watches the shared SQLite file once per process (`PRAGMA
data_version`, so it sees the bot's writes from its separate process) and tells each open tab over server-sent
events which topics changed (reports, incidents, drafts, model spend), never report text. Each marked section
re-fetches only itself, keeps the scroll position, highlights new rows briefly and never replaces a section the
officer is typing in: it shows "New updates. Refresh section" there instead, and a requested refresh keeps the typed
values. The home page `/` is an overview: open incidents, incidents waiting for officer review, reports in the last
24 hours (and held ones), drafts awaiting approval, the next filing deadline with its live countdown and model spend
against the cap, each tile linking to its page and counted with the same helpers as that page
([app/dashboard.py](app/dashboard.py)), beside the incidents needing attention, the latest reports and a compact map. Under the tiles, "Meta models at work" lists each model's calls in the last 24 hours, the share answered from the cache, the cost (adding up to the spend tile) and the median time of live calls, from one windowed SQL query, and links to `/accuracy`. `/map` (Leaflet, vendored) draws
the barangay boundaries and one marker per active incident at the mean of its reports' GPS points inside Biñan, or
at its barangay's centre when no report has GPS (a dashed, hollow "approximate" marker); the marker's shape, a
number and a legend in words carry the status, not colour alone, and its popup links to the incident. A report
pinned outside Biñan appears only when the trust checks flagged it, and the map never pans to it. OpenStreetMap
tiles are the basemap; the boundaries and markers still draw when the tiles cannot load. The header
shows Live, Reconnecting or Offline with the time of the last update in Philippine time; when the
stream keeps failing, the page checks `/events/token` every 20 seconds instead.

### Officer review

`/review` (in the header and on the overview's "Needs officer review" tile) lists every report the agent could not
settle: an uncertain or failed duplicate check, an unknown place, a high or medium trust signal nobody has checked,
and a fallen tree the photo judge or the text suggested (the models do not detect trees reliably). Each card shows the
agent's latest duplicate check (compared incident, model or rule, decision, confidence, reason) and the actions; the
incident page has the same panel, and every report on a timeline keeps the actions under "Officer actions". The
officer can:

- **merge** the report into an incident the agent compared it with, or any active incident in its barangay;
- **keep it as its own incident**;
- **set or correct its barangay** from the gazetteer, with a reason; the report is then grouped again (the pair judge
  may be asked) under the new place;
- mark a **trust signal** checked, genuine (it no longer flags the report or its incident) or a problem, with a note
  ("photo reuse checked: same family, two phones");
- **confirm or rule out a fallen tree**, which the RDANA hazard list then follows;
- **block or unblock the reporter**, with the same function as `python -m app.trust block`.

Every override is an append-only `OfficerAction` row (database triggers refuse updates and deletes) with the officer's
initials (required, at most 40 characters, until the deploy slice adds a login) and an optional note, written in the
same transaction as the change. A separate table rather than new `AgentDecision` kinds, because an officer also
reviews signals and blocks reporters, which have no incident, and every row names a person; the incident page shows
agent and officer entries together, labelled "Agent" and "Officer JDC". Overrides **stick**: a merge or keep-separate
pins the report's grouping (`Report.officer_incident_id`), which re-grouping never changes and never re-judges, and a
set barangay (`Report.officer_barangay`) is never overwritten by re-assessment (the agent's own finding is kept beside
it). The pending bucket, the counts and the incident rollups follow at once, dependent open drafts turn stale through
their evidence fingerprint, and open dashboards refresh over the live stream. Rejected forms (no initials, an
incident outside the allowed set) come back with a 400, the typed values and the error beside the field;
cross-site posts get a 403.

## Demo

- Video (3 min): _TBD_
- Live app: _TBD_
- X post: _TBD_

The synthetic scenario ([data/demo/scenario.json](data/demo/scenario.json), no real people) has 18 Taglish reports
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
use it with `--reset`. A reset deletes the demo databases, uploads and manifest but keeps the generated images and the
model cache (`var/demo/model_cache`), so a reload of the same scenario reuses the paid photo judgments and SAM calls.

## Getting started

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `ffmpeg` (voice notes are converted to WAV; macOS: `brew install ffmpeg`).

```bash
uv sync
cp .env.example .env   # add META_API_KEY, TELEGRAM_BOT_TOKEN, PSEUDONYM_SALT (openssl rand -hex 32); BASETEN_API_KEY for Baseten; FIREWORKS_API_KEY for Fireworks
# AGENT_LLM=muse-spark-1.3 (default); optional independent PAIR_JUDGE_LLM
uv run python -m app.bot                  # Telegram intake bot (long polling)
uv run python -m app.bot_profile --dry-run  # bot name, descriptions and commands: print what would change
uv run python -m app.bot_profile --photo    # apply only what differs, and upload assets/brand/ulat-avatar.png
uv run uvicorn app.main:app --reload      # web app: / (overview), /map, /health, /reports, /incidents, /drafts, /damaged-houses (.csv), /media/<id>, live updates at /events (no login on localhost unless OFFICER_PASSCODE is set)
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
DATA_DIR=var/demo uv run python -m app.demo load --reset --anchor-now  # fresh load (keeps images and model cache), times end an hour ago
DATA_DIR=var/demo uv run uvicorn app.main:app        # dashboard on the demo data
uv run python -m app.demo replay [--speed 7200] [--reset]  # live replay into var/demo-replay at real time (~3 min); refuses uncached paid calls unless --allow-paid
DATA_DIR=var/demo-replay uv run uvicorn app.main:app # dashboard that fills up as the replay sends reports
# Evals on your own labelled data (folders OUTSIDE this repo; each prints its cost first)
uv run python -m scripts.eval_transcribe /path/to/recordings --keywords barangays   # WER, raw and cleaned
uv run python -m scripts.eval_vision /path/to/photos --sam [--llm muse-spark-open]  # labels.csv + images/
uv run python -m scripts.make_vision_set /path/to/vision-synthetic   # 40 labelled Muse Image photos from data/eval/vision_scenes.json (~$0.40, idempotent)
# Record a full run in data/eval/results.json (README tables: uv run python -m app.eval_results)
uv run python -m scripts.eval_vision /path/to/vision-synthetic --sam --record data/eval/results.json --dataset synthetic-40
uv run python -m scripts.eval_dedupe --judge muse-spark-open --record data/eval/results.json
# Paid eval scripts log every call to DATA_DIR/eval-spend.db, print the cost of the uncached calls
# (a free cache-only dry run) and refuse to pass --spend-cap (default $5.00)
uv run python -m scripts.eval_facts /path/to/recordings [--llm muse-spark-open]     # facts.json + audio
# Delete expired raw media once (the bot and a running web app are not required)
uv run python -m app.retention
```

Model responses are cached on disk under `DATA_DIR/model_cache` when `MODEL_CACHE=true`, so repeated development runs do not spend credits. Raw photos, voice notes and photo overlays are deleted after `RAW_MEDIA_TTL_HOURS` (default 24), or up to `HELD_MEDIA_MAX_HOURS` (default 72 from media creation) while processing is paused by the spend cap. Blocked reporters use the normal TTL. Retention runs hourly and skips active assessments until processing finishes. The synthetic demo scenario's media (channel `demo`) is exempt: its photos are generated by Muse Image and stamped as such and its reporters are invented, so it holds no personal data, and a deployed demo would otherwise show "deleted after the retention period" on every demo photo the day after seeding. Every real channel is purged. Reports, transcripts and assessments are kept. The bot's bilingual `/start` privacy notice states both configured retention periods. Names that reporters say when introducing themselves ("Ako po si …") are redacted before any text reaches a model.

## Deploy

Ulat runs on [Modal](https://modal.com) as **one always-on container** (commands and the production checklist:
[deploy/README.md](deploy/README.md)):

1. One FastAPI process serves the officer dashboard and, with `TELEGRAM_MODE=webhook`, the Telegram bot: Telegram
   posts to a secret-checked `/telegram/webhook/…` route, and the bot, its assessor and the retention purge run in
   the same event loop. `min_containers=1`, `max_containers=1`: one SQLite writer, no cold starts.
2. The live database is on the container's disk; a Modal Volume holds media, the model cache and database
   snapshots, taken with SQLite's backup API every 2 minutes and at shutdown, and restored at start.
3. A writer lease on the volume stops a restarting container from restoring before the old one's last snapshot.
4. Staging (`ulat-staging`, @UlatPH_dev_bot) and production (`ulat`, @UlatPH_bot) have separate apps, secrets,
   volumes and pseudonym salts. A fresh volume seeds the synthetic demo through the real pipeline.
5. The webhook is registered by an explicit command, never at startup, and local polling refuses to start while
   a webhook is set, so the two never fight over the bot.

- **Cost:** about $2.65 per week for the container (0.25 CPU, 512 MiB, always on, at Modal's list rates of
  $0.0000131 per core-second and $0.00000222 per GiB-second: [modal.com/pricing](https://modal.com/pricing)), plus
  model calls, capped at $10 a day on the public instance (`DAILY_SPEND_CAP_USD`) on top of the per-reporter
  limits.
- **Login:** every page and photo needs the officer passcode (report photos and texts are personal data under
  RA 10173). Only `/health`, `/login`, the static files and the webhook are open. The passcode opens a signed, HttpOnly,
  Secure, SameSite=Lax cookie for 12 hours; wrong passcodes are limited to 10 per 15 minutes per client address.
  Initials typed at login prefill the review forms; with a shared passcode they are self-declared.
- **Durability and loss window:** a stop, redeploy or preemption takes a final snapshot first, so nothing is lost
  (tested by force-stopping the staging container 4 seconds after a report arrived). A crash that skips shutdown
  loses at most the last 2 minutes of database writes. Snapshots: the newest 15 plus one a day for a week.
- **Judges:** open the URL in the Demo section and sign in with the passcode given in the submission's judging
  notes. The dashboard starts with the synthetic typhoon scenario; messages to the bot appear on it live.

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
| 2026-10-29 | Scope: the PCIC Notice of Loss is fisheries only for the hackathon (fisherfolk, fish cages and pens); crop insurance is later work. AGENTS.md and this README say so |
| 2026-10-29 | Livelihood facts: a report can state livelihood damage without a count (fish pens or cages, fishponds, fishing gear, boats, crops, livestock), a stated fishery area and a livelihood-materials need, with Taglish cues (baklad, kulungan, lambat, kawayan, bangka); the pair judge, incident rollups and RDANA 18.1/18.2 use them, so the two De La Paz fish-pen reports are no longer judged "no damage stated". Older stored facts keep working |
| 2026-10-29 | Deadlines table (`app/data/deadlines.json`): PCIC fisheries Notice of Loss (2 calendar days) and Claim for Indemnity (7), and RDANA forms to the EOC (72 hours), each row with its source; a countdown in Philippine time with an injected clock; the RDANA drafts page reads its deadline from the table, and a test keeps deadline literals out of the code |
| 2026-10-29 | PCIC fisheries Notice of Loss draft (FIP CAS Form No. 1) per incident, defined as commodity data: location, cause, first-reported time, stated area and extent filled from the reports with evidence links, identity and policy fields left for the assured, the PCIC Region IV office fixed, and an evidence annex for the adjuster (photo and SAM overlay links, judgments, SAM measurements as evidence only, name-redacted excerpts, trust chips, a red banner for a reused photo); deterministic, no model call |
| 2026-10-29 | Review fix: phone numbers are removed from Notice of Loss excerpts, and the agent's free-text photo notes are redacted like excerpts |
| 2026-10-29 | Filing clock: Notice of Loss and Claim for Indemnity countdowns from the first report on `/drafts`, the draft page and the incident page, with the Citizen's Charter first-notice fallback; approval reads "approved for filing, not filed" and does not stop the clock. Notice of Loss drafts reuse the versioning, staleness, approval, audit and same-origin rules (a draft now records its incident) |
| 2026-10-29 | Demo: an 18th synthetic report (a San Antonio fish-pen claim resending the De La Paz photo through a validated `photo_from`, no new image) so the reused-photo warning appears on a Notice of Loss; the loader assesses each report as it arrives, so only the later copy is flagged; `--anchor-now` places scenario times relative to the load so the filing countdowns are live |
| 2026-10-29 | Review fix (demo): the De La Paz fish-pen report and follow-up move to days 13–14 so its Notice of Loss clock is live after an anchored load; the loader ingests in scenario time order; the San Antonio copy gets its own lakeshore fish-pen place and text so it cannot merge with the San Antonio roof reports, and a scenario check requires a resent photo to have its own place; `--anchor-now` refuses to run on an already loaded demo |
| 2026-10-29 | Review fix: a Notice of Loss becomes stale when a photo of its incident is added or re-assessed (its fingerprint adds each photo's assessment id, media ids and update time); the RDANA fingerprint is unchanged |
| 2026-10-29 | Review fix: one redaction helper (`redact_personal`) for Notice of Loss excerpts, photo notes and place details also removes emails, @handles and Facebook/Messenger account names; the phone pattern no longer swallows ISO dates or decimal coordinates |
| 2026-10-29 | Review fix: the `/drafts` Notice of Loss summary lists the fields the agent fills and the fields left for the assured from each commodity's field definitions; the annex shows only the SAM concepts a commodity lists (fisheries: floodwater, no roof count) with a caveat that lake or pond water reads as floodwater |
| 2026-10-29 | Demo reset keeps the model cache as well as the generated images, so a reload does not pay again for photo judgments and SAM calls; updated CLI prompt, messages, help and regression test |
| 2026-10-29 | Officer dashboard design system: one stylesheet with colour, spacing and type tokens (16 px body text, AA contrast, visible focus rings, 40 px controls), an "Ulat" header bar with the active page marked (`aria-current`), status badges that always carry text, sticky table headers, tables that scroll inside a wrapper on narrow screens and become stacked cards under 640 px, and A4 print rules that drop the dashboard chrome |
| 2026-10-29 | Correction routing (fix from the live bot test): the pair judge now compares a report with every candidate incident up to `MAX_JUDGED_CANDIDATES` instead of stopping at the first match, and the best accepted match wins: an incident not waiting for review, then for a correction the incident stating the corrected figure, then confidence. A Casile "2 houses, not 3" correction had merged into an unconfirmed old-photo incident; re-grouping moves such a report, and the stored reason names why the incident won |
| 2026-10-29 | Late captions (fix from the live bot test): a text sent within 2 minutes of the same reporter's caption-less photo or image file, with no other report in between, becomes the photo's caption instead of a caption-only report; the bot confirms it, the photo is re-assessed (cached photo calls, new facts and barangay from the caption) and re-grouped, and the joined text is exempt from the rate limit. An assessment whose text or pin changed while it ran no longer ends "assessed" with stale facts: the report stays new for the queued re-run, also across a spend-cap hold |
| 2026-10-29 | Bot profile as data (`app/bot_profile.py`): the name, short description, description and command list for @UlatPH_bot in Taglish and English (what to send, Biñan typhoon and flood damage, reports go to the City DRRM Office, not an emergency line: call 911, the same privacy line as `/start`), with Telegram's length limits enforced by a test; an idempotent CLI compares the live values and sets only what differs (`--dry-run` prints the changes), and `--photo` uploads the avatar as a square JPEG; `/help` repeats `/start` |
| 2026-10-29 | Live dashboard: one database watcher per web process (`PRAGMA data_version` on a dedicated read-only connection, then per-topic tokens in one read transaction) fanned out over server-sent events (`/events`, typed topics, heartbeat, retry, no report text) with a polling fallback (`/events/token`); marked page sections refresh in place without wiping typed input, keep scroll position and highlight new rows; Live / Reconnecting / Offline indicator; streams close on Ctrl+C or reload so uvicorn never hangs |
| 2026-10-29 | Overview home page (`/`): key-figure tiles (open incidents, officer review, reports in 24 h and held, drafts awaiting approval, next filing deadline with a live countdown, model spend against the cap) that link to their pages and reuse those pages' queries and services (`app/dashboard.py`), the incidents needing attention and the latest reports; it updates live; a test checks every figure against the linked page |
| 2026-10-29 | Officer map (`/map`, Leaflet 1.9.4 vendored): barangay boundaries served from the COD-AB file (`/map/barangays.geojson`, cached with an ETag) and shaded where incidents are active; one marker per active incident at its reports' mean GPS point inside Biñan, else an "approximate" marker at the barangay centre (`app/geo.py` `centroid`); status by shape, number and a text legend; flagged pins outside Biñan only, never auto-panned to; a list of every position for keyboard, screen-reader and no-JavaScript use; live updates; a compact map on the overview; offline-safe when OSM tiles fail |
| 2026-10-30 | Honest bot copy: the public profile and `/start` say Ulat is a prototype being prepared for Biñan's disaster office, not yet an official city service and not monitored by the city; the name drops "Biñan" so it does not read as an LGU account ("Ulat · Bagyo at Baha (prototype)"); a test rejects any claim that reports reach the City DRRM Office. Not applied to the live bot yet |
| 2026-10-30 | Same-reporter correction rule (fix from the live bot test): a correction joins the candidate incident where its own reporter stated the figure it corrects, before any judge call, logged as a deterministic merge (no model) naming the corrected figure and report; the most recent qualifying report wins. Report 18 ("dalawang bahay lang") had merged into an old-photo incident because the judge was only unsure about the reporter's own voice note; a regression test replays that case. Grouping fingerprint version 3, so a re-run applies the rule |
| 2026-10-30 | Albums (gap from the late-caption fix): the photos of a Telegram album join one report instead of one report each (stored `media_group_id`, 2-minute window, same reporter, never for a blocked reporter), so a text after a caption-less album captions every photo; the bot answers "received" once per album, later album photos are exempt from the rate limit, and an album photo arriving mid-assessment keeps the report "new" for the queued re-run |
| 2026-10-30 | Officer review: a `/review` queue (header, overview tile) and an incident-page panel for reports the agent could not settle (uncertain or failed duplicate check, unknown place, unchecked trust signals, unconfirmed fallen trees) with the judge's comparisons and audited actions: merge, keep separate, set the barangay (then re-group), review a trust signal, confirm a fallen tree, block or unblock the reporter (the CLI's function). Each is an append-only `OfficerAction` row with the officer's initials; merges and keep-separate pin the grouping so re-grouping never undoes them, a set barangay survives re-assessment, counts, rollups, the pending bucket and draft staleness follow at once, and live sections refresh. The incident page shows agent and officer decisions together |
| 2026-10-30 | Reporter loop: after assessment the bot replies with a bilingual summary of what the agent understood (barangay or "send a location pin", depth, counts, needs, "Mali? I-reply…"), built deterministically from the redacted facts; one summary per report after a 15-second quiet period (photo + pin + caption → one), none for held, blocked or failed reports; a reply to the summary becomes a correction of that report and joins its incident by rule; the chat stays in memory only, never stored |
| 2026-10-30 | Paid-call spend cap for the eval and data scripts: every call is logged to a ledger separate from the app database (`DATA_DIR/eval-spend.db`), a free cache-only dry run prices the uncached calls before anything is paid, and a run refuses to start (or stops starting paid work) past `--spend-cap` (default $5); Spark Open on Baseten and Fireworks now logs its token cost at list price instead of 0, and model clients gain a cache-only mode |
| 2026-10-30 | One results file for every accuracy number (`data/eval/results.json`): dataset provenance, n, model and backend, Manila date, spend and code commit per eval x model x dataset, a typed loader (`app/eval_results.py`) for the dashboard, `--record` upserts from the eval scripts, and README tables generated from it and checked by a test |
| 2026-10-30 | Synthetic vision eval set: 40 Biñan-style scenes as data (`data/eval/vision_scenes.json`: roof damage levels, floods ankle- to chest-deep, debris, fallen trees, tarpaulins, fish pens, hard negatives such as wet roads and tarps on intact roofs) generated with Muse Image outside the repo ($0.40), every image checked by eye (3 labels changed, noted in `label_notes`) |
| 2026-10-30 | First recorded eval runs on both LLMs: photo judge + SAM 3.1 on 25 real Wikimedia typhoon photos and the 40 synthetic photos, and the pair judge on the 64 synthetic pairs, for Spark 1.3 and Spark Open (Baseten); $1.93 in total from the call log; "How well it works" section with the caveats (not Biñan photos, synthetic sets, fallen trees weak, SAM masks the lake as floodwater). Glimmer was skipped: no local endpoint is running |
| 2026-10-30 | Live demo replay (`python -m app.demo replay`): the scenario's reports arrive at real time into `var/demo-replay` with scaled, clamped gaps (about 3 minutes), through the same intake and assessment as `load`, so the live dashboard fills up on camera; the pair judge is shown story time so a take is answered from the cache with the same grouping every time; a cache-only dry run refuses uncached paid calls unless `--allow-paid`; Ctrl-C stops cleanly and a rerun resumes. Fix: a demo reset with a relative `DATA_DIR` no longer deletes the model cache it promised to keep |
| 2026-10-30 | Officer login: a shared passcode (constant-time digest check, per-client limit of 10 wrong tries per 15 minutes) opens a signed HttpOnly, SameSite=Lax session cookie (Secure over HTTPS, 12 hours, keyed to the session secret and the passcode); every route is private unless listed as public (`/health`, `/login`, the webhook, static files), so new pages are covered by default; live updates and htmx get 401 and send the page to the login; the app refuses to start on a non-localhost host without a passcode; optional initials prefill the review and decision forms. No new dependency (stdlib HMAC) |
| 2026-10-30 | Telegram webhook mode (`TELEGRAM_MODE=webhook`): the web app runs the bot in its own event loop; `POST /telegram/webhook/<derived path>` checks Telegram's secret header in constant time and queues the update, so updates are handled in order as with polling, and the assessor and the summary reply share the process; the dashboard stays up when Telegram is unreachable (503 until the bot starts). Polling refuses to start while the token has a webhook. Log lines mask every configured secret |
| 2026-10-30 | Durable SQLite for a wiped-on-restart container: the database stays on local disk and is snapshotted to a volume with SQLite's backup API every 2 minutes and at shutdown (atomic rename, volume commit, unchanged databases skipped, 15 newest plus 7 daily kept), restored at start, with a restore-by-name request and a writer lease so a restart never restores before the old container's last snapshot; tested with backup, restore, backup, restore round-trips and a fake volume |
| 2026-10-30 | Modal deploy (`deploy/modal_app.py`, `python -m app.deploy`): one always-on container per target (staging on the dev bot, production) with its own secret, volume, URL and spend cap; commands for secrets (from `.env`, never echoed), demo assets, deploying the committed HEAD, the webhook, snapshots, restore, logs and stop; a fresh volume seeds the synthetic demo through the real pipeline. Staging verified end to end: login, seeded overview, signed webhook update, a forced container restart that kept every report, no secret in the logs |
| 2026-10-30 | Agent trace plumbing: every logged model call now names the report, pipeline step (one enumerated list in `app/trace.py`: transcribe, facts, photo judge, SAM roof and floodwater, pair judge, RDANA summary, and the rule steps) and subject (the photo, the compared incident, the voice-note part) it served, set by the pipeline in a request-scoped context the call recorder reads, so no client signature changed; RDANA summary calls are linked to the draft they wrote in the draft's own insert. A test runs two assessments concurrently and checks that no call carries the other report's id; older databases get the nullable columns on startup |
| 2026-10-30 | Agent trace view: an expandable trace on every report row and incident timeline entry (`app/agent_trace.py`, `_trace.html`) with each step's Meta model family and id, host, latency, cache hit and cost beside its one-line result, rule steps and officer decisions labelled as such, repeats from a re-assessment counted, calls with no saved output still listed, per-report totals, a "not recorded" note for older reports, and the summary's model calls on RDANA draft pages; an open trace stays open through live refreshes. Built from batched queries (five per page, not per row) |
| 2026-10-30 | Accuracy page (`/accuracy`, `app/accuracy.py`): "How accurate is the agent?" rendered from `data/eval/results.json`, one table per task and data set with Spark 1.3 beside Spark Open, every entry's n (and failed items), provenance (real Wikimedia typhoon photos, synthetic Muse Image photos, synthetic pairs), date, commit and spend, scores below 0.6 marked weak, plain-language captions for a city officer on every score, and an empty state for a missing, empty or invalid file; in the header navigation, linked from the overview, behind the officer login |
| 2026-10-30 | "Meta models at work" on the overview: calls, cache-hit share, cost and median live-call latency per model over the spend cap's 24-hour window, in one query (window functions over the `at` index), Meta model family and host from the same table as the trace, live through the spend topic; a test checks every number against a direct SQL aggregate |
| 2026-10-30 | Retention fix: the synthetic demo's media (channel `demo`, generated photos, invented reporters, no personal data) is no longer purged after the TTL, so a deployed demo keeps its photo evidence the day after seeding; every real channel is still purged, tested on both sides |
| 2026-10-30 | Review fix: a report the bot acknowledged survives a restart or crash: the bot asks for a database snapshot after every update it handles (at once, at most one per 5 s, with the history thinned to keep its 1-minute spacing), and shutdown is bounded to fit Modal's 30 s: refuse new webhook calls with 503, handle the queued updates, snapshot, give in-flight assessments 8 s with the summary's quiet period skipped, then stop the bot |
| 2026-10-30 | Review fix (RA 10173): the retention purge no longer depends on the bot starting: in webhook mode the web app starts exactly one purge loop from its lifespan (a revoked token or unreachable Telegram no longer stops it, and start retries no longer pile up loops); the polling bot starts it with itself |

### Third-party code and assets

Licenses read from each installed package's metadata. The Python libraries are installed dependencies (see `pyproject.toml` and `uv.lock`); the only third-party code copied into this repository is the vendored, unmodified browser libraries marked below, each with its license file, so the dashboard needs no CDN. The one third-party data file is listed under Data below.

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
| [Leaflet](https://github.com/Leaflet/Leaflet) 1.9.4 (vendored in `app/static/vendor/leaflet-1.9.4/` with its LICENSE and control images, from the npm package, integrity checked) | BSD-2-Clause | The officer map |
| [htmx](https://github.com/bigskysoftware/htmx) 2.0.11 (vendored in `app/static/vendor/htmx-2.0.11/` with its LICENSE, from the npm package, integrity checked) | 0BSD | Swapping live-updated page sections in place |
| [FFmpeg](https://ffmpeg.org) (external program, not bundled) | LGPL/GPL depending on build | Voice note conversion to WAV |
| [Modal](https://github.com/modal-labs/modal-client) client and CLI (not a project dependency: installed separately for deploying, and provided by Modal inside the container) | Apache-2.0 | Hosting: `deploy/modal_app.py` |
| Ulat bot avatar (`assets/brand/ulat-avatar.png`), generated for this project with OpenAI's image model through the Codex CLI ("GPT Terra") | Output owned by the user under OpenAI's [terms of use](https://openai.com/policies/terms-of-use/) | Branding only: the Telegram bot's profile photo. Not part of the report pipeline |

Data:

| Name | License | Used for |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) map tiles (`tile.openstreetmap.org`, loaded by the browser, not stored here) | [ODbL](https://opendatacommons.org/licenses/odbl/) data; tiles CC BY-SA, attributed "© OpenStreetMap contributors" on the map | Basemap of the officer map, under the [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) (light, interactive use) |
| [OCHA COD-AB Philippines](https://data.humdata.org/dataset/cod-ab-phl) admin level 4 (sources: PSA, NAMRIA), v03, valid on 2025-02-13 | [CC BY-IGO](https://creativecommons.org/licenses/by/3.0/igo/) | Barangay names, codes and boundaries for Biñan in `app/data/binan_barangays.geojson` (24 polygons, clipped and simplified to about 3 m). OCHA notes the boundaries are indicative, not official |
| 25 Wikimedia Commons photos of Philippine typhoon damage (eval set `kill-test-25`; **not copied into this repository**; only aggregate metrics are) | CC BY 2.0, CC BY-SA 3.0/4.0 or public domain, per photo | Photo-judge and SAM accuracy in "How well it works". Authors and sources: DFID - UK Department for International Development (CC BY 2.0): [img01](https://commons.wikimedia.org/wiki/File:A_car_up-ended_amid_the_wreckage_of_buildings_destroyed_by_Typhoon_Haiyan_in_Tacloban,_Philippines_(11290331544).jpg), [img02](https://commons.wikimedia.org/wiki/File:A_few_palm_trees_remain_standing_amid_the_destruction_caused_by_Typhoon_Haiyan_in_the_city_of_Tacloban,_Philippines_(11290331484).jpg), [img03](https://commons.wikimedia.org/wiki/File:A_man_stands_surrounded_by_the_devastation_wrought_by_Typhoon_Haiyan_in_the_city_of_Tacloban_(11290380063).jpg), [img06](https://commons.wikimedia.org/wiki/File:Rebuilding_after_Typhoon_Haiyan,_in_Guinan,_Philippines_(13943379519).jpg); Trocaire from Ireland (CC BY 2.0): [img04](https://commons.wikimedia.org/wiki/File:Tacloban_Typhoon_Haiyan_2013-11-14.jpg); Master Sgt. Antoine Robinson (Public domain): [img05](https://commons.wikimedia.org/wiki/File:Operation_Damayan_131123-M-IE150-015.jpg); Exec8 (CC BY-SA 4.0): [img07](https://commons.wikimedia.org/wiki/File:Flooding_in_Culiat_Creek_in_Project_8,_Quezon_City_Philippines_from_Typhoon_Ketsana_(Ondoy).webm), [img08](https://commons.wikimedia.org/wiki/File:Flooding_in_Hereford_Street,_Project_8,_Quezon_City,_Philippines_from_Typhoon_Ketsana_(Ondoy)_1.webm); Exec8 (Public domain): [img09](https://commons.wikimedia.org/wiki/File:Ketsana_002.jpg); Philippinepresidency (CC BY-SA 3.0): [img10](https://commons.wikimedia.org/wiki/File:Ondoy4.jpg); The Philippine Online Chronicles from The Philippines (CC BY 2.0): [img11](https://commons.wikimedia.org/wiki/File:Ondoy_(3967857165).jpg); IRRI Images (CC BY 2.0): [img12](https://commons.wikimedia.org/wiki/File:Typhoon_Ketsana_flood_near_Laguna_de_Bay.jpg); PaulGorduiz106 (CC BY-SA 4.0): [img13](https://commons.wikimedia.org/wiki/File:Damaged_tree_(12-24-2021).jpg), [img16](https://commons.wikimedia.org/wiki/File:Damaged_trees_(12-17-2021).jpg); ALFRED FRIAS / PRESIDENTIAL PHOTO (Public domain): [img14](https://commons.wikimedia.org/wiki/File:Photo_of_Typhoon_Odette-battered_areas_in_Tisa,_Cebu_City_01.jpg), [img15](https://commons.wikimedia.org/wiki/File:Photo_of_Typhoon_Odette-battered_areas_in_Tisa,_Cebu_City_02.jpg); Martin Michlmayr (CC BY-SA 4.0): [img17](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_in_Cebu_City_2021_12_022.jpg), [img18](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_001.jpg), [img19](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_005.jpg), [img20](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_006.jpg), [img21](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_009.jpg), [img22](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_025.jpg), [img23](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_016.jpg), [img24](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_018.jpg), [img25](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_020.jpg) |

## Repo structure

```
AGENTS.md              Instructions for AI coding agents (Muse Code, Claude Code, …)
.agents/skills/        Provider notes for coding agents: baseten, fireworks, modal (SKILL.md each)
README.md              This file
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
  gazetteer.py         Biñan's 24 barangays: names, aliases, matching in text, transcription keywords
  geo.py, data/        Point-in-barangay lookup on COD-AB boundaries; photo EXIF GPS
  transcript_cleanup.py  Removes repeated-word artifacts from transcripts
  facts.py, photo.py   Fact extraction; photo judgment, SAM measurement and evidence overlay
  assessment.py        Assesses a report and maps it to a barangay (python -m app.assessment)
  incidents.py         Candidates, deterministic rollups, audited grouping and rebuild CLI
  counting.py          Per-barangay evidence and the no-double-counting rule
  review.py            Officer review queue and audited overrides (merge, keep, place, trust, trees, block)
  rdana_form.py        The 2021 RDANA form as data (sections, numbering, fill modes)
  rdana.py             Evidence rules that fill the form; rdana_summary.py: the validated 1.4 summary
  nol_form.py, nol.py  PCIC Notice of Loss commodities as data (fisheries) and the deterministic filling
  drafts.py            Versioned drafts, approve/reject, audit, damaged-houses table and CSV
  deadlines.py         Filing deadlines from data/deadlines.json (with sources) and countdowns
  pair_judge.py        Text-only pair schema and redacted evidence prompts
  demo/                Synthetic demo: scenario, Muse Image photos, loader (python -m app.demo)
  privacy.py           HMAC pseudonymization of reporter ids
  audio.py             ffmpeg conversion and chunking for voice notes
  intake.py            Ingest text, photo, voice and location into reports
  retention.py         Purge raw media after the TTL
  bot.py               Telegram bot (python -m app.bot)
  reporter_summary.py  The reporter's "here's what we understood" reply and its in-memory acknowledgements
  bot_profile.py       The bot's public profile (name, descriptions, commands) as data; idempotent CLI that applies it
  main.py, templates/  FastAPI officer pages: reports, incidents, timelines, decision audit, drafts and media
  live.py              Live updates: one database watcher per process and the server-sent event streams
  dashboard.py         Read models shared by the officer pages (overview figures, active incidents)
  mapdata.py           Map features: incident markers (mean GPS or approximate barangay centre), flagged outside pins
  static/              Dashboard stylesheet (css/app.css, design tokens), script (js/app.js), favicon and
                       vendor/ (pinned browser libraries with their licenses)
  evaluation.py        WER, Cohen's kappa and F1 helpers
scripts/
  eval_transcribe.py   WER on your own recordings (outside the repo)
  eval_vision.py       Photo judge and SAM vs hand labels, per agent LLM
  eval_facts.py        Fact extraction from voice notes vs hand labels, per agent LLM
  eval_dedupe.py       Cached pair-judge P/R/F1, review rate and per-pair misses
deploy/                Modal app (modal_app.py) and the deploy guide (README.md)
assets/brand/          Bot avatar (branding only)
data/demo/scenario.json  The synthetic typhoon scenario (no real people)
tests/                 Mocked-HTTP tests and synthetic dedupe_pairs.json (64 labelled pairs)
```

## Team

- Ronnie Cutamora

## License

_TBD (choose before submission; the repo must be public)._
