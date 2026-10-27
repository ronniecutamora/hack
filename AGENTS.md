# AGENTS.md

Instructions for AI coding agents (Muse Code, Claude Code, Codex, etc.) working in this repository.

**Read `~/.claude/CLAUDE.md` first** (if it exists on this machine). It holds the developer's machine-wide rules, which apply here too. Rules in this file add to it.

**Commit messages:** Conventional Commits, and **never** add an author or attribution footer (`Co-Authored-By:`, "Generated with …", or anything else naming an AI tool). This also applies to PR descriptions.

## What this repo is

An entry for the **Meta Global AI Developer Hackathon** (Cerebral Valley, Oct 27 – Nov 4, 2026).

- **Builder:** Ronnie Cutamora (solo for now; teams may have up to 4 people)
- **Track:** Indie Developer (tentative)
- **Problem statement:** "Build for Your World" (tentative). We are building for the Philippines.
- **Project:** **typhoon damage assessment for LGUs** (chosen 2026-10-26). Residents and barangay officials send photos, voice notes, and Taglish texts. SAM 3.1 segments damage, Voice Transcribe turns voice reports into text, and an agent (Muse Spark / Muse Spark Open on Baseten) deduplicates, geolocates, and maps the reports, then drafts the LGU's Rapid Damage Assessment and Needs Analysis (RDANA) report for an officer to approve. The coordination and reporting agent is the core of the product; the photo analysis supports it. Don't let the product drift into a "simple image analyzer". Scope and architecture are not final, so confirm before scaffolding.

## Hard rules (breaking these can disqualify the entry)

1. **The product must use Meta's models** (see the model table below). A Meta model must do the core work, not be a side feature.
2. **New work only.** Everything here must be built during the event (from Oct 27, 2026). Don't copy in code from other projects. If you add a third-party library or asset, check that its license allows it and record it in the README.
3. **Public repo.** Never commit secrets, API keys, `.env` files, personal data, or recordings of real people.
4. **Keep the record of new work up to date.** Every feature or meaningful change gets a line in the README's "Built during the hackathon" log. Judges must be able to see what was built here.
5. **Banned project types:** medical or mental-health advice, basic RAG apps, simple image analyzers, simple education chatbots, job-application screeners, nutrition coaches, personality analyzers, sports coaches, anything political or war-related. If a feature drifts toward one of these, flag it.

## Meta models

| Model | ID / where it runs | Use it for | Gotchas |
|---|---|---|---|
| Muse Spark 1.3 | `muse-spark-1.3` (Meta Model API) | Default LLM: agents, reasoning, vision, tool calls | **Audio input is degraded on 1.3.** For audio, use `muse-spark-1.2` or Voice Transcribe |
| Muse Spark 1.2 | `muse-spark-1.2` | Audio input | — |
| Muse Wonder | `muse-wonder-1.0` | The hardest reasoning or long agent runs only | Preview; costs $4 / $20 per 1M tokens, so use it sparingly |
| Muse Image | `muse-image-1.0` | Image generation and editing | $0.01 per image |
| Muse Voice Transcribe | `muse-voice-transcribe-1.0` | Streaming speech-to-text, speaker labels, code-switching | **Tagalog is one of the 25 evaluated languages** (code-switching supported); Taglish quality still needs a test. **Cebuano, Waray, Bikol, Hiligaynon are not on the list.** Max 10 min per transcribe request. Turn-level timestamps only, no word-level |
| SAM 3.1 | `sam-3.1` | Text-prompted segmentation and tracking in images and video | Returns boxes and masks. **One concept per request**; the prompt must be a short noun phrase for a concrete object ("roof", "fallen tree"), not a condition ("damaged roof"). $2.50 / 1,000 images; video requests bill at least 50 frames |
| Muse Spark Open | Baseten `meta-models/muse-spark-open-1026` | Open-weight LLM (needed for the Open-Weight bonus prize) | Not on Meta's API. Use the `-1026` ID, not the one in Baseten's docs |
| Muse Glimmer | Local (Ollama, LM Studio, vLLM, llama.cpp) | Small open-weight LLM (Apache 2.0, 30B) | A 4-bit build needs about 24–32 GB of GPU or unified memory |

Docs: <https://dev.meta.ai/docs/overview>. Check against the live docs before relying on any model ID or parameter. Don't guess endpoint URLs or request shapes.

## Secrets and config

- Meta: `META_API_KEY`. Baseten: `BASETEN_API_KEY`. Read them from the environment or a git-ignored `.env`. Commit only `.env.example` with empty values.
- **Budget:** $150 of Meta credits with a hard $150 spend limit. Use Spark 1.3 for bulk calls; cache results during development; don't loop expensive calls in tests (mock them).
- **Contributor tier** (`*-contributor` models) lets Meta train on the data. Only use it with synthetic or public data, never with user data.

## Privacy

- Follow the Philippine Data Privacy Act (RA 10173). Collect the minimum: pseudonymous IDs, no names in prompts, and delete raw audio and images after processing unless the user opts in.
- Use synthetic or consented data for demos. Don't commit real people's faces, voices, or documents.
- If the project touches schools: DepEd Order 003 s.2026 prohibits biometric and emotion recognition. Don't add voice- or face-based identification.

## How to work

- **The demo comes first.** Judging: Impact 30%, Meta tooling 25%, Technical depth 20%, Demo 25%. One flawless end-to-end flow beats many half-working features.
- **Verify claims.** Any statistic shown in the app, README, or video needs a cited source (a link in the README or a code comment). Mark unverified numbers ⚠️.
- **Small commits** with Conventional Commit messages (`feat:`, `fix:`, `docs:`, …). Commit before long or risky operations.
- **Evaluate, don't assume.** For any model-driven score or classification, keep a small labeled test set and report agreement numbers. Judges reward verifiable agents.
- Don't run heavy local jobs (local Glimmer inference, big builds) back to back. The dev machine is shared and must stay responsive.

## Key dates (Asia/Manila, GMT+8)

| When | What |
|---|---|
| Tue Oct 27, 12:00 AM | Kickoff (Oct 26, 12 PM ET) |
| Wed Oct 28 / Thu Oct 29 / Sat Oct 31, 12:00 AM | Live sessions: Agents 101, Agents 102 (verifiable agents), Fireworks / Muse Open |
| Weekdays 10–11 PM (11 PM–12 AM from Nov 2) | Office hours in Discord `#office-hours` |
| **Thu Nov 5, 10:00 AM** | **Submission deadline** (Nov 4, 9 PM EST) and last day to claim credits |
