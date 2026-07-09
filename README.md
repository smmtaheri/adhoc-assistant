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
`adhoc_schedule_YYYY_MM.html` and `adhoc_schedule_YYYY_MM.svg`; override them with:

```bash
uv run python main.py --html output.html --image output.svg
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

Build and run with Docker Compose:

```bash
docker compose up --build
```

The Compose setup stores SQLite data in the `adhoc_sqlite_data` volume and writes
CSV/HTML/SVG outputs into `./output`.

## Code Structure

`main.py` is only the entrypoint. The main code lives in `adhoc_assistant`:

- `config.py`: read and normalize TOML/JSON config
- `scheduler.py`: scheduling logic and fairness scoring
- `exporters.py`: Markdown, CSV, and HTML output
- `storage.py`: SQLite history storage
- `cli.py`: command-line wiring
