---
name: modal
description: Deploy and run Python on Modal (serverless containers and GPUs) — web apps (FastAPI/ASGI), webhook bots, scheduled jobs, secrets, volumes, and self-hosted model serving with vLLM. Use when code or a task mentions Modal, `modal deploy`/`modal run`/`modal serve`, `import modal`, `*.modal.run` URLs, hosting a live demo, keeping a container warm, persisting SQLite or files between runs, or renting a GPU for inference.
---

# Modal

Modal runs Python functions and web apps in cloud containers that you define in code. Facts marked ✅ come from Modal's docs
(checked 2026-10-28; sources at the bottom). ⚠️ means unverified or inferred. This skill hasn't yet been exercised with a real deploy.

## Setup (one time, by a human)

```bash
pip install modal            # or: uv add modal
python3 -m modal setup       # opens a browser to create an API token (~/.modal.toml)
```

Agents must not run `modal setup` themselves, since it's an interactive sign-in. Check that a token exists instead (`modal profile current`).

## Core pattern

```python
import modal

app = modal.App("my-app")
image = modal.Image.debian_slim(python_version="3.12").uv_pip_install("fastapi", "httpx")

@app.function(image=image)
def square(x: int) -> int:
    return x * x

@app.local_entrypoint()
def main():
    print(square.remote(42))
```

- `modal run file.py` runs it once.
- `modal serve file.py` gives a dev server with hot reload and a temporary URL.
- `modal deploy file.py` makes it persistent and gives a stable URL.

## Web apps (FastAPI / ASGI) ✅

```python
@app.function(image=image, secrets=[modal.Secret.from_name("my-secrets")],
              min_containers=1, max_containers=1)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def web():
    from myproject.main import create_app
    return create_app()
```

- URL shape: `https://<workspace>--<app>-<function>.modal.run` ✅. WebSockets work ✅.
- Auth: either Modal proxy auth (`Modal-Key` and `Modal-Secret` headers) or your own check in the app ✅. A public demo should
  still put a login on admin/officer routes.
- `@modal.fastapi_endpoint()` is for a single function instead of a whole app ✅.

## Bots: use webhooks, not polling

- Functions time out after 300 s by default (max 24 h) ✅, and web containers are request-driven. A long-polling loop is not a
  Modal pattern ⚠️.
- **Telegram:** mount a webhook route (e.g. `POST /telegram/<random-path>`) on the same ASGI app. Feed updates to
  python-telegram-bot's `Application.process_update`, and call `setWebhook` once after deploy with a `secret_token`. Then
  verify the `X-Telegram-Bot-Api-Secret-Token` header on every request ✅ (Telegram API).
  - Webhooks and `getUpdates` are mutually exclusive.
  - Telegram keeps undelivered updates for 24 h.
- Example: `07_web/discord_bot.py` in modal-examples (a webhook bot on `@asgi_app`).

## Keeping it warm and the cost

- `min_containers=1` keeps one container always up (no cold starts) ✅. `scaledown_window` defaults to 60 s (2 s–20 min) ✅.
- CPU price is $0.0000131 per core-second and memory $0.00000222 per GiB-second, billed at the higher of requested or used ✅.
  One always-warm container for 7 days costs about **$1.2** (0.125 core / 128 MiB) or about **$2.7** (0.25 core / 0.5 GiB).
- The Starter plan includes $30/month of compute ✅. GPUs need a payment method on file ✅.

## Secrets ✅

```bash
modal secret create my-secrets API_KEY=... OTHER_KEY=...   # or the dashboard Secrets page
```

Attach them with `secrets=[modal.Secret.from_name("my-secrets")]`; the values appear as environment variables. `Secret.from_dotenv()`
reads a local `.env`. Never commit that file.

## Volumes and SQLite (important)

- A Volume is a persistent file store. Writes must be **committed** (in the background every few seconds, and on shutdown) and **reloaded** to be
  seen elsewhere ✅. Concurrent writes to one file are **last-write-wins, with no file locking** ✅.
- **SQLite:** keep exactly **one writer**: one container (`max_containers=1`), one process. WAL on a Volume isn't documented ⚠️. The safest pattern:
  1. Keep the live DB on the container's local disk.
  2. Restore it from the Volume at startup.
  3. Periodically, and at shutdown, `sqlite3 .backup` to the Volume, then call `volume.commit()`.
  The loss window is the snapshot interval.
- Media files (photos, audio) can live directly on the Volume.

## Scheduled jobs ✅

`@app.function(schedule=modal.Cron("0 8 * * *"))` or `modal.Period(hours=1)`. These need `modal deploy`. To pause a schedule, remove it and redeploy.

## GPUs and vLLM (self-hosted open models)

- Prices per hour (from per-second rates ✅): L4 about $0.80, L40S about $1.95, A100-80GB about $2.50, H100 about $3.95, H200 about $4.54.
- Example: `06_gpu_and_ml/llm-serving/vllm_inference.py`. It serves an OpenAI-compatible `/v1/chat/completions`, keeps the weights and
  the compile cache on Volumes, and its cold start is "tens of seconds to a few minutes", about 10 s from the cache ✅.
- A ~30B dense model in BF16 needs about 60 GB → A100-80GB or H100 ⚠️ (estimate). 4-bit builds may fit an L40S ⚠️.
- **Leave GPU apps scaled to zero.** One H100 left on for 24 h costs about $95.

## Agent skills from Modal

`modal skills install [-g] [--no-docs] [-y]` installs Modal's own agent skills into `.agents/` (or `--claude` for `.claude/`) ✅.
Prefer `-g` (home directory) over installing into a project you don't want to change.

## Checklist before deploying

- [ ] A token exists (`modal profile current`). Secrets were created in Modal, not hard-coded.
- [ ] The web app has `max_containers=1` if it writes SQLite. The bot uses a webhook with a secret token.
- [ ] The persistence pattern is chosen (local DB plus Volume snapshots) and tested by redeploying.
- [ ] Admin routes are protected. GPU functions scale to zero.
- [ ] You know the cost of anything that's always warm.

## Sources

Docs: https://modal.com/docs/guide/webhooks · /docs/guide/volumes · /docs/guide/secrets · /docs/guide/cold-start ·
/docs/guide/timeouts · /docs/guide/cron · /docs/guide/gpu · /docs/cli/latest/skills · https://modal.com/pricing ·
Examples: https://github.com/modal-labs/modal-examples (`07_web/fastapi_app.py`, `07_web/discord_bot.py`,
`06_gpu_and_ml/llm-serving/vllm_inference.py`) · Telegram: https://core.telegram.org/bots/api#setwebhook
