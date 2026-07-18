# Adhoc Assistant

A small tool for building a monthly bug day / adhoc support schedule.

## Run

```bash
uv run python main.py
```

Save the generated month into SQLite history:

```bash
uv run python main.py --save-history
```

Use a different config file:

```bash
uv run python main.py --config adhoc_config.toml
```

Every run prints a compact calendar to the terminal and also writes a calendar HTML file.
It also writes a simple SVG calendar image. The default paths are
`adhoc_schedule_YYYY_MM.html` and `adhoc_schedule_YYYY_MM.jpg`; override them with:

```bash
uv run python main.py --html output.html --image output.jpg
```

## Monthly Config

The default config file is `adhoc_config.toml`. Fill in `year`/`month` or a
custom date range, output paths, and holidays. Team members live in SQLite.

Dates default to the Jalali calendar. Configure the date system in the `date`
section:

```toml
[date]
calendar = "jalali" # or "gregorian"

year = 1405
month = 5
```

With `calendar = "jalali"`, output dates are Jalali and weekday names are Persian.
With `calendar = "gregorian"`, output dates are Gregorian and weekday names are
English.

For Jalali configs, `unavailable_weekdays` can use Persian weekday names:

```toml
unavailable_weekdays = ["یکشنبه", "پنجشنبه"]
```

Holidays are not removed from the schedule. They are shown in the output. Only
Fridays are skipped.

Unavailable days can be written as day numbers:

```toml
unavailable_days = [3, 10]
```

Or as full dates:

```toml
unavailable_dates = ["1405-05-03"]
```

## SQLite History

The default database path is `adhoc_history.sqlite3` in the project root. With `--save-history`, the
monthly summary and daily schedule are saved. Future runs automatically include
saved history in fairness scoring.

Ignore SQLite history for one run:

```bash
uv run python main.py --no-db-history
```

## Docker

Build and run the Telegram bot (domain settings come from the DB, not TOML):

```bash
cp .env.example .env
touch adhoc_history.sqlite3
mkdir -p data/output
LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)" docker compose up -d --build adhoc-assistant-bot
```

For temporary timing diagnostics, set `ADHOC_LOG_LEVEL=DEBUG` in `.env` and
restart the bot. This logs Telegram `getUpdates`, per-update handling time, and
Telegram API request durations.

Compose mounts the project-root SQLite file `./adhoc_history.sqlite3` and
`./data` for generated images and related artifacts. The bot
reads token, timezone, survey policy, holidays, and destination from DB
`runtime_settings` / `telegram_destination`.

Compose services run as your host uid/gid (`LOCAL_UID` / `LOCAL_GID`) so files
written into mounted `data/` stay writable on the host. Fix an
old root-owned output dir once with:

```bash
sudo chown -R "$(id -u):$(id -g)" data
sudo chown "$(id -u):$(id -g)" adhoc_history.sqlite3
```

Initialize a fresh DB once (example):

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --set-telegram-token "$TELEGRAM_BOT_TOKEN" \
  --set-bot-name adhoc_assistant \
  --set-bot-username "@adhoc_assistant_bot" \
  --set-bot-id 0 \
  --set-timezone Asia/Tehran \
  --set-calendar jalali \
  --set-survey-days-before-month 2 \
  --set-survey-start-at "" \
  --set-survey-collect-for "+4d" \
  --set-revision-collect-for "+2h" \
  --set-daily-reminder-time 09:00 \
  --set-poll-interval-seconds 10 \
  --set-output-dir data/output \
  --set-holidays '[]' \
  --set-telegram-group-chat-id -100123456789 \
  --set-telegram-topic-id 3
```

Old one-shot schedule generator (still uses `adhoc_config.toml`):

```bash
docker compose --profile cli run --rm adhoc-assistant-cli
```

## Telegram Bot

### Source of truth

| Source | What belongs here |
|---|---|
| `DATABASE_URL` / `--database` | Infra only: SQLite path (project-root file by default; postgres DATABASE_URL is not supported yet) |
| SQLite `bot_state.runtime_settings` | Bot name/username/id, Telegram token, timezone, calendar, survey policy, reminder time, poll interval, output dir, holidays, optional target month |
| SQLite `bot_state.telegram_destination` | Group chat id + topic id |
| SQLite `bot_users` | Members/admins, schedule participation |
| SQLite surveys / schedule_entries / daily_reminders | Active survey lifecycle and reminders |
| `adhoc_config.toml` | **Only** the legacy `main.py` schedule CLI — not used by the Telegram bot |

There is no `bot_config.toml`. Missing or incomplete `runtime_settings` makes the bot fail with a clear CLI hint.

### Users and admins

```bash
uv run python -m adhoc_assistant.telegram_bot \
  --upsert-user some_admin_username \
  --user-display-name "Some Admin" \
  --user-role backend \
  --user-access-level admin \
  --user-manager-only

uv run python -m adhoc_assistant.telegram_bot \
  --upsert-user admin_participant_username \
  --user-display-name "Admin Participant" \
  --user-role backend \
  --user-access-level admin \
  --user-schedule-participant

uv run python -m adhoc_assistant.telegram_bot \
  --upsert-user teammate_username \
  --user-display-name "Teammate Name" \
  --user-role frontend \
  --user-access-level member

uv run python -m adhoc_assistant.telegram_bot --deactivate-user teammate_username
uv run python -m adhoc_assistant.telegram_bot --delete-user teammate_username
uv run python -m adhoc_assistant.telegram_bot --list-users
uv run python -m adhoc_assistant.telegram_bot --show-runtime-settings
```

### Production vs debug surveys

Same pipeline for both kinds (`production` | `debug`): forms, callbacks, stale handling, preview, approve/post, revision, cancel/restart.

Differences:

- Debug is created manually any time (`--survey-kind debug`) with optional `--participants` (DB users and/or local-only fake names).
- Local-only participants are scheduled, never messaged, and do not block collection.
- At most one non-terminal survey per kind; kinds do not overwrite each other.
- Creating while a survey of that kind is active requires an explicit replace
  (`--replace-active-survey` / `replace_active=True`), which cancels the current
  non-terminal survey(s) of that kind and starts the new one.
- Only production mirrors lifecycle status into `bot_monthly_runs` (derived mirror; not a decision source).
- Member responses live only in `survey_responses` (no dual-write to `availability_responses`).
- Debug publish posts/approves its own survey and keeps `schedule_json` on the survey row, but does **not** overwrite live `schedule_entries` / `monthly_stats` used by daily reminders.
- Production publish writes `schedule_entries` for that month, but switches
  `active_schedule_source` (live reminders) only when that month is already the
  current calendar month. Future months cut over via `maybe_activate_due_live_schedule`
  on month start, or explicitly with `--activate-survey-id`.
- Debug publish to the configured production Telegram destination requires `--allow-production-destination`.
- That flag is stored in DB `runtime_settings.allow_production_destination` (clear with `--clear-allow-production-destination`), so approve/publish behavior is stable across process restarts.
- If you want a debug or manually-reviewed survey to become the live reminder source, do it explicitly with `--activate-survey-id`.
- If a publish timed out ambiguously but you verified the Telegram group post really landed, finalize it explicitly with `--finalize-publishing-survey-id`. If the post did **not** land, cancel/restart the survey instead of blindly retrying publish.
- `/start` prefers production when a user is in both; debug remains on its own message buttons.
- Direct schedule without waiting for responses (defaults to debug unless
  `--survey-kind production` is set explicitly):

```bash
uv run python -m adhoc_assistant.telegram_bot \
  --participants "ali_user,sara_user,Local Tester" \
  --target-month 1405-05 \
  --direct-preview

# Same path, also post to the group (explicit opt-in required):
uv run python -m adhoc_assistant.telegram_bot \
  --participants "ali_user,sara_user,Local Tester" \
  --target-month 1405-05 \
  --publish-now \
  --allow-production-destination

# Explicit production direct preview:
uv run python -m adhoc_assistant.telegram_bot \
  --survey-kind production \
  --participants "ali_user,sara_user" \
  --target-month 1405-05 \
  --direct-preview \
  --replace-active-survey
```

Coverage gaps are warnings only; Approve / `--publish-now` still work.

Do **not** run production and extra/debug bot processes with the same Telegram bot token at the same time — Telegram `getUpdates` long-polling will conflict. Use a separate token (or never run both concurrently).

### Useful operations

```bash
uv run python -m adhoc_assistant.telegram_bot --create-survey --survey-kind debug --send-now
uv run python -m adhoc_assistant.telegram_bot --force-preview --survey-id S-jalali-1405-05-a1b2c3
uv run python -m adhoc_assistant.telegram_bot --cancel-survey-id S-jalali-1405-05-a1b2c3
uv run python -m adhoc_assistant.telegram_bot --restart-survey-id S-jalali-1405-05-a1b2c3
uv run python -m adhoc_assistant.telegram_bot --activate-survey-id S-jalali-1405-05-a1b2c3
uv run python -m adhoc_assistant.telegram_bot --set-daily-reminder-time 10:30
uv run python -m adhoc_assistant.telegram_bot --clear-daily-reminder-date 2026-08-01
uv run python -m adhoc_assistant.telegram_bot --force-daily-reminder
```

Daily reminders always re-read `schedule_entries` and `bot_users` at send time.

Local bot process:

```bash
uv run python -m adhoc_assistant.telegram_bot
```

For local runs outside Docker, pick a writable output dir explicitly:

```bash
mkdir -p data/output
uv run python -m adhoc_assistant.telegram_bot --set-output-dir data/output
```

## Code Structure

`main.py` is only the entrypoint. The main code lives in `adhoc_assistant`:

- `config.py`: read and normalize TOML/JSON config
- `scheduler.py`: scheduling logic and fairness scoring
- `exporters.py`: Markdown, CSV, and HTML output
- `storage.py`: SQLite history storage
- `cli.py`: command-line wiring
- `telegram_bot/`: Telegram polling service, inline keyboards, and bot storage
