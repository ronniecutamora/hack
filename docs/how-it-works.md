# How Ulat works

Ulat turns photos, short videos, audio, voice notes and text about typhoon and flood damage into evidence-backed RDANA drafts, damaged-houses tables and PCIC fisheries claims for an officer to review. It is built for Biñan's CDRRMO and fisherfolk; rice claims are a per-office option and are off for Biñan until its rice area is verified.

**Judge links:** [Live dashboard](https://ronniecutamora47--ulat.modal.run) (passcode in the private judging notes) · [Telegram bot @UlatPH_bot](https://t.me/UlatPH_bot) · [YouTube link: added at submission]. Ulat is a prototype, not an official Biñan City service; it is not an emergency hotline.

**Meta models:** Muse Spark 1.3 and Muse Spark Open extract facts, judge photos, group reports and draft summaries; Muse Voice Transcribe reads speech; SAM 3.1 outlines roofs and floodwater as evidence; Muse Image creates synthetic demo imagery.

**Measured on small labeled sets:** [recorded results and limitations](../data/eval/results.json); see [How well it works](evaluation.md#how-well-it-works) for full scores and caveats.


## Try it in 2 minutes

1. Open the dashboard with the passcode in the private judging notes. Home shows the officer's work queue.
2. Open **Review** to inspect uncertain reports, then **Drafts** to open an RDANA or fisheries claim and its print view.
3. Switch to **Technical** and open **Accuracy** to see the labeled results; inspect a report's agent trace to see the models and their evidence.
4. Optional: send the bot a synthetic text, photo, voice note, audio file or short video. The bot is not monitored for emergencies.

---

## The problem

The Philippines has the highest disaster risk of
[193 countries in the WorldRiskReport 2026](https://www.welthungerhilfe.org/fileadmin/pictures/publications/en/studies_analysis/2026-worldriskreport-beh.pdf).
[About 20 tropical cyclones a year](https://www.pagasa.dost.gov.ph/climate/tropical-cyclone-information) enter its
area of responsibility, and 8 or 9 cross the country. Six storms in a row in October and November 2024 affected
[more than 13 million people](https://reliefweb.int/report/philippines/philippines-six-tropical-cyclones-100-days-after-26-february-2025).

Lake floods last. After Typhoon Ondoy in 2009, areas near the Laguna de Bay shore
[stayed under water for more than a month](https://dil-opac.bosai.go.jp/publication/nied_natural_disaster/pdf/45/45-04E.pdf).
Biñan sits on that shore. After Severe Tropical Storm Paeng in 2022, DSWD counted
[2,088 affected families in Biñan on 4 November](https://dromic.dswd.gov.ph/wp-content/uploads/2022/11/DSWD-DROMIC-Report-20-on-Severe-Tropical-Storm-Paeng-as-of-04-November-2022-6PM.pdf),
[20,087 on 18 November](https://dromic.dswd.gov.ph/wp-content/uploads/2022/11/DSWD-DROMIC-Report-40-on-Severe-Tropical-Storm-Paeng-as-of-18-November-2022-6AM.pdf),
and [575 families still displaced on 24 December](https://dromic.dswd.gov.ph/wp-content/uploads/2022/12/DSWD-DROMIC-Report-68-on-Severe-Tropical-Storm-Paeng-as-of-24-December-2022-6AM.pdf).

The bottleneck is paperwork. The city's disaster office has to turn scattered photos, calls and Taglish voice notes,
by hand, into the RDANA Form 1
([within 72 hours](https://ndrrmc.gov.ph/wp-content/uploads/2026/05/09-22-2021-NDRRMC-Memorandum-No.-107-s-2021-NDRRMC-Rapid-Damage-Assessment-Needs-Analysis-RDANA-Standard-Operating-Procedures.pdf)
of the assessment team's deployment) and the damaged-houses counts that DSWD's DROMIC reports roll up. An insured
fish farmer whose cage or pen was hit has
[2 calendar days](https://pcic.gov.ph/wp-content/uploads/2024/09/Fisheries-Insurance-2024-V4.pdf) to send PCIC a
written Notice of Loss. Because the water stays for weeks, the same streets are reported again and again. Ulat is a
prototype for that gap; the city has not adopted it.

## What it does

1. Residents send photos, short videos, audio files, voice notes, texts and location pins; the agent extracts damage facts and maps the report to a barangay. Reporters often send a photo and then type its words: a text within 2 minutes of the same reporter's caption-less photo, with nothing in between, becomes that photo's caption (the bot answers "Idinugtong sa iyong larawan. / Added to your photo."), and the photo is re-assessed and re-grouped with it, like a late location pin. Photos sent as one Telegram album (one `media_group_id`) are one report: one "received" reply, every photo assessed, and a text after a caption-less album captions all of it.
2. Deterministic place/time/GPS filters find candidate incidents. A text-only Meta pair judge decides whether reports describe the same situation; uncertainty stays separate for officer review. Every candidate is judged, not just the first, so a correction ("mali po… dalawang bahay lang, hindi tatlo") lands in the incident that holds the figure it corrects.
3. Officers read incident timelines, depth changes and the stored reasons for grouping, and resolve what the agent could not settle in a review queue: merge, keep separate, set the place, check trust signals, confirm fallen trees, block a reporter (see [Officer review](#officer-review)). Reported totals explicitly warn about double-counting, including corrections.
4. The agent drafts each barangay's RDANA form and the city's damaged-houses table from that evidence; the officer checks report-based values against their linked reports, then approves or rejects the draft (see [Officer outputs](#officer-outputs)).
5. For each incident of fish cage, fish pen or fishpond damage, the agent drafts the fisherfolk's PCIC Notice of Loss with an evidence annex for the adjuster and counts down its 2-day filing deadline; the officer approves it for filing, and the assured files it (see [Insurance: fisheries Notice of Loss](#insurance-claim-form-for-fish-farmers-pcic-notice-of-loss)). For a flooded or flattened rice field it drafts the farmer's PCIC Claim for Indemnity (RC-CAS-01) the same way, with its 20-day clock, where the office turns rice on (see [Insurance: rice Claim for Indemnity](#insurance-claim-form-for-rice-farmers-pcic-claim-for-indemnity)). Corn and other crops are later work.

Every report on `/reports` and on its incident timeline has an expandable **agent trace**: each pipeline step in order (transcribe, read the facts, judge each photo, SAM roofs and floodwater, screen the message, find the barangay, check trust signals, group into an incident) with the Meta model that ran it, its latency, whether it came from the cache and its estimated cost, next to the one-line result it produced ("roof damage: partial", "2 roofs found", "barangay Malaban (photo GPS)", "different from incident #2, 96% → kept apart, in incident #3"). Deterministic steps are labeled "Rule, no model", officer overrides follow as "Officer" steps, and the totals give the report's model time and cost. The trace shows only derived results the dashboard already shows, never prompts, replies or the officer's notes. An RDANA draft page lists the model calls that wrote its summary. Reports assessed before 2026-10-30 say "trace not recorded". The call log records completed responses and cache hits, but failed requests and individual retries are not necessarily separate entries ([app/meta/base.py](../app/meta/base.py)). How often each model is right is on the dashboard's `/accuracy` page (see [How well it works](evaluation.md#how-well-it-works)).

## Meta models used

| Model | What it does in this project | Status |
|---|---|---|
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Turns Telegram voice notes (Tagalog, English, Taglish) into text, biased with `keywords` toward Biñan's 24 barangay names and then 471 OpenStreetMap street, subdivision and landmark names, up to the API's 500 | In use |
| Muse Spark 1.3 (`muse-spark-1.3`) | The agent LLM (default): judges each photo as structured JSON (roof damage, floodwater, debris, fallen tree, tarpaulin, house count) and extracts report facts (flood depth, houses, evacuees, crops, needs); default incident pair judge; writes the RDANA summary description (1.4), checked against the evidence | In use |
| Muse Spark Open (`meta-models/muse-spark-open`, Baseten) | The same agent steps and incident pair judgments on open weights, photos included: set `AGENT_LLM=muse-spark-open` | Available; staging matching switch held after two follow-ups grouped differently |
| Muse Spark Open (`accounts/fireworks/models/muse-spark-open`, Fireworks) | Agent or independent pair judge via `muse-spark-open-fireworks` | Implemented; mocked HTTP verified, no live call in this slice |
| Muse Glimmer (`accounts/fireworks/models/muse-glimmer-30b`, Fireworks) | Pair-judge comparison via `muse-glimmer` | Implemented; serverless availability ⚠️ contradictory and unverified; explicit availability errors, no fallback |
| Muse Image (`muse-image-1.0`) | Synthetic, clearly labeled demo photos for the typhoon scenario and the 40-photo synthetic eval set (no real homes or people); every image is stamped "AI-generated (Muse Image) · synthetic demo" | In use (demo and eval data) |
| SAM 3.1 (`sam-3.1`) | Measures what the LLM judges: counts roofs and measures floodwater from masks, drawn on the photo as evidence | In use |

Every LLM call goes through `AgentLLM`. `AGENT_LLM` selects fact extraction and photo judgment; `PAIR_JUDGE_LLM` independently selects the incident judge and defaults to `AGENT_LLM` when omitted. Both accept `muse-spark-1.3`, `muse-spark-open` (Baseten), `muse-spark-open-fireworks` or `muse-glimmer` (Fireworks). Provider configuration comes from the local [Baseten skill](../.agents/skills/baseten/SKILL.md) and [Fireworks skill](../.agents/skills/fireworks/SKILL.md), which cite the provider sources. No live provider compatibility or accuracy is claimed by mocked tests.

Incident candidates share a barangay and activity within `INCIDENT_WINDOW_DAYS` (14 by default). GPS within 500 m can add candidates when a barangay is missing, and distance vetoes distant pairs when both sides have points. Known different barangays never match. The judge compares the report with every candidate up to `MAX_JUDGED_CANDIDATES` (4 by default), and only `same` at or above `PAIR_MERGE_THRESHOLD` (0.85 by default) is accepted. When several candidates are accepted, the report joins an incident that is not waiting for officer review; a correction joins the incident whose reports state the figure it corrects; then the higher confidence wins, and the merge decision's stored reason says why it beat the others. An unsure or low-confidence result does not stop the other comparisons: the report stays separate with `needs_officer` only when no candidate is accepted, and a judge failure merges nothing. One rule runs before the judge: a correction ("mali po yung sinabi ko kanina…") joins the candidate incident where the same pseudonymous reporter stated a figure it corrects (the most recent such report), with no model call and a logged reason such as "Correction by the same reporter of the houses partially damaged figure in report 8". These are configurable policy choices, not calibrated accuracy claims. Pair prompts use typed facts, regenerated summaries and photo captions, and redacted place details; they omit raw text, transcripts, reporter pseudonyms, images and free-form photo notes.

Grouping runs after successful assessment under the same per-report lock, without a database transaction across a model call. A changed late pin re-links the report; an empty old group is retired from the list while its audit remains readable. Unchanged grouping is a no-op. `AgentDecision` stores the actual judge model, compared incident/report, confidence and reason; pages do not regenerate explanations. Judge failures stay visible for officer review and can be retried explicitly. Existing slice-2 SQLite files receive grouping columns on startup without losing reports.

### The reporter's side (Telegram bot)

Ulat is a hackathon prototype that Biñan's disaster office has not adopted, and the bot says so. Its public profile
(`app/bot_profile.py`, applied with `python -m app.bot_profile`) reads, verbatim:

- **Name:** Ulat · Bagyo at Baha (prototype)
- **Short description:** Prototype: mag-report ng pinsala ng bagyo at baha. / Prototype damage reports, not official.
  Emergency? Call 911.
- **Description:** Prototype para sa Biñan, hindi pa opisyal o binabantayan ng LGU: magpadala ng larawan, maikling
  video, voice note, text o location ng pinsala. / A Biñan prototype, not yet official or monitored by the city: send
  photos, a short video, voice notes, text or location. — Hindi ito emergency hotline. Para sa rescue, tumawag sa
  911. / Not an emergency line. For rescue, call 911. — (the `/start` privacy line)

`/start` and `/help` carry the same prototype notice. A test keeps every public text free of claims that reports
reach the City DRRM Office.

**Short videos and audio files.** Telegram videos, round video notes, video documents, audio files and audio documents
join the same report and agent as photos. A mixed photo/video album is one report, with the same caption, location,
rate, block and spend rules. Files over Telegram's 20 MiB download limit, video over `VIDEO_MAX_SECONDS` (90 seconds
by default), or audio over `AUDIO_FILE_MAX_SECONDS` (600 seconds by default) are refused before a report is stored.
The server checks the actual duration as well as Telegram's metadata. ffmpeg extracts up to `VIDEO_KEYFRAMES` (default
3, maximum 4) evenly spaced frames; the existing photo judge, SAM measurements and photo-reuse checks apply to each.
Frames are labeled with their timestamp and clip duration in Reports and Review. Audio uses the same Voice Transcribe
language bias, place keywords and WAV chunking as voice notes. A clip without an audio track skips transcription.

The original video is kept only in a temporary directory while it is processed, and extracted or uploaded audio is
deleted after successful transcription. If processing is held or fails, the audio needed for a retry follows the
existing raw-media retention limits (24 hours normally, up to 72 hours for a spend-cap hold). Frames and their
overlays follow the photo retention rule. Temporary files left by a crash are swept as orphans. No face or voice
identification is used.

**Why still frames, not SAM video tracking?** [Meta's published standard prices](https://dev.meta.ai/docs/pricing-rate-limits)
are $0.0025 per image and $0.0002 per video frame, with a minimum of 50 frames per video request. At the default
three keyframes, roof and floodwater measurements cost **$0.015 per video** (6 image requests), compared with at least
**$0.020** for two video-tracking requests. For a 23-second clip at 30 fps, tracking all 690 frames for both concepts
would cost **$0.276**. These are SAM charges only: Spark judgment and any transcription cost extra. Tracking provides
motion continuity, but this workflow needs a few officer-reviewable stills; we have not established that tracking adds
enough evidence to justify its extra cost. The comparison changes if the number of stills or tracking frames changes.

The [video evaluation](evaluation.md#video-evaluation) includes the labeled clips, results and limits.

**"Here's what we understood."** When a report's assessment finishes, the bot answers in the same chat with what the
agent took from it, built from stored facts and photo judgments with no extra model call: the barangay (or "hindi pa alam ang lugar /
place not identified yet: send a location pin"), the flood depth, the counts and the needs, ending "Mali? I-reply ang
tamang detalye. / Wrong? Reply with the correct details." The place detail goes through the same redaction as every
output, so the reply never repeats a name or phone number, not even the reporter's own. A report must stay quiet for
15 seconds after its assessment before the summary goes out, so a photo, its pin and its caption get one summary,
not three; held, blocked and failed reports get no facts back (only the earlier "received"). A reply to a summary
within 24 hours is stored as a correction of that report (`Report.corrects_report_id`, the same reporter only) and
joins its incident by rule, even when it names no place ("dalawa lang po"). **Privacy:** the chat id is never stored;
it lives only in the bot process's memory (inside the message being answered), so after a restart pending summaries
are skipped and replies to older summaries are ordinary reports. Rate limits are unchanged.

**Not a report? A kind nudge, not a report.** Weekend tests of the live bot showed "test", "hahaha", keyboard mash,
emoji and stickers becoming reports on the dashboard. Every message is now screened before it can become one
([app/screening.py](../app/screening.py)), precision first, because dropping one real "baha dito, tulong" is worse than
a dashboard full of "hahaha":

1. **A free rule, no model call.** A text made only of emoji, punctuation, keyboard mash ("asdfghjkl", "jsjsjs"),
   laughter, a greeting, thanks or "test" is stored as `not_a_report` and never reaches a model. The rule never drops
   a message with a photo, a voice note or a location pin, any digit ("3" in a reply), a distress emoji (🆘, 🌊) or
   any word outside that short list, however short or misspelled ("baha", "lubog n bhay nmin", "SOS").
2. **A Meta model verdict, only when nothing says "report".** The facts call already reads every text and
   transcript. When it finds nothing a report states (no hazard, flood, count, need, crop or livelihood loss,
   place or correction) and no photo shows damage, Muse Spark is asked one small question: is this a disaster
   report, yes, no or unsure? Only a certain "no" screens the message out; "unsure" stays a report with an amber
   "maybe not a report" trust signal for the officer. Real reports state facts, so they never pay for this call, and
   the photo judge's "nothing seen" is only supporting evidence, never a verdict on its own. A reply to the bot's
   summary is a correction by intent and is never screened out by the model.

A screened-out message is **kept, not deleted, and not counted**: it is out of incidents, counts, RDANA drafts, the
damaged-houses table, Notices of Loss, the map, the overview tiles and the bot summary (one predicate,
`app.screening.counted`, used everywhere), shows on `/reports` as "not a report", and follows the same retention
purge as every report. Its agent trace says why ("Screen the message · Rule, no model · not a report (keyboard
mashing)"). The officer can restore it (see [Officer review](#officer-review)). The reporter gets, at most once per
15 minutes, "Salamat sa mensahe! Para maging report ito, sabihin po kung saan … at ano ang nangyari …, o magpadala
ng larawan o voice note. / Thanks for your message! To make it a report, tell us where … and what happened …, or send
a photo or a voice note." Stickers and GIFs get the same nudge and are not stored. Messages sent before the screen
existed are screened by the free rule with `uv run python -m app.assessment --screen` (no model call; a
screened-out report leaves its incident with a logged reason), or on a deploy with `uv run python -m app.deploy
screen --target staging` (snapshot first, `--dry-run` to preview; see [deploy/README.md](../deploy/README.md)).

## Spam and fake reports

Ulat never sends aid or files a claim by itself: every output is a draft an officer approves, and PCIC
adjusters validate losses in the field. So the risks are **credit drain**, **officer overload**,
**poisoned incident numbers** and **insurance fraud**. The agent handles them in layers; only the
not-a-report screen's verdict, asked for messages that state nothing, costs a model call:

| Layer | What happens | Default |
|---|---|---|
| Not-a-report screen | Greetings, tests, laughter, emoji and keyboard mash are kept as `not_a_report` by a free rule, before any model; a message whose facts state nothing gets a yes / no / unsure verdict from Muse Spark and only "no" screens it out. Never counted; the officer can restore it ([details](#the-reporters-side-telegram-bot)) | One nudge per reporter per 15 min |
| Rate limit | Each pseudonymous reporter is checked *before* a file is downloaded. Over the limit, the bot replies (Tagalog / English) to try later and to call 911 for emergencies. A late pin or caption that joins the sender's last report is not a new report and is not limited | 20 reports per hour, 60 per 24 h (`REPORTS_PER_HOUR`, `REPORTS_PER_DAY`) |
| Spend cap | Model costs logged in the last 24 h are checked before every assessment. Over the cap, new reports are stored as `held` and assessed later by `python -m app.assessment --pending` | $10 (`DAILY_SPEND_CAP_USD`) |
| Block list | `python -m app.trust block <pseudonym-prefix>` (and `unblock`). Messages are still stored as `held` for the officer but never reach a model | — |
| Trust signals | Rule-based flags on each report, shown as red / amber / gray chips: **photo reused** by another reporter (SHA-256 or a 64-bit difference hash, so resized or recompressed copies match), **old photo** (EXIF capture time), **pin outside Biñan**, **pin and text name different barangays**, and **Spark and SAM disagree** on a photo (Spark sees floodwater or a partly damaged roof that SAM 3.1 cannot outline; `app/crosscheck.py`), which Simple mode's Review asks as "Is there floodwater?" with the photo and its outline side by side | Photo older than 3 days (`PHOTO_MAX_AGE_DAYS`) |
| Corroboration | Each incident shows how many *different* reporters it has; one reporter is labeled **single source** | — |

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
"sic"), printable on A4 in the paper form's own layout. The form is defined once as data in [app/rdana_form.py](../app/rdana_form.py); each field is
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
law and order and the overall assessment, is an empty input the officer fills in on the draft (see "Filling in a
draft" below), and a blank ruled line or empty boxes on the printout. Missing evidence is never turned into "No"
or "0". The evidence for each report-based value links to its incidents and reports, with the reporter count,
a "single source" chip and flagged-report chips. A report in an incident links to its place on the incident
timeline; any other report links to `/reports?around=<id>`, which shows the reports around it however old it is.

**Filling in a draft.** Every field of the RDANA form and the Notice of Loss is a typed input on the draft page
(`app/fillable.py`; the control, the form's own options, a plain hint, whether only the office can answer it and
whether it is personal are data in `app/rdana_form.py` and `app/nol_form.py`): text, long text, numbers with
limits, dates and date-times that may not be in the future, single and multiple choice. Values from the reports
stay editable with their evidence links; an officer's change shows "Edited by RC · 14:20" with the value it
replaced one tap away and a button to go back to it. Saving checks every value on the server (refused values come
back with the message under the field and nothing typed lost) and makes the next version; the audit records the
initials, the time and which fields changed (keys only, never values). Approve and reject name the exact version;
editing an approved version makes a new one that needs approval again. When new reports make a draft stale,
regenerating keeps the officer's entries and says under each one if the reports now say something else. Simple mode
starts with **What you need to fill in** (event name, type and date, the overall assessment and its justification,
who submits it; for a Notice of Loss the date, type of policy, loss time, area, harvest date and underwriter) with a
progress line, then the values to check, with the questions for the RDANA team on site folded away.
Personal details for the printout are optional and start folded. With JavaScript, Simple mode offers one next
action: **Save changes** while stored fields have unsaved edits, **Approve for filing** after saving, and **Print
final form** after approval. Opening **More options and evidence** gives the saved printout, rejection, earlier
drafts, audit, linked reports and, when needed, regeneration. Safety warnings remain visible. With JavaScript off,
Save and Approve remain available as ordinary forms; save edits before approving.

**Help on every field.** A "?" beside each label (both views, never on paper) opens a short explanation: what to
enter, an example, and where it comes from. It is a bottom sheet on a phone and a popover beside the button on a
wider screen (a native popover: Escape or a tap outside closes it, and it works without JavaScript). The text is data
on each field (`help` and `help_source` in `app/rdana_form.py` and `app/nol_form.py`), checked at start-up (every
field has one, at most 240 characters). Sources are the official documents: where one explains a field (the note
under RDANA section 2, the heading of section 20, SOP section 7.5.2, the PCIC fisheries brochure, the PCIC
application form and farm plan), the help cites its page or section; otherwise it only says what the form's own
label asks and cites the Annex I page that prints it. 9 of the 108 RDANA fields and 7 of the 14 Notice of Loss
fields cite a document; the rest cite the form's own label.

**Personal fields are never stored (RA 10173).** Names, ages, phone numbers, emails, addresses, lot and policy
numbers (RDANA 1.3 persons interviewed, "submitted by" name and contact; the assured's details on a Notice of Loss)
are typed into the page for the printout only: their inputs have no `name`, so the browser never posts them, they
stay in that browser tab (sessionStorage, per form, so they survive a reload and a saved new version) until it
closes, and the server drops any that arrive. Each says "Saved only on this device for printing. Not stored by
Ulat."; the first one typed in a tab shows a shared-computer warning, and **Clear personal details** wipes them at
once. Left empty, or with JavaScript off, they print as blank lines to write by hand.

**Printing.** Fill the whole form on screen, then print it: **Print final form** on an approved version, **Print
draft for checking** before. The printout is the saved version, so the button first says if changes are not saved
yet (Save first, or print without them), then lists the office's questions that are still empty ("3 fields are still
empty": fill them, or print anyway); otherwise it prints at once. It prints the paper form alone (`app/templates/_paper.html`, laid out by
`app/paper.py`), audited element by element against the official scans: the RDANA form as Annex I's four A4 pages
(gray title bar, ruled boxes, two-column cells, option columns, the 6.3 and 7.2 tables, the 15.3 Yes/No matrix, the
"Submitted by" grid and the paper's page breaks) and the Notice of Loss as the one-page PCIC letter (FIP CAS Form
No. 1: "Regional Office No.", the date and type-of-policy captions under their lines, 12 particulars on ruled lines,
the signature block). Every label and option prints in the paper's own words, kept as data in
`app/rdana_form.py` and `app/nol_form.py` (`paper_label` only where the screen needs other words); options renamed
to the paper's wording still tick from entries saved in the old wording. The only words Ulat adds are one line in
the bottom page margin: "DRAFT, NOT APPROVED · version N · date" or "APPROVED · version N · date · initials", with
"page N of M" (Chrome and Edge print it; Firefox and Safari don't print page-margin text). For a Notice of Loss with
photos, the print dialog offers **Include the photo evidence page** (off by default): one page after the form,
titled as an attachment and not part of the PCIC form, with the photos, Ulat's measurements and the note that they
are evidence for the adjuster, not the loss figure. The app bar, navigation, buttons, hints, provenance notes,
evidence links, the on-screen annex and the trace never print.
The fisheries form says the measurements can be included on that separate page, whether or not it is selected;
older generated wording is corrected when displayed without changing the saved draft.

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
include pending figures. **Who may approve:** every page and action sits behind the
officer login (one shared passcode; see [Deploy](developer.md#deploy)); only a localhost dev server without `OFFICER_PASSCODE`
runs without it. Every state-changing request (generate, approve, reject) must also be same-origin: when the
browser sends an `Origin` or `Referer` header it must name this host, or the app answers 403, so another site open
in the officer's browser cannot post to it. The app answers only to the host names in `ALLOWED_HOSTS` (default
`localhost,127.0.0.1`; other names get 400), so a DNS rebinding site cannot pose as this host.

### Insurance claim form for fish farmers (PCIC Notice of Loss)

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
| 5 Date/time of loss | The earliest report in the incident, labeled "first reported": the loss may be earlier |
| 6 Total area affected | Only an area a report states; never derived from photos or SAM masks |
| 9 Extent of damage | The livelihood damage and counts as reported, and the photos with the agent's judgments and SAM 3.1 measurements, labeled "evidence for the adjuster, not the loss figure". The PCIC loss bands (total 90% and above, partial over 10% and below 90%) are shown; the agent never classifies the loss |
| Filled in with the assured | Date, type of policy, 5 date/time of loss, 6 area, 8 expected harvest date, 11 underwriter: entered on the draft and saved as a version. 1 name, 2 address, 7 lot number, 10 policy number (also in the opening line), 12 contact number: typed for the printout only, never stored. Signature by hand |

An **evidence annex** for the PCIC adjuster lists every report in the incident: its time, links to the photo and the
SAM overlay (until retention deletes the raw files), the photo judgments and the floodwater measurement (roof counts
are hidden for fisheries, and a caveat says SAM's "floodwater" also covers the lake or pond water around pens), an
excerpt of the message or voice note with names and contact details (phone numbers, emails, @handles, Facebook or
Messenger account names) removed, and its trust chips. A photo another reporter already sent (`photo_reused`) is a
red chip linking the earlier report and a red banner on the draft: one photo cannot prove two losses. Single-source
incidents, missing photos and grouping still pending officer review are marked too.

**Deadlines.** Every deadline comes from one table, [app/data/deadlines.json](../app/data/deadlines.json), each row with
its source; adding a row (crops later) is data only.

| Deadline | Window | Counted from | Source |
|---|---|---|---|
| PCIC fisheries Notice of Loss | 2 calendar days | occurrence of loss | [PCIC Fisheries Insurance general information, 2024 V4](https://pcic.gov.ph/wp-content/uploads/2024/09/Fisheries-Insurance-2024-V4.pdf) |
| PCIC fisheries Claim for Indemnity | 7 calendar days | occurrence of loss | same brochure |
| PCIC rice Claim for Indemnity | 20 calendar days, and before the scheduled harvest | occurrence of loss | [PCIC Rice Crop Insurance general information, April 2024 V12](https://pcic.gov.ph/wp-content/uploads/2024/09/HO-RICE-2024-APRIL-V12.pdf), "Claim for Indemnity" |
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

**Commodities as data.** The form is defined once in [app/nol_form.py](../app/nol_form.py) as a commodity entry: its
PCIC form fields and numbering, the deadline-table rows it uses, the facts that make an incident eligible, the
evidence it requires, and how each field is filled (`assured` or an `evidence` rule; the office is fixed). The
drafting, deadline and approval code never names a commodity; a test adds a dummy commodity with no code change.
Fisheries and rice are built; corn and high-value crops are later work, one more entry each.

### Insurance claim form for rice farmers (PCIC Claim for Indemnity)

Rice has **no separate Notice of Loss**: an insured rice farmer's first filing after a typhoon or flood is the
**Claim for Indemnity** ([RC-CAS-01, Rev. 2017/FEB](https://pcic.gov.ph/wp-content/uploads/2024/02/RC-CAS-01_Claim-for-Indemnity.pdf)),
due within 20 calendar days of the loss and before the scheduled harvest (PCIC rice brochure, April 2024 V12,
"Claim for Indemnity"; the PCIC Citizen's Charter 2026 lists RC-CAS-01 as the claim document for rice and corn).
Ulat drafts it from the same reports, through the same pipeline and pages as the fisheries notice:

- **Facts.** The agent reads "lubog ang palay" (submerged), "nadapa" or "natumba" (lodged, flattened), "isang
  ektarya" and stage words ("namumulaklak": flowering) into typed facts: `crop`, `crop_area_hectares`,
  `crop_damage_percent`, and two new ones, `crop_stage` and `crop_damage`. "Palaisdaan" is a fishpond, not a crop.
  A report about a fish pen and a rice field is eligible for both claims, each drafted from its own facts.
- **Photos.** The photo judge says whether a rice field is flooded, lodged or not visibly damaged (κ 0.75 to 0.83
  on 19 real photos; see [How well it works](evaluation.md#how-well-it-works)). SAM 3.1 did not outline every rice field, so no
  "water over N % of the field" figure is shown.
- **The form.** All 17 numbered items in RC-CAS-01's order and section numbering (I.1 to I.11, II.1 to II.6) with the
  paper's English and Filipino labels. The agent fills I.4 location (place details and pin), I.5 Palay, II.1 cause
  (for example "Flood (rice submerged)"), II.2 the first report, II.3 the stage, II.4 the damaged area and II.5 the
  share lost, each only as stated in the reports. The farmer's name, address, phone and CIC number are typed for the
  printout only and never stored; Section III, the sketch of the damaged lots with the neighboring farmers' names, is
  written by hand on the printout. Every field has its "?" help with a source.
- **The printout** is RC-CAS-01 word for word and in its layout (Cambria, the bilingual labels, "( )" options, the
  sketch table and the signature block), audited field by field like the fisheries letter.

**Which claims an office drafts** is data: [app/data/pcic_lines.json](../app/data/pcic_lines.json) has one row per
commodity, on or off, each with its source. Fisheries is on for Biñan (the City Agriculturist registers fisherfolk and
fish pens, Citizen's Charter 2026 pp. 203–204). ⚠️ **Rice is off for Biñan**: the same charter (p. 198) lists a seed
and fertilizer subsidy for "Registered Biñan Rice Farmers", but no source found gives a rice area or names a
barangay, so Biñan's rice area is unconfirmed. Rice is built because the product serves any Laguna de Bay LGU:
around the lake, NIA's Sta. Cruz River Irrigation System serves
[1,542 farmers in Sta. Cruz and Pila](https://nia.gov.ph/press_release/automated-feature-sta-cruz-ris-diversion-dam-installed-laguna/).
Turn rice on with one setting, no code change:

```
PCIC_COMMODITIES=fisheries,rice
```

(`Settings.pcic_commodities`; for a deploy target, add it to that target's `env` in `app/deploy_targets.py`). The demo
loader reads the same setting: with rice on, `python -m app.demo load` adds two example rice reports from "Example
Farm", a farm in a Laguna de Bay town, pinned outside Biñan's barangays (the dashboard flags the pin as outside
Biñan, honestly) and never placed in a Biñan barangay. One has an AI-generated photo, stamped as such like every
demo photo.

### Officer dashboard

The officer works in one server-rendered dashboard (FastAPI, Jinja, HTMX; no build step, every page usable with
JavaScript off). It follows Material Design 3 and is built for a phone first: color roles generated from one seed
color in light and dark, self-hosted Roboto and Material Symbols icons, a bottom navigation bar on phones (Home,
Review with a live count badge, Incidents, Drafts and More, which holds Reports, Map, Damaged houses, Help and, in
Technical mode, Accuracy), a navigation rail on
tablets and a drawer on wide screens ([app/nav.py](../app/nav.py) is the one list of destinations), tables that become
cards on a phone, 48 px touch targets and no sideways scrolling at 360 px. Moving between pages swaps only the main
region (htmx boost with View Transitions), so the live connection stays open, the address bar, Back, Forward and
refresh behave like separate pages, focus moves to the page heading, the scroll position comes back on Back and a
thin bar shows a slow load; officer actions confirm in a snackbar. Pages, styles and scripts are sent gzipped. The
top bar carries the live status and an account menu (signed-in officer, sign out). It updates live: the web app watches the shared SQLite file once per process (`PRAGMA
data_version`, so it sees the bot's writes from its separate process) and tells each open tab over server-sent
events which topics changed (reports, incidents, drafts, model spend), never report text. Each marked section
re-fetches only itself, keeps the scroll position, highlights new rows briefly and never replaces a section the
officer is typing in: it shows "New updates. Refresh section" there instead, and a requested refresh keeps the typed
values. It has two views of the same pages ([app/modes.py](../app/modes.py)), decided on the server for each request
and remembered per browser in a cookie (not personal data): the account menu's **View** switch, or `?mode=technical`
on any link for a demo; `DASHBOARD_MODE` sets the default. **Simple** (the default, for officers) opens on a to-do
list in plain words, most urgent first, each item one card with one button: a Notice of Loss with its filing
countdown ("Make the draft" or "Check and approve"), the quick questions about reports ("Answer"), drafts ready to
approve, barangays with new reports for the RDANA form, and new reports; "All caught up" with the latest incidents
when nothing waits. It names no models, costs, ids, confidence numbers or internal statuses, explains RDANA, DROMIC,
Notice of Loss and incident in place ("What's this?"), labels buttons by what they do ("Approve for filing", "Same
flood"), gives every page a one-line hint and an empty state, shows a dismissible three-step
"How Ulat works" guide once per browser (again from More, then Help) and hides Accuracy, the agent trace, the model
strip and raw tables. **Technical** (for developers, judges and the video) shows everything below. Every officer
action and permission is the same in both views and posts to the same endpoint with the same audit (a test compares
every page's actions); Simple mode only words them differently, asks Review one question at a time and folds the
rarer actions under "More options". Messages set aside as not
reports have their own list with Restore (`/reports/set-aside`); Simple mode shows only a quiet "N messages set
aside" line. In Technical mode the home page `/` is an overview: open incidents, incidents waiting for officer review, reports in the last
24 hours (and held ones), drafts awaiting approval, the next filing deadline with its live countdown and model spend
against the cap, each tile linking to its page and counted with the same helpers as that page
([app/dashboard.py](../app/dashboard.py)), beside the incidents needing attention, the latest reports and a compact map. Under the tiles, "Meta models at work" lists each model's calls in the last 24 hours, the share answered from the cache, the cost (adding up to the spend tile) and the median time of live calls, from one windowed SQL query, and links to `/accuracy`. `/map` (Leaflet, vendored) draws
the barangay boundaries and one marker per active incident (every marker a 48 px touch target; markers closer than
that on screen become one "+N" marker that fans out when tapped) at the mean of its reports' GPS points inside Biñan, or
at its barangay's center when no report has GPS (a dashed, hollow "approximate" marker); the marker's shape, a
number and a legend in words carry the status, not color alone, and its popup links to the incident. A report
pinned outside Biñan appears only when the trust checks flagged it, and the map never pans to it. OpenStreetMap
tiles are the basemap; the boundaries and markers still draw when the tiles cannot load; Leaflet loads only on a
page that shows a map. The top bar
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
- **block or unblock the reporter**, with the same function as `python -m app.trust block`;
- **restore a message screened out as not a report** (`POST /reports/<id>/review/restore`, the same login,
  same-origin check and initials as the other actions): it goes through the normal pipeline again at once when the
  web app runs the bot (otherwise at the bot's next start or with `python -m app.assessment --pending`), and
  screening never drops it again. Screened-out messages show on `/reports` with a "not a report" chip, the
  screening reason and a "Restore as a report" form; a page of their own (`app.screening.not_reports`) is still to
  come.

Every override is an append-only `OfficerAction` row (database triggers refuse updates and deletes) with the officer's
initials (required, at most 40 characters; prefilled from the initials typed at login, and self-declared, since the
officers share one passcode) and an optional note, written in the
same transaction as the change. A separate table rather than new `AgentDecision` kinds, because an officer also
reviews signals and blocks reporters, which have no incident, and every row names a person; the incident page shows
agent and officer entries together, labeled "Agent" and "Officer JDC". Overrides **stick**: a merge or keep-separate
pins the report's grouping (`Report.officer_incident_id`), which re-grouping never changes and never re-judges, and a
set barangay (`Report.officer_barangay`) is never overwritten by re-assessment (the agent's own finding is kept beside
it). The pending bucket, the counts and the incident rollups follow at once, dependent open drafts turn stale through
their evidence fingerprint, and open dashboards refresh over the live stream. Rejected forms (no initials, an
incident outside the allowed set) come back with a 400, the typed values and the error beside the field;
cross-site posts get a 403.

In **Simple mode** (the default) Review is a one-question queue ([app/questions.py](../app/questions.py)): one item at a
time, "1 of 9", Skip and Back, the report as it came in (photo, text or voice transcript, place, "2 h ago", drawn
like the chat message it was) and one question with one or two big buttons. Each open reason on a card becomes one
question, from one table, and each answer is one of the actions above with its audit: a possible duplicate asks "Is
this the same flood as *Malaban · flood · Purok 3* (4 reports)?" with both reports side by side (**Same flood** merges,
**Different** keeps it separate); an unknown place asks "Where is this?" with the likely barangays (the names the
geolocation found too many of, names loosely matched in the words, then where floods are reported now) and
"Other…" for the full list; a trust signal asks "Someone else already sent this same photo, on Oct 30. Is it OK to
use?" (**It's fine** marks it genuine, **Not OK** keeps it marked doubtful on the forms); a fallen tree is asked
**only** when a photo judgment or the report's words suggest one. A report with two open questions asks them one
after the other. The initials are never shown when they are known (typed at sign-in, or asked once on Review and
kept in the signed session), and Technical mode keeps the field on every form. With JavaScript, an answer waits
behind the snackbar's **Undo** for six seconds before it is sent (answering the next question or leaving the page
sends it at once) and the next question is already on screen; a live update never replaces the question being read:
answered ones drop out and new ones join the end with a quiet "2 new" chip. Without JavaScript each answer is a plain
form and Skip and Back are links. Moving a report to any other flood, keeping it on its own, setting the barangay by
hand and blocking the sender sit under the item's "More options". Home's to-do card, the Review badge and the
queue's "1 of n" are one count (`app.questions.plan`), and the incident page links its questions to Review instead
of repeating the forms.
