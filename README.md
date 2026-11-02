# Ulat

> An entry for the **Meta Global AI Developer Hackathon** (Oct 27 – Nov 4, 2026), built for the Philippines with Meta's Muse models.

**Status:** live prototype. Since 2026-10-30 it runs on Modal: the officer dashboard at
<https://ronniecutamora47--ulat.modal.run>, seeded with a synthetic typhoon scenario, and the Telegram bot
[@UlatPH_bot](https://t.me/UlatPH_bot) (see [Demo](#demo)). It is a hackathon prototype, not a service of Biñan City. Typhoon and flood damage reporting for Philippine LGUs, plus PCIC insurance claims: one agent turns photos, Taglish voice notes, and texts from barangays, fisherfolk and rice farmers into an officer-approved RDANA report, a damaged-houses table, a PCIC Notice of Loss for each fish cage or pen loss, and a PCIC Claim for Indemnity for each flooded or flattened rice field. Corn and other crops are later work. Demo area: Biñan City, Laguna.

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

1. Residents send photos, voice notes, texts and location pins; the agent extracts damage facts and maps the report to a barangay. Reporters often send a photo and then type its words: a text within 2 minutes of the same reporter's caption-less photo, with nothing in between, becomes that photo's caption (the bot answers "Idinugtong sa iyong larawan. / Added to your photo."), and the photo is re-assessed and re-grouped with it, like a late location pin. Photos sent as one Telegram album (one `media_group_id`) are one report: one "received" reply, every photo assessed, and a text after a caption-less album captions all of it.
2. Deterministic place/time/GPS filters find candidate incidents. A text-only Meta pair judge decides whether reports describe the same situation; uncertainty stays separate for officer review. Every candidate is judged, not just the first, so a correction ("mali po… dalawang bahay lang, hindi tatlo") lands in the incident that holds the figure it corrects.
3. Officers read incident timelines, depth changes and the stored reasons for grouping, and resolve what the agent could not settle in a review queue: merge, keep separate, set the place, check trust signals, confirm fallen trees, block a reporter (see [Officer review](#officer-review)). Reported totals explicitly warn about double-counting, including corrections.
4. The agent drafts each barangay's RDANA form and the city's damaged-houses table from that evidence; the officer checks every value against its linked reports, then approves or rejects the draft (see [Officer outputs](#officer-outputs)).
5. For each incident of fish cage, fish pen or fishpond damage, the agent drafts the fisherfolk's PCIC Notice of Loss with an evidence annex for the adjuster and counts down its 2-day filing deadline; the officer approves it for filing, and the assured files it (see [Insurance: fisheries Notice of Loss](#insurance-fisheries-notice-of-loss)). For a flooded or flattened rice field it drafts the farmer's PCIC Claim for Indemnity (RC-CAS-01) the same way, with its 20-day clock, where the office turns rice on (see [Insurance: rice Claim for Indemnity](#insurance-rice-claim-for-indemnity)). Corn and other crops are later work.

Every report on `/reports` and on its incident timeline has an expandable **agent trace**: each pipeline step in order (transcribe, read the facts, judge each photo, SAM roofs and floodwater, screen the message, find the barangay, check trust signals, group into an incident) with the Meta model that ran it, its latency, whether it came from the cache and its estimated cost, next to the one-line result it produced ("roof damage: partial", "2 roofs found", "barangay Malaban (photo GPS)", "different from incident #2, 96% → kept apart, in incident #3"). Deterministic steps are labeled "Rule, no model", officer overrides follow as "Officer" steps, and the totals give the report's model time and cost. The trace shows only derived results the dashboard already shows, never prompts, replies or the officer's notes. An RDANA draft page lists the model calls that wrote its summary. Reports assessed before 2026-10-30 say "trace not recorded". How often each model is right is on the dashboard's `/accuracy` page (see [How well it works](#how-well-it-works)).

## Meta models used

| Model | What it does in this project | Status |
|---|---|---|
| Muse Voice Transcribe (`muse-voice-transcribe-1.0`) | Turns Telegram voice notes (Tagalog, English, Taglish) into text, biased with `keywords` toward Biñan's 24 barangay names and then 471 OpenStreetMap street, subdivision and landmark names, up to the API's 500 | In use |
| Muse Spark 1.3 (`muse-spark-1.3`) | The agent LLM (default): judges each photo as structured JSON (roof damage, floodwater, debris, fallen tree, tarpaulin, house count) and extracts report facts (flood depth, houses, evacuees, crops, needs); default incident pair judge; writes the RDANA summary description (1.4), checked against the evidence | In use |
| Muse Spark Open (`meta-models/muse-spark-open`, Baseten) | The same agent steps and incident pair judgments on open weights, photos included: set `AGENT_LLM=muse-spark-open` | In use |
| Muse Spark Open (`accounts/fireworks/models/muse-spark-open`, Fireworks) | Agent or independent pair judge via `muse-spark-open-fireworks` | Implemented; mocked HTTP verified, no live call in this slice |
| Muse Glimmer (`accounts/fireworks/models/muse-glimmer-30b`, Fireworks) | Pair-judge comparison via `muse-glimmer` | Implemented; serverless availability ⚠️ contradictory and unverified; explicit availability errors, no fallback |
| Muse Image (`muse-image-1.0`) | Synthetic, clearly labeled demo photos for the typhoon scenario and the 40-photo synthetic eval set (no real homes or people); every image is stamped "AI-generated (Muse Image) · synthetic demo" | In use (demo and eval data) |
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

**Not a report? A kind nudge, not a report.** Weekend tests of the live bot showed "test", "hahaha", keyboard mash,
emoji and stickers becoming reports on the dashboard. Every message is now screened before it can become one
([app/screening.py](app/screening.py)), precision first, because dropping one real "baha dito, tulong" is worse than
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
screen --target staging` (snapshot first, `--dry-run` to preview; see [deploy/README.md](deploy/README.md)).

## How well it works

Measured on labeled sets with real (cached) model calls, not mocks. Every number below is generated from [data/eval/results.json](data/eval/results.json), which also stores each run's dataset provenance, model id and backend, date, spend and code commit; a test keeps this table identical to the file. Re-run with the commands under "Getting started". The officer dashboard renders the same file at `/accuracy` ("How accurate is the agent?", under More in the navigation and linked from the overview): one table per task and data set with Spark 1.3 and Spark Open side by side, n, data provenance, run date, code commit and spend, weak scores marked, and a plain-language caption on every score saying what it means and what it does not (SAM's outlines are evidence, not the loss figure; fallen trees need confirmation); a missing or broken file shows an empty state. The Voice Transcribe numbers are in the file, the table below and the page's voice chart, with WER, place names and false insertions per keyword setting.

<!-- eval-results:start (generated by: uv run python -m app.eval_results) -->

**Photo judge** (the agent LLM judges each photo; SAM 3.1 finds roofs and floodwater):

| Model | Dataset | n | Roof damage κ | Floodwater F1 | Debris F1 | Tarpaulin F1 | Fallen tree F1 | SAM roof F1 | SAM floodwater F1 | Date |
|---|---|---|---|---|---|---|---|---|---|---|
| muse-spark-1.3 | Real typhoon photos (real) | 25 | 0.60 | 0.92 | 0.89 | 0.86 | 0.57 | 1.00 | 1.00 | 2026-11-02 |
| muse-spark-open | Real typhoon photos (real) | 25 | 0.50 | 1.00 | 0.92 | 1.00 | 0.40 | 1.00 | 1.00 | 2026-11-02 |
| muse-spark-1.3 | Synthetic Biñan-style photos (synthetic) | 40 | 0.88 | 0.82 | 0.79 | 0.91 | 1.00 | 0.96 | 0.44 | 2026-11-02 |
| muse-spark-open | Synthetic Biñan-style photos (synthetic) | 40 | 0.75 | 0.95 | 0.81 | 0.91 | 0.89 | 0.96 | 0.44 | 2026-11-02 |

**Rice-field judge** (the same photo judge says whether a rice field is flooded, lodged or not visibly damaged; SAM 3.1 is asked for "rice field" and "floodwater" to see whether it can measure the share of a field under water):

| Model | Dataset | n | Field condition κ | Same as label | Flooded or lodged: precision / recall | SAM field outlined | SAM water share within 20 points | Date |
|---|---|---|---|---|---|---|---|---|
| muse-spark-1.3 | Real rice-field photos (real) | 19 | 0.75 | 16 of 19 | 1.00 / 0.80 | "rice field" 12 of 19; "rice plants" 8 of 19 | "rice field" 9 of 12; "rice plants" 6 of 8 | 2026-11-02 |
| muse-spark-open | Real rice-field photos (real) | 19 | 0.83 | 17 of 19 | 1.00 / 0.80 | "rice field" 12 of 19; "rice plants" 8 of 19 | "rice field" 9 of 12; "rice plants" 6 of 8 | 2026-11-02 |

**Spark and SAM 3.1 check each other** on every photo. When Spark sees floodwater or a partly damaged roof that SAM cannot outline, the report gets a trust signal and Review asks the officer; the other disagreements are only measured (each cell: times it fired, then whose answer matched the hand label). muse-spark-1.3: they disagreed on 21 of 65 photos, and Review asked about 1; muse-spark-open: they disagreed on 23 of 65 photos, and Review asked about 0.

| Model | Dataset | n | Disagreed | Asked the officer | Floodwater: Spark sees it, SAM outlines none (asks) | Floodwater: SAM outlines it, Spark sees none (measured only) | Partly damaged roof: SAM outlines no roof (asks) | Severe roof damage: SAM outlines no roof (measured only) | Date |
|---|---|---|---|---|---|---|---|---|---|
| muse-spark-1.3 | Real typhoon photos (real) | 25 | 2 | 1 of 25 | 1 (Spark 0, SAM 1) | 0 (Spark 0, SAM 0) | 0 (Spark 0, SAM 0) | 1 (Spark 1, SAM 0) | 2026-11-02 |
| muse-spark-open | Real typhoon photos (real) | 25 | 1 | 0 of 25 | 0 (Spark 0, SAM 0) | 0 (Spark 0, SAM 0) | 0 (Spark 0, SAM 0) | 1 (Spark 1, SAM 0) | 2026-11-02 |
| muse-spark-1.3 | Synthetic Biñan-style photos (synthetic) | 40 | 19 | 0 of 40 | 0 (Spark 0, SAM 0) | 19 (Spark 19, SAM 0) | 0 (Spark 0, SAM 0) | 1 (Spark 1, SAM 0) | 2026-11-02 |
| muse-spark-open | Synthetic Biñan-style photos (synthetic) | 40 | 22 | 0 of 40 | 0 (Spark 0, SAM 0) | 22 (Spark 22, SAM 0) | 0 (Spark 0, SAM 0) | 2 (Spark 1, SAM 1) | 2026-11-02 |

**Incident pair judge** ("is this new report the same incident?", merge threshold 0.85; below it, or unsure, goes to the officer):

| Model | Dataset | n | Precision | Recall | F1 | Officer review rate | Date |
|---|---|---|---|---|---|---|---|
| muse-spark-1.3 | Synthetic report pairs (synthetic) | 64 | 1.00 | 1.00 | 1.00 | 0% | 2026-10-30 |
| muse-spark-open | Synthetic report pairs (synthetic) | 64 | 1.00 | 1.00 | 1.00 | 0% | 2026-10-30 |

**Not-a-report screen** (keeps real reports, screens out "test", "hahaha", mash and spam; a kept real report is a positive):

| Model | Dataset | n | Precision | Recall | Real reports wrongly filtered | Non-reports filtered | By the free rule | Screening calls | Date |
|---|---|---|---|---|---|---|---|---|---|
| muse-spark-1.3 | Synthetic bot messages (synthetic) | 60 | 0.94 | 1.00 | 0 | 28 of 30 | 20 | 8 | 2026-11-02 |

**Voice Transcribe** (Taglish voice notes through the bot's own audio path; WER counts every word that differs, so "seventy" for "70" is an error):

| Model | Dataset | n | Setting | Keywords | WER | WER after cleanup | Place names heard | False insertions | Date |
|---|---|---|---|---|---|---|---|---|---|
| muse-voice-transcribe-1.0 | Taglish voice notes (real) | 8 | No language hint | 0 | 19.3% | 16.5% | 3 of 10 | 0 | 2026-11-02 |
| muse-voice-transcribe-1.0 | Taglish voice notes (real) | 8 | Language hint (Tagalog, English) | 0 | 19.8% | 16.5% | 3 of 10 | 0 | 2026-11-02 |
| muse-voice-transcribe-1.0 | Taglish voice notes (real) | 8 | Hint + Biñan's barangay names | 29 | 20.3% | 18.1% | 3 of 10 | 0 | 2026-11-02 |
| muse-voice-transcribe-1.0 | Taglish voice notes (real) | 8 | Hint + barangay names + the notes' own place names | 39 | 18.8% | 14.0% | 10 of 10 | 1 | 2026-11-02 |
| muse-voice-transcribe-1.0 | Taglish voice notes (real) | 8 | Hint + barangay names + 100 Biñan street and place names | 129 | 20.5% | 16.8% | 3 of 10 | 0 | 2026-11-02 |
| muse-voice-transcribe-1.0 | Taglish voice notes (real) | 8 | Hint + barangay names + Biñan street and place names, up to the 500-keyword limit **(the bot)** | 500 | 19.1% | 16.3% | 4 of 10 | 0 | 2026-11-02 |

<!-- eval-results:end -->

Read these numbers with their caveats:

- **The real set is not Biñan.** It is 25 Wikimedia Commons photos of typhoons Haiyan (2013), Ketsana/Ondoy (2009) and Rai/Odette (2021), mostly severe damage, hand-labeled by one person for the 2026-10-27 kill test (credits under "Third-party code and assets"). The 2026-11-02 rows are a re-run after the judge prompt gained its rice-field question (SAM replies cached): roof-damage κ moved from 0.66 to 0.60 (Spark 1.3) and from 0.56 to 0.50 (Spark Open), and Spark 1.3 called floodwater on one dry photo. The same house prompt scored κ 0.78 on 2026-10-27 and 0.66 on 2026-10-28: with 25 photos, one changed answer moves κ by about 0.05, so these shifts are within run-to-run noise.
- **The synthetic set is synthetic.** Its 40 photos are Muse Image renders of Biñan-style scenes from [a scene table](data/eval/vision_scenes.json), stamped as AI-generated. Their labels were designed per scene and then checked against every image: 3 labels changed (a roof is visible on the far shore of three fish-pen photos) and 4 borderline ones were kept and noted. Accuracy on renders is not accuracy on residents' phone photos.
- **The pair set is synthetic and too easy to separate the models.** Both score 1.00 on 64 hand-written pairs. Real reports will be messier; the officer-review path for unsure and low-confidence pairs stays.
- **Fallen trees are the weak spot on real photos** (F1 0.57 on Spark 1.3, 0.40 on Spark Open), as the 2026-10-27 kill test found. The dashboard therefore asks for confirmation of fallen trees.
- **SAM calls the lake "floodwater".** On the synthetic lakeshore scenes, SAM's floodwater precision is 0.28: Laguna de Bay itself, fish-pen water and an irrigated paddy are masked as floodwater. For a lakeshore city, the SAM floodwater mask is evidence for the officer; the LLM's judgment (floodwater F1 0.82 / 0.95 there) decides.
- **When Spark and SAM disagree, the hand label mostly sided with Spark.** They disagreed on 21 of the 65
  photos (Spark 1.3, re-scored on 2026-11-02 from the photo re-run above): on 19 synthetic lakeshore photos SAM
  outlined the lake, a canal or a wet road as floodwater where Spark saw none, and on 1 severely damaged house
  SAM outlined no roof; the hand label sided with Spark in those 20. So the check asks the officer only the
  other way round, when Spark sees floodwater or a partly damaged roof that SAM cannot outline. That happened on
  1 of 65 photos: Spark 1.3 called floodwater on a dry real photo, SAM outlined none, and the hand label sided
  with SAM, which is the catch the question exists for (n = 1; the run before the re-run had none). The
  officer's answer never changes the counts (they keep Spark's judgment); it clears the flag or keeps the
  report marked doubtful, like every trust signal.
- **Rice fields: the judge, not SAM.** On 19 real rice-field photos from Wikimedia Commons (IRRI and others, credits under "Third-party code and assets"; labeled by one person, n is small), both models say whether a field is flooded, lodged or not visibly damaged with κ 0.75 (Spark 1.3) and 0.83 (Spark Open), and never called a normally irrigated paddy damaged. SAM 3.1 outlined a "rice field" in only 12 of 19 photos, and in only 2 of the 7 flooded ones, where the water hides the field; so Ulat does not report "water over N % of the field". The field condition is evidence for the PCIC adjuster on the rice claim; the loss figure is the adjusters'.
- **Spark Open is close to Spark 1.3** on photos and equal on pairs, which is what the open-weight mode needs.
- **The screen kept every real report** on 60 synthetic Telegram-style messages (30 reports, many very short or
  typo-heavy, and 30 non-reports): 0 real reports wrongly filtered, recall 1.00, precision 0.94. The free rule
  dropped 20 non-reports with no model call; 8 more were screened out by Muse Spark after the facts call found
  nothing. Two were kept because the facts call read them as requests for help ("needs: other"): "Sino may charger
  ng Samsung dyan?" and "pa-load naman po 50". The precision-first policy keeps such messages on purpose. The
  messages are synthetic; residents' own will be messier.
- **Voice Transcribe was measured on one speaker reading scripts.** The 8 Taglish voice notes (399 words) were
  recorded by the builder on a phone, in the roles of barangay officials, farmers and fisherfolk, with invented
  names and places. They are read speech, not field recordings: no wind, rain, crowd noise or second speaker, so
  residents' reports will score worse. The audio and the reference texts stay outside the repo; only these metrics
  are committed. Each setting is one run per note, and identical requests have returned different words, so a gap
  of 2–3 WER points between rows is within noise.
- **WER is not the share of wrong facts.** It counts every word that differs from the script after lowercasing
  and dropping punctuation, so "seventy" for "70" or "dalawa't kalahati" for "2.5" is an error even when the fact
  is right. The repeated-word cleanup the bot runs before reading facts takes off 1.3–3.3 points. Whether the facts
  survive is a separate measure (`scripts.eval_facts`: transcript, then Spark, then fields), not in this table;
  the 2026-10-27 test's hand count of 31 of 37 key facts (84%) cannot be reproduced from plain-text references, so
  it is not recorded.
- **Keywords fix place names only when the place is on the list.** The notes name invented barangays and sitios
  that stand in for real ones. With those names on the keyword list, all 10 were spelled right (WER 18.8%, 14.0%
  after cleanup). With only Biñan's 29 barangay names and aliases it heard the same 3 of 10 as with no list. So
  the list should help with real names (⚠️ not measured: no note names a real barangay or street).
- **The long street list costs nothing measurable, so the bot sends it.** Voice Transcribe accepts at most 500
  keywords of up to 30 characters each (the API answers HTTP 400 past either; the docs give no limit, tested
  2026-11-02). The bot sends the barangays first, then the OpenStreetMap names (areas, landmarks, then streets
  by length) up to 500. With 100 or 471 OSM names, WER (20.5% and 19.1%) stayed within noise of the barangays
  alone (20.3%), and no list put a name in a transcript that the speaker did not say (0 false insertions, counted
  over all 510 keywords any row sends; the notes' own place names inserted 1). The 4 of 10 place names with the
  long list is noise too: none of the notes' places is on the OSM list. Because no note names a Biñan street,
  this measures the cost of the long list, not its benefit (⚠️ follow-up: voice notes that name real streets,
  read by a synthetic voice; Meta offers no text-to-speech).
- **Rows move between runs.** The barangay rows were re-run on 2026-11-02 (the request changed) and came out 1.3
  and 2.5 points away from the first run (21.6% and 16.3%); the hint rows are the 2026-10-27 test's cached replies
  and reproduce its 19.8%. That test counted place names 4 of 9 over notes 1–10; this one scores the 10 labeled
  barangay and sitio names in notes 3–10.
- Spend for the photo and pair runs, including the 40 generated images: $1.93 at list prices, from the eval call
  log; the screen runs added $0.35 and the Voice Transcribe runs $0.08 ($0.02 for the first, $0.06 for the
  street-name rows). The rice-field kill test cost $0.41 (both models) and the 2026-11-02 photo re-run $1.17
  (both models; SAM cached).

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
"sic"), printable on A4 in the paper form's own layout. The form is defined once as data in [app/rdana_form.py](app/rdana_form.py); each field is
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
or "0". The evidence for each filled value links to its incidents and reports, with the reporter count,
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
officer login (one shared passcode; see [Deploy](#deploy)); only a localhost dev server without `OFFICER_PASSCODE`
runs without it. Every state-changing request (generate, approve, reject) must also be same-origin: when the
browser sends an `Origin` or `Referer` header it must name this host, or the app answers 403, so another site open
in the officer's browser cannot post to it. The app answers only to the host names in `ALLOWED_HOSTS` (default
`localhost,127.0.0.1`; other names get 400), so a DNS rebinding site cannot pose as this host.

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

**Deadlines.** Every deadline comes from one table, [app/data/deadlines.json](app/data/deadlines.json), each row with
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

**Commodities as data.** The form is defined once in [app/nol_form.py](app/nol_form.py) as a commodity entry: its
PCIC form fields and numbering, the deadline-table rows it uses, the facts that make an incident eligible, the
evidence it requires, and how each field is filled (`assured` or an `evidence` rule; the office is fixed). The
drafting, deadline and approval code never names a commodity; a test adds a dummy commodity with no code change.
Fisheries and rice are built; corn and high-value crops are later work, one more entry each.

### Insurance: rice Claim for Indemnity

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
  on 19 real photos; see [How well it works](#how-well-it-works)). SAM 3.1 could not outline flooded fields, so no
  "water over N % of the field" figure is shown.
- **The form.** All 17 numbered items in RC-CAS-01's order and section numbering (I.1 to I.11, II.1 to II.6) with the
  paper's English and Filipino labels. The agent fills I.4 location (place details and pin), I.5 Palay, II.1 cause
  (for example "Flood (rice submerged)"), II.2 the first report, II.3 the stage, II.4 the damaged area and II.5 the
  share lost, each only as stated in the reports. The farmer's name, address, phone and CIC number are typed for the
  printout only and never stored; Section III, the sketch of the damaged lots with the neighboring farmers' names, is
  written by hand on the printout. Every field has its "?" help with a source.
- **The printout** is RC-CAS-01 word for word and in its layout (Cambria, the bilingual labels, "( )" options, the
  sketch table and the signature block), audited field by field like the fisheries letter.

**Which claims an office drafts** is data: [app/data/pcic_lines.json](app/data/pcic_lines.json) has one row per
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
tablets and a drawer on wide screens ([app/nav.py](app/nav.py) is the one list of destinations), tables that become
cards on a phone, 48 px touch targets and no sideways scrolling at 360 px. Moving between pages swaps only the main
region (htmx boost with View Transitions), so the live connection stays open, the address bar, Back, Forward and
refresh behave like separate pages, focus moves to the page heading, the scroll position comes back on Back and a
thin bar shows a slow load; officer actions confirm in a snackbar. Pages, styles and scripts are sent gzipped. The
top bar carries the live status and an account menu (signed-in officer, sign out). It updates live: the web app watches the shared SQLite file once per process (`PRAGMA
data_version`, so it sees the bot's writes from its separate process) and tells each open tab over server-sent
events which topics changed (reports, incidents, drafts, model spend), never report text. Each marked section
re-fetches only itself, keeps the scroll position, highlights new rows briefly and never replaces a section the
officer is typing in: it shows "New updates. Refresh section" there instead, and a requested refresh keeps the typed
values. It has two views of the same pages ([app/modes.py](app/modes.py)), decided on the server for each request
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
([app/dashboard.py](app/dashboard.py)), beside the incidents needing attention, the latest reports and a compact map. Under the tiles, "Meta models at work" lists each model's calls in the last 24 hours, the share answered from the cache, the cost (adding up to the spend tile) and the median time of live calls, from one windowed SQL query, and links to `/accuracy`. `/map` (Leaflet, vendored) draws
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

In **Simple mode** (the default) Review is a one-question queue ([app/questions.py](app/questions.py)): one item at a
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

## Demo

- **Live officer dashboard:** <https://ronniecutamora47--ulat.modal.run> (production, on Modal since 2026-10-30).
  It needs the officer passcode: judges get it in the submission's private judging notes. It starts with the
  synthetic typhoon scenario below (no real people), plus whatever has been sent to the bot since.
- **Telegram bot:** [@UlatPH_bot](https://t.me/UlatPH_bot). A prototype, not run or monitored by Biñan City: don't
  use it for emergencies, **call 911**. Real messages are processed by Meta's models; raw photos and voice notes are
  deleted after 24 hours (at most 72 while processing is paused by the spend cap).
- Video (3 min): _TBD_
- X post: _TBD_

### Try it in 2 minutes

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
   photo, voice note or location pin. It answers "received", then a summary of what the agent understood; the report
   appears on the open dashboard live, mapped to its barangay. Reply to the summary to correct it.
5. **Review** lists what the agent could not settle (an ambiguous place, a reused photo, a fallen tree to confirm);
   **Accuracy** (Technical view) charts how often each model is right on the labeled sets: the roof-damage confusion
   matrix against hand labels, F1 per flag, the duplicate check, the not-a-report screen and Voice Transcribe.

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
use it with `--reset`. On a deploy, `uv run python -m app.deploy reseed --target staging` replaces the demo with a
fresh anchored load and keeps every real report ([deploy/README.md](deploy/README.md), "Before a demo"). A reset deletes the demo databases, uploads and manifest but keeps the generated images and the
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
uv run pytest                             # tests (all HTTP is mocked, no API calls)
uv run ruff check . && uv run ruff format --check .
```

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
# Record a full run in data/eval/results.json (README tables: uv run python -m app.eval_results)
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
- **Durability and loss window:** a stop, redeploy or preemption takes a final snapshot first, so nothing the bot
  acknowledged is lost (measured on staging on 2026-10-30, see [deploy/README.md](deploy/README.md#durability)). A
  crash that skips shutdown loses at most about 5 seconds of acknowledged reports, since the bot asks for a snapshot
  after each update it handles, and at most the last 2 minutes of other database writes. Snapshots: the newest 15
  plus one a day for a week.
- **Judges:** see [Demo](#demo) for the URL, the bot and what to try; the passcode is in the submission's private
  judging notes. Staging (`ulat-staging`, the dev bot) is for testing only and is not a public entry point.

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
| 2026-10-28 | Cached pair-judge eval CLI with cost confirmation, precision/recall/F1 and review-rate reporting, plus 64 synthetic labeled pairs (paraphrased places and near-identical hard negatives; exact string matching scores 62.5%) and fake-judge verification; source lint/format excludes the read-only provider skill references; no paid eval run |
| 2026-10-28 | Fixes from a review of the incident work: a grouping error no longer marks the assessment failed; a "different" verdict moves on to older incidents; names after ni/nina/kay/kina and titles are redacted from places; the client refactor keeps the old cache keys and accepts complete answers that end at the token limit |
| 2026-10-28 | Synthetic demo scenario: a Muse Image client, 17 Taglish reports over two weeks after landfall (a street flooding and receding, a correction, an ambiguous and a placeless report, a fisherfolk's broken fish cages), labeled synthetic photos, and a loader into a separate demo database through the real intake and assessment pipeline |
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
| 2026-10-29 | Review fix (round 2): every number in the 1.4 summary needs a label naming exactly one computed count; unlabeled numbers, people with no qualifier and "one of" followed by a count word are rejected |
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
| 2026-10-29 | Officer dashboard design system: one stylesheet with color, spacing and type tokens (16 px body text, AA contrast, visible focus rings, 40 px controls), an "Ulat" header bar with the active page marked (`aria-current`), status badges that always carry text, sticky table headers, tables that scroll inside a wrapper on narrow screens and become stacked cards under 640 px, and A4 print rules that drop the dashboard chrome |
| 2026-10-29 | Correction routing (fix from the live bot test): the pair judge now compares a report with every candidate incident up to `MAX_JUDGED_CANDIDATES` instead of stopping at the first match, and the best accepted match wins: an incident not waiting for review, then for a correction the incident stating the corrected figure, then confidence. A Casile "2 houses, not 3" correction had merged into an unconfirmed old-photo incident; re-grouping moves such a report, and the stored reason names why the incident won |
| 2026-10-29 | Late captions (fix from the live bot test): a text sent within 2 minutes of the same reporter's caption-less photo or image file, with no other report in between, becomes the photo's caption instead of a caption-only report; the bot confirms it, the photo is re-assessed (cached photo calls, new facts and barangay from the caption) and re-grouped, and the joined text is exempt from the rate limit. An assessment whose text or pin changed while it ran no longer ends "assessed" with stale facts: the report stays new for the queued re-run, also across a spend-cap hold |
| 2026-10-29 | Bot profile as data (`app/bot_profile.py`): the name, short description, description and command list for @UlatPH_bot in Taglish and English (what to send, Biñan typhoon and flood damage, reports go to the City DRRM Office, not an emergency line: call 911, the same privacy line as `/start`), with Telegram's length limits enforced by a test; an idempotent CLI compares the live values and sets only what differs (`--dry-run` prints the changes), and `--photo` uploads the avatar as a square JPEG; `/help` repeats `/start` |
| 2026-10-29 | Live dashboard: one database watcher per web process (`PRAGMA data_version` on a dedicated read-only connection, then per-topic tokens in one read transaction) fanned out over server-sent events (`/events`, typed topics, heartbeat, retry, no report text) with a polling fallback (`/events/token`); marked page sections refresh in place without wiping typed input, keep scroll position and highlight new rows; Live / Reconnecting / Offline indicator; streams close on Ctrl+C or reload so uvicorn never hangs |
| 2026-10-29 | Overview home page (`/`): key-figure tiles (open incidents, officer review, reports in 24 h and held, drafts awaiting approval, next filing deadline with a live countdown, model spend against the cap) that link to their pages and reuse those pages' queries and services (`app/dashboard.py`), the incidents needing attention and the latest reports; it updates live; a test checks every figure against the linked page |
| 2026-10-29 | Officer map (`/map`, Leaflet 1.9.4 vendored): barangay boundaries served from the COD-AB file (`/map/barangays.geojson`, cached with an ETag) and shaded where incidents are active; one marker per active incident at its reports' mean GPS point inside Biñan, else an "approximate" marker at the barangay center (`app/geo.py` `centroid`); status by shape, number and a text legend; flagged pins outside Biñan only, never auto-panned to; a list of every position for keyboard, screen-reader and no-JavaScript use; live updates; a compact map on the overview; offline-safe when OSM tiles fail |
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
| 2026-10-30 | Agent trace view: an expandable trace on every report row and incident timeline entry (`app/agent_trace.py`, `_trace.html`) with each step's Meta model family and id, host, latency, cache hit and cost beside its one-line result, rule steps and officer decisions labeled as such, repeats from a re-assessment counted, calls with no saved output still listed, per-report totals, a "not recorded" note for older reports, and the summary's model calls on RDANA draft pages; an open trace stays open through live refreshes. Built from batched queries (five per page, not per row) |
| 2026-10-30 | Accuracy page (`/accuracy`, `app/accuracy.py`): "How accurate is the agent?" rendered from `data/eval/results.json`, one table per task and data set with Spark 1.3 beside Spark Open, every entry's n (and failed items), provenance (real Wikimedia typhoon photos, synthetic Muse Image photos, synthetic pairs), date, commit and spend, scores below 0.6 marked weak, plain-language captions for a city officer on every score, and an empty state for a missing, empty or invalid file; in the header navigation, linked from the overview, behind the officer login |
| 2026-10-30 | "Meta models at work" on the overview: calls, cache-hit share, cost and median live-call latency per model over the spend cap's 24-hour window, in one query (window functions over the `at` index), Meta model family and host from the same table as the trace, live through the spend topic; a test checks every number against a direct SQL aggregate |
| 2026-10-30 | Retention fix: the synthetic demo's media (channel `demo`, generated photos, invented reporters, no personal data) is no longer purged after the TTL, so a deployed demo keeps its photo evidence the day after seeding; every real channel is still purged, tested on both sides |
| 2026-10-30 | Review fix: a report the bot acknowledged survives a restart or crash: the bot asks for a database snapshot after every update it handles (at once, at most one per 5 s, with the history thinned to keep its 1-minute spacing), and shutdown is bounded to fit Modal's 30 s: refuse new webhook calls with 503, handle the queued updates, snapshot, give in-flight assessments 8 s with the summary's quiet period skipped, then stop the bot |
| 2026-10-30 | Review fix (RA 10173): the retention purge no longer depends on the bot starting: in webhook mode the web app starts exactly one purge loop from its lifespan (a revoked token or unreachable Telegram no longer stops it, and start retries no longer pile up loops); the polling bot starts it with itself |
| 2026-10-30 | Review fix: reports a restart interrupted (stored but never assessed, or cut off while assessing) resume once at startup in both bot modes, without Telegram: the newest 50, oldest first, one at a time 2 s apart under the spend cap, with the status re-checked under the report's lock so a report a live message assessed meanwhile is not assessed twice; held and failed reports still wait for the officer or `--pending` |
| 2026-10-30 | Review fix: "Keep separate" now undoes a wrong merge: when the report's incident has other reports it is split into a new incident in the same barangay, pinned there, both incidents' rollups are recomputed (drafts go stale), and the audit records an `officer_split` from the old incident; the incident page labels the action "Split into its own incident". The accuracy page's merge note now says figures in one incident are counted once |
| 2026-10-30 | Review fix: the database lease is enforced, not advisory: a container whose stale lease another one took over writes no more snapshots and never overwrites or releases the new holder's lease (the final snapshot reloads the volume first to see a takeover) |
| 2026-10-30 | Review fix: incident titles use the facts as the officer adjusted them (a ruled-out fallen tree leaves the title, a confirmed one joins the hazards), and a fallen-tree verdict refreshes its incident |
| 2026-10-30 | Fix found in the staging restart check: a stored report is queued for assessment before the bot replies "received", so a reporter who blocked the bot (or a failed reply) no longer leaves it unassessed until the next restart |
| 2026-10-30 | Fix found in the staging restart check: a restart that cuts off grouping after the facts were saved left the report assessed but never grouped; startup now also groups such reports (no new facts call) |
| 2026-10-30 | Production deploy and license: Ulat went live on Modal at 13:30 Manila (app `ulat`, commit `8510536`, seeded with the synthetic scenario through the real pipeline), with @UlatPH_bot moved from local polling to the webhook and checked with a phone message; the project is released under the MIT License (`LICENSE`, `pyproject.toml` metadata) with third-party code and data keeping their own terms; the README gains the live URL, a 2-minute "try it" path for judges and corrections of lines the deploy made stale (login, local bot, retention, loss window) |
| 2026-10-30 | README "The problem" section: the national, Laguna de Bay and Biñan picture and the 72-hour RDANA and 2-day PCIC deadlines, each figure linked to the primary source it was checked against (WorldRiskReport 2026, PAGASA, UN OCHA, NIED report No. 45, DSWD DROMIC reports #20, #40 and #68 on Paeng, NDRRMC Memo 107 s. 2021, the PCIC fisheries brochure) |
| 2026-11-02 | Not-a-report screen (`app/screening.py`): a free rule stores greetings, tests, laughter, emoji and keyboard mash as `not_a_report` before any model call (never a message with a photo, voice note, pin, digit or distress emoji); a Muse Spark yes / no / unsure verdict only for messages whose facts state nothing, where only "no" screens out and "unsure" is flagged for the officer; screened-out messages are kept but out of incidents, counts, drafts, the map and the overview (one shared predicate), with a "Screen the message" trace step, an audited officer restore (`officer_restored`) that re-runs the pipeline, a kind bilingual nudge to the reporter (one per 15 min, also for stickers and GIFs) and `python -m app.assessment --screen` for messages sent before it |
| 2026-11-02 | Voice Transcribe accuracy in the results file: `scripts.eval_transcribe` runs each voice note through the bot's own audio path under four settings (no hint, language hint, the bot's barangay keywords, and those plus the notes' own place names), scores corpus WER raw and after cleanup and the labeled place names heard, dry-runs the cache and prices uncached audio against the spend cap, and records one `transcribe` entry (metrics only, never audio or text) in `data/eval/results.json`; it reproduced the 2026-10-27 test (WER 19.8% with the hint, 16.3% and 10 of 10 place names with them as keywords) and showed the bot's list helps only for places on it. The accuracy page now leaves out a task it has no table for instead of failing |
| 2026-11-02 | Material Design 3, mobile-first dashboard: color roles generated from one seed color (Material Color Utilities, TonalSpot) in light and dark, the M3 type scale, shapes and state layers in one stylesheet, self-hosted Roboto and a 31-icon Material Symbols sprite (`scripts/build_icons.py`); a bottom navigation bar under 600 px with More as a bottom sheet, a rail up to 1240 px and a drawer above, from one list (`app/nav.py`); an app bar with the live status and an account menu that has room for a mode switch; tables as cards and form tables stacked on phones, 48 px targets, no sideways scrolling at 360 px; in-app navigation (htmx boost into the page region with View Transitions, real URLs, Back / Forward with scroll restored, focus on the heading, a progress bar, the live stream kept open, no report data in session storage, full page loads without JavaScript); snackbars for officer actions; Leaflet loaded only where a map is shown; gzip for pages, styles and scripts; screened-out messages as a chip on `/reports` with a restore form. Lighthouse mobile: performance 99, accessibility 96 (overview) and 100 (draft) |
| 2026-11-02 | Simple and Technical views (`app/modes.py`): decided on the server per request, remembered per browser in a cookie, switched from the account menu or with `?mode=technical`, default from `DASHBOARD_MODE`. Simple (the default) opens on a to-do list in plain words, most urgent first, one card and one button each (Notices of Loss by filing deadline, reports to check, drafts to approve, barangays with new reports for the RDANA form, new reports; "All caught up" otherwise), names no models, costs, ids, confidence or internal statuses, explains RDANA, DROMIC, Notice of Loss and incident in place, adds a one-line hint and empty state per page, a dismissible "How Ulat works" guide and a Help page, a count badge on Review, and hides Accuracy and the agent internals; Technical shows everything. Same officer actions in both (tested form by form). Screened-out messages get their own list with Restore. Accuracy charts drawn on the server as inline SVG from `data/eval/results.json` (`app/charts.py`): roof-damage confusion matrices, flag F1, the duplicate check, the screen and Voice Transcribe, with number tables. Lighthouse follow-ups: map markers are 48 px targets and close ones fan out, RDANA tables use real captions, and the font and map no longer shift the page (CLS 0.054 to 0.005 on a draft, 0.471 to 0.02 on the map); Lighthouse mobile on Home: 100 / 100 / 100 |
| 2026-11-02 | Live updates that keep every word true: Home's heading and hint ("What needs you" or "All caught up") are redrawn in the same live region as the to-do list, from one template, so they never contradict the cards below them; the Technical overview keeps an empty to-do region on the page, so to-dos that arrive later appear without a reload; focus on a page heading survives a live redraw; and several sections refreshing on one change each get their update (htmx had dropped all but the first and last when they shared one request source). Checked in a real browser under Xvfb: an empty Home turns into "What needs you" when a report arrives and back when it is settled, and a section with focus inside waits behind its "New updates" notice. The count on Review is its own quiet live region in the navigation (`NavItem.badge_live` names its topics), so it follows the review queue on every page, Help included |
| 2026-11-02 | Safe admin commands for a deployed app (`app/admin.py`, `app/reseed.py`): `python -m app.deploy reseed` swaps the aged synthetic demo for a fresh `--anchor-now` load so the filing countdowns are live again, and `screen` sets aside messages stored before the not-a-report screen existed. Both run inside the target's one live container (a method of the Modal class, so never a second SQLite writer), pin a snapshot first that is never pruned and print its name for `restore`, take `--dry-run` (no change, no `--yes`), need `--yes` on production and print counts and ids only. The reseed loads the new demo in a scratch database whose ids count on above every live id, then swaps it in one transaction that refuses (listing ids) an incident or reporter that mixes demo and real reports, keeps incidents and drafts that real rows point at, and rolls back unless every real report and its rows are unchanged |
| 2026-11-02 | Fillable RDANA form and Notice of Loss (`app/fillable.py`): every field a typed input defined as data (control, the paper form's own options, hint, asked-first, personal), validated on the server; saving makes an append-only version with an "edited" audit of field keys; approval names the exact version; regeneration carries the officer's entries and flags evidence that moved under them; Simple mode's "What you need to fill in" with progress; personal fields typed for the printout only, never posted or stored; a print view that is the paper form alone (A4, ruled blanks, DRAFT/APPROVED status line); no "not in reports" placeholders left |
| 2026-11-02 | Fill the whole form on screen, then print it; a "?" help on every field: each RDANA and Notice of Loss field carries a short help (what to enter, an example) and its source as data, read from the official PDFs (the RDANA SOP's Annex I note and headings and section 7.5.2, the PCIC fisheries brochure, application form and farm plan) or, where no document explains it, the form's own label with its Annex I page; a test and the start-up check refuse a field without one. The "?" is a 48 px icon button with a native popover (a bottom sheet on a phone, beside the button on wider screens; Escape and a tap outside close it; hidden in print). Personal details say they are kept on this device only, warn once about shared computers and have a Clear personal details button. Print final form (approved) and Print draft for checking first flag unsaved changes and the office's empty fields |
| 2026-11-02 | Print fidelity: every printed element of the RDANA form and the Notice of Loss audited against the official scans (62 RDANA labels and section titles and 27 options put back into the paper's own words, the old wording still read from saved entries); the printout now follows each form's layout (Annex I's boxes, columns, tables, matrix, title bar and page breaks from a layout table in `app/rdana_form.py`; the PCIC letter's captions, ruled lines and signature block); Ulat's additions removed except one stamp line with the page count in the page margin; an optional, off-by-default "Include the photo evidence page" prints the Notice of Loss photos and measurements as a separate attachment |
| 2026-11-02 | Simple mode's Review as a one-question queue (`app/questions.py`, `_queue.html`): one item at a time with "1 of n", Skip and Back, the report as it came in and one question with one or two big buttons, built from one table of question kinds (possible duplicate with both reports side by side, unknown place with the likely barangays, a photo or pin to double-check, a fallen tree only when a photo or the words suggest one), each answer posting the existing officer action with its audit; Undo in the snackbar before an answer is sent; live updates add new questions at the end ("2 new") without replacing the one being read; initials asked once and kept in the signed session, never shown in Simple mode when known; rarer actions under "More options"; Home's to-do, the Review badge and the queue share one count; Drafts and Reports in plainer words with the history and event details folded away; before/after button counts per screen and screenshots in the slice report |
| 2026-11-02 | US English everywhere: every page, bot reply, form help, README line, comment and private name now uses US spelling (center, color, labeled, canceled, gray, neighbor, acknowledgment, -ize; about 190 words in 56 files), and `tests/test_us_english.py` fails on any British form from its word list, with the few kept forms named and explained (ARIA and asyncio names, the cached photo schema name `DamageJudgement`, and the Muse Image prompts whose hash names the stored photos) |
| 2026-11-02 | Spark and SAM check each other on every photo (`app/crosscheck.py`): a table of rules pairs each Spark judgment with the SAM 3.1 measurement that should back it. When Spark sees floodwater or a partly damaged roof that SAM cannot outline, the report gets a trust signal, and Simple mode's Review asks "Is there floodwater?" with the photo and its outline side by side; the answer is the existing signal review, with what the officer saw as its note. Two more rules are measured only. `scripts.eval_crosscheck` scores every rule for free from the cached photo runs (results file, README and `/accuracy`): Spark 1.3 and SAM disagreed on 22 of 65 labeled photos, the hand label sided with Spark in all 22, and Review asked about 0. The outline is explained where it is shown (Reports, the Notice of Loss annex, Review) |
| 2026-11-02 | Voice Transcribe hears Biñan's streets: `scripts/build_places.py` queries OpenStreetMap (Overpass) for Biñan's named streets, subdivisions, sitios and landmarks, clips them to the barangay polygons, normalizes "St." and "Brgy.", drops ordinary-word, numbered and private-home names and writes `app/data/binan_places.json` (560 names, with its query and data date; ODbL, credited below); the bot sends the barangays first, then those names up to the API's 500-keyword, 30-character limits (found by testing; the docs give none). `scripts.eval_transcribe` gained the two list sizes and a false-insertions count: with 471 OSM names WER was 19.1% against 20.3% for the barangays alone, with 0 false insertions, so the long list ships; no note names a street, so its benefit is not measured yet |
| 2026-11-02 | PCIC rice claims through the same pipeline: the rice Claim for Indemnity (RC-CAS-01, Rev. 2017/FEB; rice has no separate Notice of Loss) as a second commodity entry with all 17 items in the paper's bilingual numbering and wording, "?" help with sources, the farmer's identity and CIC number never stored, and a printout in RC-CAS-01's layout (audited field by field); its 20-day, before-harvest deadline row with source; new crop facts (`crop_stage`, `crop_damage`) and Taglish rice words in the facts prompt; the photo judge says whether a rice field is flooded, lodged or not visibly damaged, with a 19-photo kill test in the results file (SAM could not outline flooded fields, so no field water share is shown); drafts, Home's to-do, the help and the guide name each claim from data; which claims an office drafts is data with a source per line (`app/data/pcic_lines.json`: fisheries on, rice off for Biñan until a source shows a rice area) and one setting, `PCIC_COMMODITIES`; two example rice reports in the demo load only with rice on, pinned outside Biñan; the eval spend ledger now upgrades an older database instead of failing after a paid call |
| 2026-11-02 | Photo evidence wording: the fisheries draft refers to the optional separate photo evidence page without claiming it was included; existing generated drafts display the corrected wording without rewriting their saved snapshots, and fisheries and rice use the same page name |
| 2026-11-02 | Simple draft workflow: RDANA, fisheries and rice show the next action for the saved or edited state, fold optional print-only personal details and group rejection, version history, regeneration, audit and evidence under More; Technical mode keeps its original layout and both views keep the same officer actions |

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
| [htmx](https://github.com/bigskysoftware/htmx) 2.0.11 (vendored in `app/static/vendor/htmx-2.0.11/` with its LICENSE, from the npm package, integrity checked) | 0BSD | Swapping live-updated page sections in place and in-app navigation |
| [Roboto](https://github.com/googlefonts/roboto-classic) variable font, weights 400 to 700, Latin subset (vendored in `app/static/vendor/roboto/` with OFL.txt, from Google Fonts) | SIL OFL 1.1 | Dashboard typeface |
| [Material Symbols](https://github.com/google/material-design-icons) Rounded, 24 px (only the icons the dashboard uses, built into `app/static/vendor/material-symbols/icons.svg` by `scripts/build_icons.py`, with the LICENSE) | Apache-2.0 | Dashboard icons |
| [Material Color Utilities](https://github.com/material-foundation/material-color-utilities) (build-time only, not in this repository: used once to generate the color values written into `app/static/css/app.css`) | Apache-2.0 | Material Design 3 color roles from the seed color |
| [FFmpeg](https://ffmpeg.org) (external program, not bundled) | LGPL/GPL depending on build | Voice note conversion to WAV |
| [Modal](https://github.com/modal-labs/modal-client) client and CLI (not a project dependency: installed separately for deploying, and provided by Modal inside the container) | Apache-2.0 | Hosting: `deploy/modal_app.py` |
| Ulat bot avatar (`assets/brand/ulat-avatar.png`), generated for this project with OpenAI's image model through the Codex CLI ("GPT Terra") | Output owned by the user under OpenAI's [terms of use](https://openai.com/policies/terms-of-use/) | Branding only: the Telegram bot's profile photo. Not part of the report pipeline |

Data:

| Name | License | Used for |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) map tiles (`tile.openstreetmap.org`, loaded by the browser, not stored here) | [ODbL](https://opendatacommons.org/licenses/odbl/) data; tiles CC BY-SA, attributed "© OpenStreetMap contributors" on the map | Basemap of the officer map, under the [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) (light, interactive use) |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) data: Biñan's named streets, subdivisions, sitios and landmarks, queried through the [Overpass API](https://overpass-api.de/) (data as of 2026-11-02) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), © OpenStreetMap contributors | `app/data/binan_places.json` (560 names, with the query that made it; rebuilt by `scripts/build_places.py`): the Voice Transcribe keyword list. It is a derived database under the ODbL, so it stays under the ODbL (share-alike) and keeps this attribution; the MIT License of this repository does not cover it |
| [OCHA COD-AB Philippines](https://data.humdata.org/dataset/cod-ab-phl) admin level 4 (sources: PSA, NAMRIA), v03, valid on 2025-02-13 | [CC BY-IGO](https://creativecommons.org/licenses/by/3.0/igo/) | Barangay names, codes and boundaries for Biñan in `app/data/binan_barangays.geojson` (24 polygons, clipped and simplified to about 3 m). OCHA notes the boundaries are indicative, not official |
| 25 Wikimedia Commons photos of Philippine typhoon damage (eval set `kill-test-25`; **not copied into this repository**; only aggregate metrics are) | CC BY 2.0, CC BY-SA 3.0/4.0 or public domain, per photo | Photo-judge and SAM accuracy in "How well it works". Authors and sources: DFID - UK Department for International Development (CC BY 2.0): [img01](https://commons.wikimedia.org/wiki/File:A_car_up-ended_amid_the_wreckage_of_buildings_destroyed_by_Typhoon_Haiyan_in_Tacloban,_Philippines_(11290331544).jpg), [img02](https://commons.wikimedia.org/wiki/File:A_few_palm_trees_remain_standing_amid_the_destruction_caused_by_Typhoon_Haiyan_in_the_city_of_Tacloban,_Philippines_(11290331484).jpg), [img03](https://commons.wikimedia.org/wiki/File:A_man_stands_surrounded_by_the_devastation_wrought_by_Typhoon_Haiyan_in_the_city_of_Tacloban_(11290380063).jpg), [img06](https://commons.wikimedia.org/wiki/File:Rebuilding_after_Typhoon_Haiyan,_in_Guinan,_Philippines_(13943379519).jpg); Trocaire from Ireland (CC BY 2.0): [img04](https://commons.wikimedia.org/wiki/File:Tacloban_Typhoon_Haiyan_2013-11-14.jpg); Master Sgt. Antoine Robinson (Public domain): [img05](https://commons.wikimedia.org/wiki/File:Operation_Damayan_131123-M-IE150-015.jpg); Exec8 (CC BY-SA 4.0): [img07](https://commons.wikimedia.org/wiki/File:Flooding_in_Culiat_Creek_in_Project_8,_Quezon_City_Philippines_from_Typhoon_Ketsana_(Ondoy).webm), [img08](https://commons.wikimedia.org/wiki/File:Flooding_in_Hereford_Street,_Project_8,_Quezon_City,_Philippines_from_Typhoon_Ketsana_(Ondoy)_1.webm); Exec8 (Public domain): [img09](https://commons.wikimedia.org/wiki/File:Ketsana_002.jpg); Philippinepresidency (CC BY-SA 3.0): [img10](https://commons.wikimedia.org/wiki/File:Ondoy4.jpg); The Philippine Online Chronicles from The Philippines (CC BY 2.0): [img11](https://commons.wikimedia.org/wiki/File:Ondoy_(3967857165).jpg); IRRI Images (CC BY 2.0): [img12](https://commons.wikimedia.org/wiki/File:Typhoon_Ketsana_flood_near_Laguna_de_Bay.jpg); PaulGorduiz106 (CC BY-SA 4.0): [img13](https://commons.wikimedia.org/wiki/File:Damaged_tree_(12-24-2021).jpg), [img16](https://commons.wikimedia.org/wiki/File:Damaged_trees_(12-17-2021).jpg); ALFRED FRIAS / PRESIDENTIAL PHOTO (Public domain): [img14](https://commons.wikimedia.org/wiki/File:Photo_of_Typhoon_Odette-battered_areas_in_Tisa,_Cebu_City_01.jpg), [img15](https://commons.wikimedia.org/wiki/File:Photo_of_Typhoon_Odette-battered_areas_in_Tisa,_Cebu_City_02.jpg); Martin Michlmayr (CC BY-SA 4.0): [img17](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_in_Cebu_City_2021_12_022.jpg), [img18](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_001.jpg), [img19](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_005.jpg), [img20](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_006.jpg), [img21](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_009.jpg), [img22](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_025.jpg), [img23](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_016.jpg), [img24](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_018.jpg), [img25](https://commons.wikimedia.org/wiki/File:Aftermath_of_Typhoon_Rai_on_Mactan_island_2021_12_020.jpg) |
| 19 Wikimedia Commons photos of flooded, flattened and normal rice fields (eval set `rice-field-19`; **not copied into this repository**; only aggregate metrics are) | CC BY 2.0, CC BY 3.0, CC BY-SA 3.0, CC BY-SA 4.0, CC0, Public domain, per photo | Rice-field judge and SAM accuracy in "How well it works". Photos: [Typhoon Ketsana flooded rice field.jpg](https://commons.wikimedia.org/wiki/File:Typhoon_Ketsana_flooded_rice_field.jpg) by Chris Quintana (CC BY 2.0); [Typhoon Ketsana flooded rice field2.jpg](https://commons.wikimedia.org/wiki/File:Typhoon_Ketsana_flooded_rice_field2.jpg) by IRRI Images (CC BY 2.0); [Typhoon Ketsana submerged rice plants just before harvest.jpg](https://commons.wikimedia.org/wiki/File:Typhoon_Ketsana_submerged_rice_plants_just_before_harvest.jpg) by IRRI Images (CC BY 2.0); [Rice fields submerged in flooding in Naga, Camarines Sur.jpg](https://commons.wikimedia.org/wiki/File:Rice_fields_submerged_in_flooding_in_Naga,_Camarines_Sur.jpg) by Naga City Government (Public domain); [Flooded Philippines.jpg](https://commons.wikimedia.org/wiki/File:Flooded_Philippines.jpg) by Sir Mervs from Metro Sta. Rosa, Philippines (CC BY 2.0); [Flood-in-Odisha 2011.jpg](https://commons.wikimedia.org/wiki/File:Flood-in-Odisha_2011.jpg) by Ansuman (CC BY-SA 3.0); [Senja di Sawah kebanjiran.jpg](https://commons.wikimedia.org/wiki/File:Senja_di_Sawah_kebanjiran.jpg) by Aabimut27 (CC BY-SA 4.0); [Blown Rice Field, Bulacan, Philippines - panoramio.jpg](https://commons.wikimedia.org/wiki/File:Blown_Rice_Field,_Bulacan,_Philippines_-_panoramio.jpg) by Andrew Martin (CC BY 3.0); [令和8年8月千葉豪雨によりなぎ倒されたイネ.jpg](https://commons.wikimedia.org/wiki/File:%E4%BB%A4%E5%92%8C8%E5%B9%B48%E6%9C%88%E5%8D%83%E8%91%89%E8%B1%AA%E9%9B%A8%E3%81%AB%E3%82%88%E3%82%8A%E3%81%AA%E3%81%8E%E5%80%92%E3%81%95%E3%82%8C%E3%81%9F%E3%82%A4%E3%83%8D.jpg) by Misei sen (CC0); [倒伏した稲.JPG](https://commons.wikimedia.org/wiki/File:%E5%80%92%E4%BC%8F%E3%81%97%E3%81%9F%E7%A8%B2.JPG) by 多摩に暇人 (talk) (CC BY-SA 3.0); [Rice Field Panorama, Bulacan, Philippines - panoramio.jpg](https://commons.wikimedia.org/wiki/File:Rice_Field_Panorama,_Bulacan,_Philippines_-_panoramio.jpg) by Andrew Martin (CC BY 3.0); [Rice Field Orchard, Bulacan, Philippines - panoramio.jpg](https://commons.wikimedia.org/wiki/File:Rice_Field_Orchard,_Bulacan,_Philippines_-_panoramio.jpg) by Andrew Martin (CC BY 3.0); [Rice Paddy at Calabanga.jpg](https://commons.wikimedia.org/wiki/File:Rice_Paddy_at_Calabanga.jpg) by MarvinBikolano (CC BY-SA 4.0); [Paddy fields in North-Palawan - panoramio.jpg](https://commons.wikimedia.org/wiki/File:Paddy_fields_in_North-Palawan_-_panoramio.jpg) by Tuderna (CC BY 3.0); [Rice Field Irrigation, Bulacan, Philippines - panoramio.jpg](https://commons.wikimedia.org/wiki/File:Rice_Field_Irrigation,_Bulacan,_Philippines_-_panoramio.jpg) by Andrew Martin (CC BY 3.0); [Green Cultivation.jpg](https://commons.wikimedia.org/wiki/File:Green_Cultivation.jpg) by ViaViaL (CC BY-SA 4.0); [Rice seedlings in a flooded paddy, Tsuruga, May 2016.jpg](https://commons.wikimedia.org/wiki/File:Rice_seedlings_in_a_flooded_paddy,_Tsuruga,_May_2016.jpg) by NevinThompson (CC BY-SA 4.0); [Flooded paddy field Raichur Karnataka India monsoon irrigation July 2025.jpg](https://commons.wikimedia.org/wiki/File:Flooded_paddy_field_Raichur_Karnataka_India_monsoon_irrigation_July_2025.jpg) by Vraj Acharya, WELL Labs (CC BY-SA 4.0); [Paddy fields in Dinajpur District 01.jpg](https://commons.wikimedia.org/wiki/File:Paddy_fields_in_Dinajpur_District_01.jpg) by Kritzolina (CC BY-SA 4.0) |

## Repo structure

```
AGENTS.md              Instructions for AI coding agents (Muse Code, Claude Code, …)
.agents/skills/        Provider notes for coding agents: baseten, fireworks, modal (SKILL.md each)
README.md              This file
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

## Team

- Ronnie Cutamora

## License

The code and documentation in this repository are released under the [MIT License](LICENSE), Copyright (c) 2026
Ronnie Cutamora.

The MIT License covers only what was written for this project. Everything listed under
[Third-party code and assets](#third-party-code-and-assets) keeps its own terms: the Python libraries and the
vendored Leaflet and htmx files, the Roboto font and the Material Symbols icons (their licenses are in the table, and the vendored copies keep their LICENSE files),
the OCHA COD-AB boundaries (CC BY-IGO, credit to OCHA, PSA and NAMRIA), the OpenStreetMap tiles and the
OpenStreetMap place names in `app/data/binan_places.json` (ODbL, share-alike, "© OpenStreetMap contributors"), the Wikimedia Commons eval photos (CC BY, CC BY-SA or public domain per photo; not in
this repository, credited by author above) and the bot avatar (see its row). Meta's models are used through their
APIs under Meta's terms and are not part of this repository.
