---
name: fireworks
description: Call models on Fireworks AI (OpenAI-compatible serverless inference), including Meta's open-weight Muse Spark Open and Muse Glimmer. Use when code or a task mentions Fireworks, FIREWORKS_API_KEY, api.fireworks.ai, `accounts/fireworks/models/...` IDs, structured JSON output from an open-weight model, or Fireworks rate limits, 429s, 503s, on-demand deployments or fine-tuning.
---

# Fireworks AI

Fireworks serves open models through an **OpenAI-compatible** API. Facts marked ✅ come from Fireworks' own docs or model
pages (checked 2026-10-28; sources at the bottom). ⚠️ means unverified or contradictory, so **make one test call before you build on it.**
This skill has not yet been exercised with real calls.

## Connect

| What | Value |
|---|---|
| Base URL | `https://api.fireworks.ai/inference/v1` ✅ |
| Chat endpoint | `POST /chat/completions` ✅ |
| Auth | `Authorization: Bearer <FIREWORKS_API_KEY>` ✅. The OpenAI SDK sets it from `api_key` |
| Key | Read `FIREWORKS_API_KEY` from the environment or a git-ignored `.env`. Never print, log or commit it |

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["FIREWORKS_API_KEY"], base_url="https://api.fireworks.ai/inference/v1")
r = client.chat.completions.create(
    model="accounts/fireworks/models/muse-spark-open",
    max_tokens=2048,
    messages=[{"role": "user", "content": "Reply with OK."}],
)
print(r.choices[0].message.content, r.choices[0].finish_reason)
```

## Meta models on Fireworks

| Model | ID | Serverless | Price per 1M tokens | Notes |
|---|---|---|---|---|
| Muse Spark Open | `accounts/fireworks/models/muse-spark-open` ✅ | Yes ✅ | $1.25 in / $0.15 cached / $4.25 out ✅ | 816B MoE (42B active), 1.04M context, image input ✅. A reasoning model |
| Muse Glimmer 30B | `accounts/fireworks/models/muse-glimmer-30b` ✅ | **⚠️ Contradictory.** The model page says serverless is "Not Supported" (deprecated from 2026-09-25, on-demand only), while the launch blog says serverless | Not on Fireworks' page. A third party lists $0.35 / $1.50 ⚠️ | 29.7B dense, Apache 2.0, 131k context, image input ✅. SFT fine-tuning free until 11/30 ✅ |

Check availability with one call. If Glimmer returns a "model not found / not deployed" error, either use Spark Open or
create an on-demand deployment (see below).

## Request features

- **Structured output** ✅: `response_format={"type": "json_schema", "json_schema": {"name": "Result", "schema": {...}}}`.
  Also `json_object` and Grammar mode.
  - ⚠️ The Meta model pages don't state json_schema support. Test it.
  - **On reasoning models, json_schema turns off the reasoning output** ✅. Say "answer in JSON" in the prompt and give enough `max_tokens`, or `finish_reason == "length"` truncates the JSON.
- **Images** ✅: OpenAI-style `image_url` parts with a URL or a `data:image/jpeg;base64,...` data URL.
  - Up to 120 images per request.
  - Base64 images: 10 MB in total per request.
  - URL images: under 5 MB each, and they must be fetchable within 1.5 s.
  - Formats: PNG, JPG, GIF, BMP, TIFF, PPM.
  - ⚠️ Images together with json_schema are untested.

## Rate limits and credits

- **10 requests/minute without a payment method or active credits** ✅. Up to 6,000 RPM with a payment method and credits ✅.
  - ⚠️ Whether promotional or hackathon credits lift the 10 RPM cap is unknown. A bulk job (an eval, a batch judge) at 10 RPM crawls, so check the account tier first.
  - Handle `429` with backoff.
- Tiers: Tier 1 needs a valid payment method. Tier 2 starts at $50 spent or added ✅.

## On-demand deployments (only if serverless isn't available)

- Create: `firectl deployment create accounts/fireworks/models/<MODEL> --deployment-shape default --wait` ✅.
  Query it as `accounts/<ACCOUNT_ID>/deployments/<DEPLOYMENT_ID>`.
- Billed **per GPU-second** (H100/H200 about $8/h on the pricing page) ✅. It scales to zero after 1 h idle by default, and while it boots
  requests get **503**, so retry with backoff ✅. **Delete deployments when you're done**; they cost money while warm.

## Gotchas

1. Model IDs are full paths (`accounts/fireworks/models/...`). Short names fail.
2. Validate structured replies against your schema **before caching them**. Never cache truncated or invalid output.
3. Mock HTTP in tests (e.g. `respx`), and never loop paid calls in a test suite.

## Checklist

- [ ] One live test call confirms the model ID is served (especially Glimmer).
- [ ] json_schema (and images, if used) have been tested on that model.
- [ ] The account isn't stuck at 10 RPM for bulk jobs.
- [ ] Replies are validated before caching; 429 and 503 are retried with backoff.
- [ ] Any on-demand deployment is deleted after use.

## Sources

- Quickstart: https://docs.fireworks.ai/getting-started/quickstart.md
- Structured output: https://docs.fireworks.ai/structured-responses/structured-response-formatting.md
- Vision: https://docs.fireworks.ai/guides/querying-vision-language-models.md
- Rate limits: https://docs.fireworks.ai/guides/quotas_usage/rate-limits.md
- On-demand: https://docs.fireworks.ai/guides/ondemand-deployments.md
- Models: https://fireworks.ai/models/fireworks/muse-spark-open · https://fireworks.ai/models/fireworks/muse-glimmer-30b
- Pricing: https://fireworks.ai/pricing
