# Daily Check Bot

Async Telegram workflow for dispatcher-led daily vehicle safety checks. Built with Python 3.11, aiogram v3, asyncpg, and APScheduler.

## Features
- Dispatcher-only commands and review controls inside a dedicated supergroup topic
- Driver streak tracking, media intake (albums supported), and manual review outcomes
- Autosend reminders, daily digest posts, hourly compliance snapshots, and automatic midnight streak resets
- Dispatcher broadcasts via `/announce` (inline one-liner or guided flow)
- Docker Compose stack with Postgres and SQL migrations

## Quick Start

### 1. Environment
Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Key variables:
- `BOT_TOKEN` — BotFather token
- `DATABASE_URL` — e.g. `postgresql://postgres:postgres@db:5432/postgres`
- `DATABASE_READONLY_URL` — read-only connection string used by the dashboard (defaults to the `dashboard_reader` role)
- `ADMIN_ONLY_REVIEW` — `true` to restrict dispatcher actions to admins
- `TZ` — default timezone fallback (per-group overrides stored in DB). Default: `America/Chicago`.
- `DIGEST_TIME` — fallback digest time (`HH:MM`)
- `DASHBOARD_BASIC_USER` / `DASHBOARD_BASIC_PASSWORD` — HTTP basic auth credentials for the dashboard
- `DASHBOARD_PORT` — dashboard host port (defaults to `8000`)
- `DASHBOARD_TITLE` — optional HTML title override

### 2. Start the stack

```bash
docker compose up -d db
```

Run the migrations (apply `migrations/001_init.sql`, `migrations/002_add_driver_notify_chat.sql`, `migrations/003_compliance_features.sql`, and `migrations/004_add_trailer_topic.sql`):

```bash
docker compose run --rm bot bash -lc "python - <<'PY'
import asyncio, asyncpg, os, pathlib
async def main():
    parts = [
        pathlib.Path("migrations/001_init.sql"),
        pathlib.Path("migrations/002_add_driver_notify_chat.sql"),
        pathlib.Path("migrations/003_compliance_features.sql"),
        pathlib.Path("migrations/004_add_trailer_topic.sql"),
        pathlib.Path("migrations/005_dashboard_support.sql"),
    ]
    sql = "
".join(p.read_text() for p in parts)
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    try:
        await conn.execute(sql)
    finally:
        await conn.close()
asyncio.run(main())
PY"
```

Then start the bot and dashboard services:

```bash
docker compose up -d bot dashboard
```

### 3. Wire the dispatcher topic
1. Add the bot to the dispatcher supergroup.
2. Create (or open) the “Daily Checks” topic.
3. Run `/set_topic` inside that topic to persist the chat and thread IDs.
4. Use `/status` anytime for a live snapshot (IDs, pause state, autosend/digest times, daily stats).

## Dashboard Service
- Serves at `http://localhost:${DASHBOARD_PORT:-8000}` with HTTP basic auth (`DASHBOARD_BASIC_USER` / `DASHBOARD_BASIC_PASSWORD`).
- Reads from Postgres using the `dashboard_reader` role created in `migrations/005_dashboard_support.sql` (default password `dashboard_reader` — change it with `ALTER ROLE`).
- JSON endpoints:
  - `GET /api/compliance/summary` — total drivers, passes, pendings, last compliance reset.
  - `GET /api/compliance/pending` — current pending drivers with usernames, pass counts (7 days), and last notification timestamps.
  - `GET /api/drivers/{id}/checkins` — last seven days of check-in history for a driver.
- The HTML dashboard (`/`) renders the same data plus a link reminding dispatch to run `/compliance_report` from Telegram (Webhook integration can be added later).
- Launch alongside the bot via `docker compose up -d bot dashboard`. Hot reload locally with `uvicorn dashboard.main:app --reload` after installing `dashboard/requirements.txt`.


## Daily Operations
- `/notify` — Post a reminder in the current driver chat (or DM if no chat is linked) and update their review card.
- `/autosend on 09:00` / `/autosend off` — Toggle scheduled reminders (per dispatcher group).
- `/reopen @driver [YYYY-MM-DD]` — Reopen a finished record for follow-up.
- `/reset @driver [YYYY-MM-DD]` — Clear today's notification/media state so reminders can resend.
- `/reset all` — Reset reminder state for all active drivers today.
- `/set_timezone America/Chicago` — Update the dispatcher group’s saved timezone (reschedules jobs).
- `/set_compliance_topic` — Run inside the dispatcher topic thread where you want hourly compliance reports.
- `/set_trailer_topic` — Run inside the dispatcher topic where trailer alerts should be posted.
- `/compliance_report` — Manually post the compliance summary and pending driver cards now.
- `/compliance_reset [YYYY-MM-DD]` — Reset PTI/compliance state for all drivers (default today, testing only).
- `/announce` — Broadcast to drivers/dispatch (inline parser or guided buttons).
- Review buttons (Pass, Fail, Needs Fix, Notify Today, Refresh) work only for dispatchers inside the saved topic.

### Automatic jobs
- **Autosend** — Daily reminder in each driver's linked chat (falls back to DM) at the configured time (skipped while paused).
- **Compliance report (every 2h)** — Posts to the saved compliance topic with 24h stats, pending drivers, and individual Pass/Comment actions.
- **Daily digest** — Summary post in the dispatcher topic at each group’s digest time (default `DIGEST_TIME`).
- **Daily compliance snapshot** — At reset (00:05 local), posts the prior day’s completion summary and top/worst performers in the compliance topic.
- **Weekly leaderboard** — Monday 06:00 (group TZ) top/worst compliance percentages for the past 7 days.
- **Midnight reset** — Resets streaks at 00:05 (group timezone) for drivers without a record the previous day.

### Compliance & escalations
- Hourly report lists non-compliant drivers, grouped by their chat. Exceptions (Needs Fix, excused, INACTIVE/HOME chats) are excluded automatically.
- Inline buttons let dispatch mark a pass or add a comment directly in the compliance topic.
- The bot tracks rolling 24h compliance: two consecutive reports trigger a driver chat reminder (once per 24h), three trigger a dispatcher escalation ping.
- Drivers with 5+ passes in the current week receive an automatic congrats note (once per week).
- `/set_compliance_topic` and `/status` show whether the compliance thread is configured.

### Pause mode
If a driver chat title contains `INACTIVE`, `HOME`, or `HOME TIME` (case-insensitive), the bot pauses reminders for that chat. Manual `/notify`, autosend, and follow-up pings are skipped until the name no longer includes those tokens. Dispatcher commands and compliance reporting still work normally.

## Development
- Install dependencies with `pip install -r requirements.txt` (Python 3.11+).
- Run locally via `python -m app.main` (Postgres required).
- SQL schema lives in `migrations/001_init.sql`.
- Key modules:
  - `app/handlers/*` — command, review, media, and announcement routers
  - `app/services/*` — database logic, scheduling, streaks, digests
  - `app/main.py` — bootstrap, router wiring, scheduler startup
- `dashboard/` — FastAPI dashboard. Install dependencies with `pip install -r dashboard/requirements.txt` and run tests via `python3 -m pytest`.



### Driver group alerts
- Run `/notify` inside a driver group to post the Daily Check reminder there.
- Mention or reply to the driver once so the bot links that chat; afterwards `/notify` can run without args.
- The bot still mirrors results in the dispatcher topic and uses the linked chat for autosend reminders.
- `/trailer TRAILER_ID ACTION` and an optional second line for location (e.g., `/trailer HKK214 DROP\nOrlando FL`) send the driver prompt in-place and relay a bold alert in the configured trailer topic.
