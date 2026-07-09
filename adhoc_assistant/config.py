import json
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

from .constants import VALID_WEEKDAYS


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


def parse_month(config: dict) -> tuple[int, int]:
    raw_year = config.get("year")
    raw_month = config.get("month")

    if isinstance(raw_month, str) and "-" in raw_month:
        year_part, month_part = raw_month.split("-", maxsplit=1)
        return int(year_part), int(month_part)

    if raw_year is None or raw_month is None:
        raise ValueError("Config must include 'year' and 'month', or month = 'YYYY-MM'.")

    return int(raw_year), int(raw_month)


def parse_month_date(raw_value: Any, year: int, month: int, field_name: str) -> date:
    if isinstance(raw_value, int):
        return date(year, month, raw_value)

    if isinstance(raw_value, str):
        if raw_value.isdigit():
            return date(year, month, int(raw_value))
        return date.fromisoformat(raw_value)

    raise ValueError(f"Invalid {field_name}: {raw_value!r}")


def normalize_person(person: dict, year: int, month: int) -> dict:
    unavailable_dates = []

    for raw_date in person.get("unavailable_dates", []):
        unavailable_dates.append(
            parse_month_date(raw_date, year, month, "unavailable_dates").isoformat()
        )

    for raw_day in person.get("unavailable_days", []):
        unavailable_dates.append(
            parse_month_date(raw_day, year, month, "unavailable_days").isoformat()
        )

    return {
        "name": person.get("name"),
        "role": person.get("role", ""),
        "unavailable_dates": sorted(set(unavailable_dates)),
        "unavailable_weekdays": [
            weekday.lower() for weekday in person.get("unavailable_weekdays", [])
        ],
    }


def normalize_holidays(raw_holidays: list[Any], year: int, month: int) -> dict[str, str]:
    holidays = {}
    for item in raw_holidays:
        holiday_name = "Holiday"
        raw_date = item

        if isinstance(item, dict):
            holiday_name = item.get("name") or holiday_name
            raw_date = item.get("date", item.get("day"))

        holiday_date = parse_month_date(raw_date, year, month, "holidays").isoformat()
        holidays[holiday_date] = holiday_name

    return holidays


def normalize_config(config: dict) -> dict:
    year, month = parse_month(config)
    people = [normalize_person(person, year, month) for person in config.get("people", [])]

    normalized = {
        "year": year,
        "month": month,
        "people": people,
        "holidays": normalize_holidays(config.get("holidays", []), year, month),
        "history": normalize_count_history(config.get("history", {})),
        "output": config.get("output", {}),
    }
    validate_config(normalized)
    return normalized


def validate_config(config: dict) -> None:
    year = config["year"]
    month = config["month"]
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
                    f"Allowed values: {allowed}"
                )

        for raw_date in person.get("unavailable_dates", []):
            try:
                parsed_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid unavailable date '{raw_date}' for {name}. "
                    "Use YYYY-MM-DD or a day number in TOML."
                ) from exc

            if parsed_date.year != year or parsed_date.month != month:
                raise ValueError(
                    f"Unavailable date '{raw_date}' for {name} is outside "
                    f"{year}-{month:02d}."
                )

    for raw_date in config.get("holidays", {}):
        holiday_date = date.fromisoformat(raw_date)
        if holiday_date.year != year or holiday_date.month != month:
            raise ValueError(
                f"Holiday date '{raw_date}' is outside {year}-{month:02d}."
            )


def load_config(path: Path) -> dict:
    if path.suffix.lower() == ".toml":
        with path.open("rb") as f:
            raw_config = tomllib.load(f)
    else:
        with path.open("r", encoding="utf-8") as f:
            raw_config = json.load(f)

    return normalize_config(raw_config)
