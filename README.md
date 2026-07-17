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

The default database path is `adhoc_history.sqlite3`. With `--save-history`, the
monthly summary and daily schedule are saved. Future runs automatically include
saved history in fairness scoring.

Ignore SQLite history for one run:

```bash
uv run python main.py --no-db-history
```

## Docker

Build and run the Telegram bot service with Docker Compose:

```bash
cp .env.example .env
docker compose up -d --build adhoc-assistant-bot
```

The Compose setup stores SQLite data in the `adhoc_sqlite_data` volume and writes
calendar images into `/app/data/output` inside that volume. This keeps the bot
away from host-side `output/` permission issues.

Run the one-shot CLI through Compose:

```bash
docker compose --profile cli run --rm adhoc-assistant-cli
```

Run an interactive debug Telegram flow. Its defaults live in
`bot_config.debug.toml`:

```bash
docker compose --profile debug run --rm adhoc-assistant-debug
```

Run a timed Telegram test. Its defaults live in `bot_config.timed.toml`:

```bash
docker compose --profile timed run --rm adhoc-assistant-timed
```

## Telegram Bot

Configuration map:

| File | What belongs here |
|---|---|
| `.env` | Secrets only, currently `TELEGRAM_BOT_TOKEN` |
| `bot_config.toml` | Bot runtime settings: timezone, calendar, reminder cadence, paths |
| `bot_config.debug.toml` | Interactive debug defaults and isolated debug paths |
| `bot_config.timed.toml` | Timed test defaults: target month, start delay, collect window, isolated paths |
| SQLite DB | Allowed users, Telegram IDs, access levels, schedule participation, group/topic destination |
| `adhoc_config.toml` | Manual CLI schedule policy, date/month/range, output, and holidays |
| `docker-compose.yml` | Runtime wiring only: mounts, volumes, and command |

Fill `bot_config.toml` with bot metadata and runtime timing/path settings. Keep
the real bot token only in `.env`. Do not store real teammates, admins, or group
destinations in committed files.

Users are DB-backed. First add allowed usernames to SQLite. When a person sends
`/start`, the bot matches their Telegram username, stores their numeric Telegram
ID and Telegram display name, and lets them enter any open survey.

Add an admin who manages the flow but is not scheduled for bug-day duty:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --upsert-user some_admin_username \
  --user-display-name "Some Admin" \
  --user-role backend \
  --user-access-level admin \
  --user-manager-only
```

Add an admin who also participates in the schedule and must answer surveys:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --upsert-user admin_participant_username \
  --user-display-name "Admin Participant" \
  --user-role backend \
  --user-access-level admin \
  --user-schedule-participant
```

Add a normal member:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --upsert-user teammate_username \
  --user-display-name "Teammate Name" \
  --user-role frontend \
  --user-access-level member
```

List DB users:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --list-users
```

Access is DB-based. Active users can use `/today` and answer surveys. Admin users
can approve/post schedules. Only active users with `participates_in_schedule = 1`
are snapshotted into surveys and assigned bug-day duty. Any other Telegram user
is rejected before entering the flow.

The bot asks active members for availability two days before the next month. Users
can only answer with inline buttons: fully available, recurring unavailable
weekdays, specific unavailable dates, and confirm. Choosing fully available locks
the custom date controls until the user chooses `Change availability`.

Every availability cycle is stored as a survey with a stable ID such as
`S-jalali-1405-05-a1b2c3`. Member forms, callbacks, admin previews, responses,
and canonical Telegram message IDs all point to that survey ID. Old buttons from
an ended survey are rejected and their keyboard is removed instead of mutating the
current survey. `/start` resumes the active survey form for that person instead
of creating unlimited duplicate live forms.

When the collection window closes, the bot sends admins a preview with a review
report. Schedules with missing main/helper coverage are marked `blocked` and
cannot be approved. Imbalanced but covered schedules stay in admin review, where
admins can approve, request corrections from flagged members, reopen for
everyone, rebuild the preview from current data, or cancel the cycle. After a
revision starts, admins can close it immediately or reopen it for everyone. A
canceled cycle shows a restart button so the same month can be collected again.
After approval, the bot posts the image to the configured group topic, saves the
month into SQLite, and sends the daily 09:00 reminder in that topic. If Telegram
rejects pinning, the schedule is still posted and the admin gets the exact pin
error.

Authorized users can send `/today` to see today's bug day person and helper with
their Telegram IDs. This works after a schedule has been approved and saved.

For a timed test, edit `bot_config.timed.toml` instead of passing inline
environment variables. The default timed profile targets Jalali `1405-05`, starts
after `+2m`, waits `+10m` before building the first preview, and uses `+2h`
correction windows. These values accept forms like `+2m`, `+2h`, `+2d`, or an
exact timestamp such as `2026-07-10 14:30`.

Relative `survey_start_at` values are one-shot start delays. They do not repeat
forever and do not reset a collecting or reviewed survey.

For an interactive debug run, `bot_config.debug.toml` already enables debug
mode:

```toml
[debug]
enabled = true
auto_preview_on_confirm = true
```

With debug enabled, only admins can chat with the bot. If the admin is also an
active member, `/start` immediately opens their availability form and resets that
target month first so the test can be repeated from a clean form. Pressing
`Confirm` creates the admin preview right away, so the whole approve/post flow
can be tested without waiting for the monthly trigger.

For custom CLI ranges, set both dates in `adhoc_config.toml`:

```toml
[date]
calendar = "jalali"
start_date = "1405-05-10"
end_date = "1405-06-10"
```

If `people` is omitted from `adhoc_config.toml`, the CLI loads active schedule
participants from SQLite.

### Local admin operations

Admins can fully inspect and repair surveys from the local machine without adding
every operation as an inline Telegram button.

Set the DB-backed group/topic destination:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --set-telegram-group-chat-id -100123456789 \
  --set-telegram-topic-id 3
```

Show DB-backed runtime settings:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --show-runtime-settings
```

List surveys:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --list-surveys
```

Create a survey from the active DB roster:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --target-month 1405-05 \
  --survey-start-at "+2m" \
  --survey-collect-for "+4d" \
  --create-survey
```

Send an existing scheduled survey:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --send-survey-id S-jalali-1405-05-a1b2c3
```

Cancel or edit a survey deadline:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --cancel-survey-id S-jalali-1405-05-a1b2c3

docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --survey-id S-jalali-1405-05-a1b2c3 \
  --set-survey-closes-at "+2d"
```

Edit an approved daily schedule entry and reminder time:

```bash
docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --set-schedule-entry-date 2026-08-01 \
  --entry-main "New Main" \
  --entry-backup "New Backup"

docker compose run --rm adhoc-assistant-bot \
  python -m adhoc_assistant.telegram_bot \
  --config /app/bot_config.toml \
  --database /app/data/adhoc_history.sqlite3 \
  --set-daily-reminder-time 10:30
```

The daily reminder reads `schedule_entries` at send time, so editing that row
changes who gets mentioned on the next reminder.

The same profiles can run locally without Docker by pointing the bot at the
matching config file:

```bash
uv run python -m adhoc_assistant.telegram_bot --config bot_config.debug.toml

uv run python -m adhoc_assistant.telegram_bot \
  --config bot_config.timed.toml \
  --reset-target-month
```

For a local run without Docker:

```bash
uv run python -m adhoc_assistant.telegram_bot --config bot_config.toml
```

## Code Structure

`main.py` is only the entrypoint. The main code lives in `adhoc_assistant`:

- `config.py`: read and normalize TOML/JSON config
- `scheduler.py`: scheduling logic and fairness scoring
- `exporters.py`: Markdown, CSV, and HTML output
- `storage.py`: SQLite history storage
- `cli.py`: command-line wiring
- `telegram_bot/`: Telegram polling service, inline keyboards, and bot storage
