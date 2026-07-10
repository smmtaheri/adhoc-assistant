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

The default config file is `adhoc_config.toml`. For each month, fill in `year`,
`month`, people, unavailable days, and holidays.

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

Run an interactive debug Telegram flow:

```bash
docker compose --profile debug run --rm adhoc-assistant-debug
```

Run a timed Telegram test using `ADHOC_SURVEY_START_AT` from `.env`:

```bash
docker compose --profile timed run --rm adhoc-assistant-timed
```

## Telegram Bot

Configuration map:

| File | What belongs here |
|---|---|
| `.env` | Secrets only, currently `TELEGRAM_BOT_TOKEN` |
| `bot_config.toml` | Bot runtime settings: timezone, calendar, admin IDs, group/topic IDs, reminder time, paths |
| `members.toml` | Real team roster: display name, Telegram ID, username, role, active/inactive |
| `debug_members.toml` | Debug-only fake/local members, loaded only with `--debug` |
| `adhoc_config.toml` | Manual CLI schedule config and shared schedule policy/holidays |
| `docker-compose.yml` | Runtime wiring only: mounts, volumes, and command |

Fill `bot_config.toml` with bot metadata, admin IDs, the target group chat ID,
and the topic ID. Fill `members.toml` with each teammate's display name,
Telegram ID, username, role, and `active` status. Keep the real bot token only in
`.env`.

Access is config-based. Active members in `members.toml` can use the bot for
their own availability flow and status queries. IDs in `telegram.admin_ids` are
admins and can approve/post schedules. Any other Telegram user is rejected before
entering the flow.

The bot asks active members for availability two days before the next month. Users
can only answer with inline buttons: fully available, recurring unavailable
weekdays, specific unavailable dates, and confirm. Missing replies are treated as
fully available when the month starts. The bot sends the generated image to admins
first; after approval it posts the image to the configured group topic, saves the
month into SQLite, and sends the daily 09:00 reminder in that topic.

Authorized users can send `/today` to see today's bug day person and helper with
their Telegram IDs. This works after a schedule has been approved and saved.

For a timed test, set `ADHOC_SURVEY_START_AT`, `ADHOC_SURVEY_COLLECT_FOR`, and
`ADHOC_TARGET_MONTH` in `.env`. `ADHOC_SURVEY_START_AT` controls when the bot
starts messaging members. `ADHOC_SURVEY_COLLECT_FOR` controls how long it waits
before building the preview with missing replies treated as fully available.
Both accept values like `+2m`, `+2h`, `+2d`, or an exact timestamp such as
`2026-07-10 14:30`.

For an interactive debug run, enable debug mode:

```toml
[debug]
enabled = true
auto_preview_on_confirm = true
```

With debug enabled, `/start` immediately opens the availability form for active
members. If the sender is an admin, `/start` resets that target month first so
the test can be repeated from a clean form. Pressing `Confirm` creates the admin
preview right away, so the whole approve/post flow can be tested without waiting
for the monthly trigger or for every real member to confirm.

In debug mode, members from `debug_members.toml` are added to the real roster.
Members with `telegram_id = 0` are local/config-only members. They do not receive
Telegram messages and do not block collection, but their configured
`unavailable_days` and `unavailable_weekdays` are included in the generated
schedule. Normal mode ignores `debug_members.toml`.

The same test settings can be passed without editing the file when running
locally:

```bash
uv run python -m adhoc_assistant.telegram_bot \
  --config bot_config.toml \
  --debug \
  --survey-start-at "+2m" \
  --target-month 1405-05 \
  --database data/debug_adhoc.sqlite3 \
  --output-dir data/debug_output
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
