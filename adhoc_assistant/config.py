import json
import tomllib
from pathlib import Path
from typing import Any

from .calendars import (
    date_in_month,
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
) -> dict[str, str]:
    holidays = {}
    for item in raw_holidays:
        holiday_name = "Holiday"
        raw_date = item

        if isinstance(item, dict):
            holiday_name = item.get("name") or holiday_name
            raw_date = item.get("date", item.get("day"))

        holiday_date = parse_calendar_date(raw_date, year, month, calendar_type).isoformat()
        holidays[holiday_date] = holiday_name

    return holidays


def normalize_config(config: dict) -> dict:
    calendar_type = parse_calendar(config)
    year, month = parse_month(config)
    people = [
        normalize_person(person, year, month, calendar_type)
        for person in config.get("people", [])
    ]

    normalized = {
        "calendar": calendar_type,
        "year": year,
        "month": month,
        "people": people,
        "holidays": normalize_holidays(
            config.get("holidays", []),
            year,
            month,
            calendar_type,
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

    if month < 1 or month > 12:
        raise ValueError("Config key 'month' must be between 1 and 12.")

    if not people or len(people) < 2:
        raise ValueError("Config must contain at least two people.")

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
                parsed_date = parse_calendar_date(raw_date, year, month, "gregorian")
            except ValueError as exc:
                raise ValueError(
                    f"Invalid unavailable date '{raw_date}' for {name}. "
                    "Use YYYY-MM-DD in the configured calendar, or a day number in TOML."
                ) from exc

            if not date_in_month(parsed_date, year, month, calendar_type):
                raise ValueError(
                    f"Unavailable date '{raw_date}' for {name} is outside "
                    f"{year}-{month:02d} in {calendar_type} calendar."
                )

    for raw_date in config.get("holidays", {}):
        holiday_date = parse_calendar_date(raw_date, year, month, "gregorian")
        if not date_in_month(holiday_date, year, month, calendar_type):
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
