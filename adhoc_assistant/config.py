import json
import tomllib
from pathlib import Path
from typing import Any

from .calendars import (
    date_in_month,
    gregorian_to_jalali,
    normalize_calendar_type,
    parse_calendar_date,
)
from .constants import PERSIAN_WEEKDAY_ALIASES, VALID_WEEKDAYS


def normalize_count_history(history: dict | None) -> dict[str, dict[str, int]]:
    normalized = {}
    for name, counts in (history or {}).items():
        normalized[name] = {
            "main_count": int(counts.get("main_count", 0)),
            "backup_count": int(counts.get("backup_count", 0)),
            "total_count": int(counts.get("total_count", 0)),
            "thursday_count": int(counts.get("thursday_count", 0)),
        }
    return normalized


def add_history(
    base: dict[str, dict[str, int]],
    extra: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    merged = normalize_count_history(base)
    for name, counts in normalize_count_history(extra).items():
        person = merged.setdefault(
            name,
            {
                "main_count": 0,
                "backup_count": 0,
                "total_count": 0,
                "thursday_count": 0,
            },
        )
        for key, value in counts.items():
            person[key] += value
    return merged


def parse_calendar(config: dict) -> str:
    raw_calendar = config.get("calendar")
    if isinstance(raw_calendar, dict):
        raw_calendar = raw_calendar.get("type")

    raw_date_config = config.get("date")
    if isinstance(raw_date_config, dict):
        raw_calendar = raw_date_config.get("calendar", raw_calendar)

    return normalize_calendar_type(raw_calendar)


def parse_month(config: dict) -> tuple[int, int]:
    raw_year = config.get("year")
    raw_month = config.get("month")
    raw_date_config = config.get("date")

    if isinstance(raw_date_config, dict):
        raw_year = raw_date_config.get("year", raw_year)
        raw_month = raw_date_config.get("month", raw_month)

    if isinstance(raw_month, str) and "-" in raw_month:
        year_part, month_part = raw_month.split("-", maxsplit=1)
        return int(year_part), int(month_part)

    if raw_year is None or raw_month is None:
        raise ValueError("Config must include 'year' and 'month', or month = 'YYYY-MM'.")

    return int(raw_year), int(raw_month)


def parse_period(config: dict, calendar_type: str) -> tuple[int, int, object | None, object | None]:
    raw_date_config = config.get("date")
    raw_start = config.get("start_date")
    raw_end = config.get("end_date")
    if isinstance(raw_date_config, dict):
        raw_start = raw_date_config.get("start_date", raw_date_config.get("start", raw_start))
        raw_end = raw_date_config.get("end_date", raw_date_config.get("end", raw_end))

    if raw_start or raw_end:
        if not raw_start or not raw_end:
            raise ValueError("Custom ranges must include both start_date and end_date.")
        fallback_year, fallback_month = parse_month_with_fallback(config)
        start_date = parse_calendar_date(raw_start, fallback_year, fallback_month, calendar_type)
        end_date = parse_calendar_date(raw_end, fallback_year, fallback_month, calendar_type)
        if end_date < start_date:
            raise ValueError("Custom range end_date must be on or after start_date.")
        if calendar_type == "gregorian":
            return start_date.year, start_date.month, start_date, end_date
        jalali_year, jalali_month, _ = gregorian_to_jalali(start_date)
        return jalali_year, jalali_month, start_date, end_date

    year, month = parse_month(config)
    return year, month, None, None


def parse_month_with_fallback(config: dict) -> tuple[int, int]:
    try:
        return parse_month(config)
    except ValueError:
        return 1, 1


def normalize_weekday(raw_day: str) -> str:
    weekday = raw_day.strip().lower()
    if weekday in VALID_WEEKDAYS:
        return weekday
    return PERSIAN_WEEKDAY_ALIASES.get(raw_day.strip(), weekday)


def normalize_person(person: dict, year: int, month: int, calendar_type: str) -> dict:
    unavailable_dates = []

    for raw_date in person.get("unavailable_dates", []):
        unavailable_dates.append(
            parse_calendar_date(raw_date, year, month, calendar_type).isoformat()
        )

    for raw_day in person.get("unavailable_days", []):
        unavailable_dates.append(
            parse_calendar_date(raw_day, year, month, calendar_type).isoformat()
        )

    return {
        "name": person.get("name"),
        "role": person.get("role", ""),
        "unavailable_dates": sorted(set(unavailable_dates)),
        "unavailable_weekdays": [
            normalize_weekday(weekday) for weekday in person.get("unavailable_weekdays", [])
        ],
    }


def normalize_holidays(
    raw_holidays: list[Any],
    year: int,
    month: int,
    calendar_type: str,
    start_date=None,
    end_date=None,
) -> dict[str, str]:
    holidays = {}
    for item in raw_holidays:
        holiday_name = "Holiday"
        raw_date = item

        if isinstance(item, dict):
            holiday_name = item.get("name") or holiday_name
            raw_date = item.get("date", item.get("day"))

        holiday_date = parse_calendar_date(raw_date, year, month, calendar_type).isoformat()
        if start_date is not None and end_date is not None:
            parsed = parse_calendar_date(raw_date, year, month, calendar_type)
            if not start_date <= parsed <= end_date:
                raise ValueError(f"Holiday date '{raw_date}' is outside the configured range.")
        holidays[holiday_date] = holiday_name

    return holidays


def normalize_config(config: dict) -> dict:
    calendar_type = parse_calendar(config)
    year, month, start_date, end_date = parse_period(config, calendar_type)
    people = [
        normalize_person(person, year, month, calendar_type)
        for person in config.get("people", [])
    ]

    normalized = {
        "calendar": calendar_type,
        "year": year,
        "month": month,
        "start_date": start_date,
        "end_date": end_date,
        "people": people,
        "holidays": normalize_holidays(
            config.get("holidays", []),
            year,
            month,
            calendar_type,
            start_date,
            end_date,
        ),
        "history": normalize_count_history(config.get("history", {})),
        "output": config.get("output", {}),
    }
    validate_config(normalized)
    return normalized


def validate_config(config: dict) -> None:
    year = config["year"]
    month = config["month"]
    calendar_type = config["calendar"]
    people = config["people"]
    start_date = config.get("start_date")
    end_date = config.get("end_date")

    if month < 1 or month > 12:
        raise ValueError("Config key 'month' must be between 1 and 12.")
    if (start_date is None) != (end_date is None):
        raise ValueError("Custom ranges must include both start_date and end_date.")

    if people and len(people) < 2:
        raise ValueError("Config must contain at least two people when people are configured.")

    seen_names = set()
    for index, person in enumerate(people, start=1):
        name = person.get("name")
        if not name:
            raise ValueError(f"Person #{index} is missing a non-empty 'name'.")

        if name in seen_names:
            raise ValueError(f"Duplicate person name in config: {name}")
        seen_names.add(name)

        for raw_day in person.get("unavailable_weekdays", []):
            weekday = raw_day.lower()
            if weekday not in VALID_WEEKDAYS:
                allowed = ", ".join(sorted(VALID_WEEKDAYS))
                raise ValueError(
                    f"Invalid unavailable weekday '{raw_day}' for {name}. "
                    f"Allowed English values: {allowed}"
                )

        for raw_date in person.get("unavailable_dates", []):
            try:
                parsed_date = parse_calendar_date(raw_date, year, month, calendar_type)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid unavailable date '{raw_date}' for {name}. "
                    "Use YYYY-MM-DD in the configured calendar, or a day number in TOML."
                ) from exc

            if start_date is not None and end_date is not None:
                if not start_date <= parsed_date <= end_date:
                    raise ValueError(
                        f"Unavailable date '{raw_date}' for {name} is outside the configured range."
                    )
            elif not date_in_month(parsed_date, year, month, calendar_type):
                raise ValueError(
                    f"Unavailable date '{raw_date}' for {name} is outside "
                    f"{year}-{month:02d} in {calendar_type} calendar."
                )

    for raw_date in config.get("holidays", {}):
        holiday_date = parse_calendar_date(raw_date, year, month, calendar_type)
        if start_date is not None and end_date is not None:
            if not start_date <= holiday_date <= end_date:
                raise ValueError(f"Holiday date '{raw_date}' is outside the configured range.")
        elif not date_in_month(holiday_date, year, month, calendar_type):
            raise ValueError(
                f"Holiday date '{raw_date}' is outside "
                f"{year}-{month:02d} in {calendar_type} calendar."
            )


def load_config(path: Path) -> dict:
    if path.suffix.lower() == ".toml":
        with path.open("rb") as f:
            raw_config = tomllib.load(f)
    else:
        with path.open("r", encoding="utf-8") as f:
            raw_config = json.load(f)

    return normalize_config(raw_config)
