# Deploying Ulat on Modal

One always-on Modal container serves the officer dashboard and the Telegram bot's webhook. The
code is [modal_app.py](modal_app.py); the targets (app, URL, secret, volume, bot, spend cap) are
data in [app/deploy_targets.py](../app/deploy_targets.py); every step below is a subcommand of
`python -m app.deploy` ([app/deploy.py](../app/deploy.py)).

| Target | App | URL | Bot | Secret | Volume | Spend cap |
|---|---|---|---|---|---|---|
| staging | `ulat-staging` | `https://<workspace>--ulat-staging.modal.run` | @UlatPH_dev_bot (`TELEGRAM_DEV_BOT_TOKEN`) | `ulat-staging-keys` | `ulat-staging-data` | $2/day |
| production | `ulat` | `https://<workspace>--ulat.modal.run` | @UlatPH_bot (`TELEGRAM_BOT_TOKEN`) | `ulat-keys` | `ulat-data` | $10/day |

Use the URL without a region. Modal also prints a regional alias (`…--ulat-staging.us-east.modal.run`),
which the app refuses with "Invalid host header" because `ALLOWED_HOSTS` names only the canonical host.

## Requirements

- The Modal CLI (`pip install modal`, 1.6 or newer) and a signed-in profile (`modal profile current`). `modal` is
  not a project dependency: only `modal_app.py` imports it, under the CLI's own Python, and Modal installs it in
  the container image.
- `.env` with `META_API_KEY`, the target's bot token and (production) `PSEUDONYM_SALT`. The other secrets are
  generated.

## Commands

Run from the repo root. Production commands that change something need `--yes`.

```bash
# 1. Secrets: .env -> Modal secret, through a private temp file (values are never printed or put on a command
#    line). Missing generated values (STAGING_/PRODUCTION_ TELEGRAM_WEBHOOK_SECRET, OFFICER_PASSCODE,
#    SESSION_SECRET, and staging's PSEUDONYM_SALT) are appended to .env first; only their names are printed.
uv run python -m app.deploy secrets --target staging

# 2. Demo assets for the first start: the generated scenario photos and the model-response cache (synthetic only).
uv run python -m app.deploy seed-assets --target staging            # --from var/demo by default

# 3. Deploy the committed HEAD (not the working tree), tagged with its commit, with the recreate strategy so
#    the old container stops (final snapshot) before the new one starts.
uv run python -m app.deploy deploy --target staging

# 4. Point the bot at it. Never automatic: a staging boot cannot take a bot from anywhere else.
uv run python -m app.deploy webhook set --target staging            # info | set | delete
uv run python -m app.deploy webhook info --target staging           # pending updates and Telegram's last error

# Operate
uv run python -m app.deploy logs --target staging                   # stream logs (modal app logs)
uv run python -m app.deploy snapshots --target staging              # list database snapshots
uv run python -m app.deploy restore --target staging app-20261030T101500000000Z.db   # restore one, restart
uv run python -m app.deploy stop --target staging                   # stop (final snapshot); deploy again to start
```

The first start on an empty volume seeds the synthetic demo by running `python -m app.demo load --anchor-now`
in the container (18 reports through the real pipeline, about 2 minutes, $0.03 of model calls with the uploaded
cache on 2026-10-30); later starts restore the newest snapshot instead.

**Officer access:** the passcode is `STAGING_OFFICER_PASSCODE` / `PRODUCTION_OFFICER_PASSCODE` in `.env`. Read it
there; never paste it into the repo, an issue or a chat log. Changing it (then `secrets` and `deploy`) signs
every session out.

## Rollback (back to the local bot)

1. `uv run python -m app.deploy webhook delete --target production --yes` (pending updates are kept).
2. Start the bot locally: `uv run python -m app.bot`. It refuses to poll while a webhook is set, so step 1 comes
   first.
3. Optionally `uv run python -m app.deploy stop --target production --yes`. The volume and snapshots stay.

A bad deploy without a data problem: `modal app rollback --strategy recreate ulat` redeploys the previous version, and the new
container restores the latest snapshot. A data problem: `restore` a snapshot from before it (the replaced
database is kept on the container as `app.db.before-restore` until the next restart).

## Durability

- The live SQLite database is on the container's disk (`/var/lib/ulat/app.db`); the volume only receives finished
  snapshot files. The app takes an online backup (SQLite backup API), renames it into place and commits the
  volume every 2 minutes (`SNAPSHOT_INTERVAL_MINUTES`), within 5 seconds of each update the bot handled
  (`SNAPSHOT_SOON_S`, at most one per 5 s) and at shutdown. The newest 15 at least a minute apart are kept, plus
  one per day for 7 days. Media files and the model cache are written to the volume directly.
- **Shutdown** is bounded to fit Modal's 30 s (constants in [app/bot.py](../app/bot.py)): new webhook calls get
  503 (Telegram re-sends them to the next container); the updates already answered with 200 are handled (up to
  8 s) and snapshotted at once; in-flight assessments get 8 s, and the summary reply skips its 15 s quiet
  period; the bot stops (up to 4 s); then the final snapshot. An assessment or grouping cut off there resumes at
  the next start. Measured on staging (2026-10-30): an update posted 2.5 s before `modal app stop` was in a
  snapshot 0.6 s after it was handled; a stop with an assessment in flight took about 18 s from the stop
  command to the lease release; after the restart both test reports were there and were assessed and grouped.
- **Loss window:** a graceful stop, redeploy or preemption loses nothing the bot acknowledged. A crash that skips
  shutdown loses at most about 5 seconds of acknowledged reports (plus a webhook update still in the queue: answered
  200 but not yet handled, usually under a second) and at most the last 2 minutes of other database writes
  (assessments, officer actions). Media are committed every few seconds.
- A lease file (`snapshots/LEASE.json`) keeps one writer: a starting container waits up to 45 s for the previous
  one to release it, and takes over a lease that has not been refreshed for 2 intervals + 60 s.
- Download a snapshot for an offline copy: `modal volume get ulat-data snapshots/<name> .` (it holds personal
  data: keep it off shared drives and out of the repo).

## Cost

One container at 0.25 CPU and 512 MiB, always on: about $2.65 per 7 days at Modal's list rates
($0.0000131 per core-second, $0.00000222 per GiB-second; [modal.com/pricing](https://modal.com/pricing), checked
2026-10-28, billed at the higher of requested and used). Model calls are capped per target by
`DAILY_SPEND_CAP_USD` ($2 staging, $10 production) on top of the per-reporter limits.

## Production cut-over checklist

Only after Ronnie's go. Staging must have passed the same steps first.

**Done on 2026-10-30:** production went live at 13:30 Manila on commit `8510536`
(<https://ronniecutamora47--ulat.modal.run>, @UlatPH_bot on the webhook, the local polling bot stopped), steps 1–8
completed. Keep the list for a re-run, for example after rolling back to the local bot.

1. `git status` clean on the commit to ship; the remote test runner is green.
2. `uv run python -m app.deploy secrets --target production --yes` (generates the production webhook secret,
   passcode and session secret into `.env`).
3. `uv run python -m app.deploy seed-assets --target production --yes` (only if the public instance should start
   with the synthetic demo).
4. `uv run python -m app.deploy deploy --target production --yes`; wait for the first start (seeding takes about
   2 minutes), then open `https://<workspace>--ulat.modal.run/login`, sign in, check the overview and `/health`.
5. Stop the local polling bot (Ctrl+C on `uv run python -m app.bot`).
6. `uv run python -m app.deploy webhook set --target production --yes`, then `webhook info`: no last error.
7. Send @UlatPH_bot a test message from a phone; it answers "received" and the report appears on the overview.
8. Put the URL in the README's Demo section and the passcode in the submission's private judging notes.
9. Watch `logs` for a few minutes. To undo: Rollback above.

## Gotchas

- Modal logs every request path. The webhook path is derived from the secret (HMAC), not the secret itself, and
  the secret header is never logged, but treat Modal log access as access to that path.
- The seeded demo's photos are never purged: the retention purge skips the `demo` channel (generated images,
  invented reporters, no personal data; see [app/retention.py](../app/retention.py)), so a deployed demo keeps its
  evidence. Every real report's raw photos and voice notes are still deleted `RAW_MEDIA_TTL_HOURS` (24) after
  arrival.
- The login limit (10 wrong passcodes per 15 minutes) is per client address: the last `X-Forwarded-For` hop, which
  Modal's proxy adds. Forged entries before it are ignored (checked on staging 2026-10-30, see
  [app/auth.py](../app/auth.py)).
- `--strategy recreate` matters: a rolling deploy would start the new container while the old one still writes.
