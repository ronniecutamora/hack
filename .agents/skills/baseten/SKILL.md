---
name: baseten
description: Call LLMs hosted on Baseten Model APIs (OpenAI-compatible chat completions), including Meta's open-weight Muse Spark Open. Use when code or a task mentions Baseten, BASETEN_API_KEY, inference.baseten.co, `meta-models/...` model IDs, or running an open-weight model through a partner API; also when a Baseten call returns empty content, is cut off at max_tokens, or is rejected for a bad model ID.
---

# Baseten Model APIs

Baseten serves hosted models through an **OpenAI-compatible** Chat Completions endpoint. Facts marked ✅ were checked
with real calls; ⚠️ means unverified, so check the live docs (<https://docs.baseten.co>) before relying on it.

## Connect

| What | Value |
|---|---|
| Base URL | `https://inference.baseten.co/v1` ✅ |
| Chat endpoint | `POST /chat/completions` ✅ |
| Auth header | `Authorization: Api-Key <BASETEN_API_KEY>` ✅. Whether `Bearer` also works (the OpenAI SDK's default) is ⚠️ unverified, so set the header explicitly |
| Key | Read `BASETEN_API_KEY` from the environment or a git-ignored `.env`. Never print, log or commit it |

```python
import os, httpx

r = httpx.post(
    "https://inference.baseten.co/v1/chat/completions",
    headers={"Authorization": f"Api-Key {os.environ['BASETEN_API_KEY']}"},
    json={"model": "meta-models/muse-spark-open", "max_tokens": 4096,
          "messages": [{"role": "user", "content": "Reply with OK."}]},
    timeout=120,
)
r.raise_for_status()
msg = r.json()["choices"][0]["message"]
print(msg.get("content"), "| finish:", r.json()["choices"][0].get("finish_reason"))
```

## Verified models

| Model ID | Notes |
|---|---|
| `meta-models/muse-spark-open` ✅ | Meta's open-weight Muse Spark Open (reasoning model). **`…-1026` is rejected** ✅, even though one guide lists it. Accepts images and `json_schema` ✅ |

List what your key can use rather than guessing an ID: `GET https://inference.baseten.co/v1/models` with the same header (⚠️ check that the endpoint exists for your account).

## Request features

- **Images** ✅: OpenAI-style content parts with a base64 data URL:
  `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<...>"}}`.
- **Structured output** ✅: `response_format: {"type": "json_schema", "json_schema": {"name": "...", "schema": {...}, "strict": true}}`.
  Still validate the parsed JSON against your schema yourself; never trust a reply blindly.
- **Reasoning** ✅: reasoning text comes back in `message.reasoning_content`; the answer is in `message.content`.

## Gotchas (all hit in practice)

1. **Reasoning tokens count against `max_tokens`.** A photo-judging call used about 1,900 completion tokens, about
   1,800 of them reasoning. With a small budget, `content` comes back **empty** and `finish_reason == "length"`.
   - Start structured-output calls at **8,192**.
   - On `length` with empty content, **retry once with double the budget**. One long Taglish transcript needed more than 8,192.
2. **Never cache a reply you haven't validated.** A truncated or schema-invalid reply that gets cached is replayed on every
   re-run and can't be fixed by retrying. Validate first, then cache.
3. **Wrong model ID → 4xx.** Use the IDs above or list models; don't copy IDs from slides or guides.
4. **Latency:** reasoning makes calls take seconds to tens of seconds. Use generous timeouts (≥120 s), and don't run calls
   inside a database transaction.

## Cost and credits

- Hackathon credits are tracked in the Baseten dashboard. The per-token price wasn't checked (⚠️), so read it from the
  pricing page or the dashboard before running bulk jobs.
- Mock HTTP in tests (e.g. `respx`), cache real responses during development, and never loop paid calls in a test suite.

## Checklist before shipping a Baseten call

- [ ] The model ID comes from the verified table or a model listing.
- [ ] `max_tokens` leaves room for reasoning, with one retry on `finish_reason == "length"`.
- [ ] Replies are validated against the schema **before** caching.
- [ ] The key comes from the environment; nothing logs headers.
- [ ] Tests mock the endpoint.
