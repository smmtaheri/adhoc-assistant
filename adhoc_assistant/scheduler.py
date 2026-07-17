from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .calendars import display_day, format_date, format_weekday, month_days
from .config import add_history
from .constants import SCORE_WEIGHTS, WEEKDAY_NAMES


def empty_person_stats() -> dict[str, Any]:
    return {
        "main_count": 0,
        "backup_count": 0,
        "total_count": 0,
        "thursday_count": 0,
        "weekday_count": defaultdict(int),
    }


def month_workdays(year: int, month: int, calendar_type: str) -> list[date]:
    """Return all days in the month except Fridays."""
    days = []
    for current in month_days(year, month, calendar_type):
        # Python: Monday=0 ... Friday=4 ... Sunday=6
        if current.weekday() == 4:
            continue

        days.append(current)

    return days


def range_workdays(start_date: date, end_date: date) -> list[date]:
    """Return all days in an inclusive custom range except Fridays."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date.")

    days = []
    current = start_date
    while current <= end_date:
        if current.weekday() != 4:
            days.append(current)
        current += timedelta(days=1)
    return days


def is_available(person: dict, current_day: date) -> bool:
    date_str = current_day.isoformat()
    weekday = WEEKDAY_NAMES[current_day.weekday()].lower()

    unavailable_dates = set(person.get("unavailable_dates", []))
    unavailable_weekdays = set(
        day.lower() for day in person.get("unavailable_weekdays", [])
    )

    if date_str in unavailable_dates:
        return False

    if weekday in unavailable_weekdays:
        return False

    return True


def score_candidate(
    person: dict,
    current_day: date,
    role: str,
    stats: dict,
    history: dict,
    previous_main: str | None,
) -> int:
    """Lower score means a fairer candidate."""
    name = person["name"]
    weekday = WEEKDAY_NAMES[current_day.weekday()]
    person_stats = stats[name]
    person_history = history.get(name, {})

    score = 0

    if role == "main":
        score += person_stats["main_count"] * SCORE_WEIGHTS["current_main"]
        score += person_history.get("main_count", 0) * SCORE_WEIGHTS["history_main"]

    if role == "backup":
        score += person_stats["backup_count"] * SCORE_WEIGHTS["current_backup"]
        score += person_history.get("backup_count", 0) * SCORE_WEIGHTS["history_backup"]

    score += person_stats["total_count"] * SCORE_WEIGHTS["current_total"]
    score += person_history.get("total_count", 0) * SCORE_WEIGHTS["history_total"]

    if weekday == "Thursday":
        score += person_stats["thursday_count"] * SCORE_WEIGHTS["current_thursday"]
        score += person_history.get("thursday_count", 0) * SCORE_WEIGHTS["history_thursday"]

    if role == "main" and previous_main == name:
        score += SCORE_WEIGHTS["consecutive_main"]

    score += person_stats["weekday_count"][weekday] * SCORE_WEIGHTS["same_weekday"]

    return score


def choose_person(
    people: list[dict],
    current_day: date,
    role: str,
    stats: dict,
    history: dict,
    previous_main: str | None = None,
    exclude_names: set[str] | None = None,
) -> dict | None:
    exclude_names = exclude_names or set()

    candidates = [
        person
        for person in people
        if person["name"] not in exclude_names and is_available(person, current_day)
    ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda person: (
            score_candidate(
                person=person,
                current_day=current_day,
                role=role,
                stats=stats,
                history=history,
                previous_main=previous_main,
            ),
            person["name"],
        )
    )

    return candidates[0]


def build_schedule(config: dict, db_history: dict | None = None) -> tuple[list[dict], dict]:
    calendar_type = config["calendar"]
    year = config["year"]
    month = config["month"]
    people = config["people"]
    history = add_history(db_history or {}, config.get("history", {}))
    holidays = config.get("holidays", {})

    if config.get("start_date") and config.get("end_date"):
        days = range_workdays(config["start_date"], config["end_date"])
    else:
        days = month_workdays(year, month, calendar_type)

    stats = defaultdict(empty_person_stats)
    for person in people:
        stats[person["name"]]

    schedule = []
    previous_main = None

    for current_day in days:
        weekday = WEEKDAY_NAMES[current_day.weekday()]
        gregorian_date = current_day.isoformat()
        display_date = format_date(current_day, calendar_type)
        display_weekday = format_weekday(current_day, calendar_type)
        holiday_name = holidays.get(gregorian_date, "")

        main = choose_person(
            people=people,
            current_day=current_day,
            role="main",
            stats=stats,
            history=history,
            previous_main=previous_main,
        )

        if main is None:
            schedule.append(
                {
                    "gregorian_date": gregorian_date,
                    "date": display_date,
                    "day": display_day(current_day, calendar_type),
                    "weekday": display_weekday,
                    "holiday": holiday_name,
                    "main": "NO_AVAILABLE_PERSON",
                    "backup": "NO_AVAILABLE_PERSON",
                }
            )
            continue

        backup = choose_person(
            people=people,
            current_day=current_day,
            role="backup",
            stats=stats,
            history=history,
            previous_main=previous_main,
            exclude_names={main["name"]},
        )

        backup_name = "NO_AVAILABLE_BACKUP" if backup is None else backup["name"]
        main_name = main["name"]

        schedule.append(
            {
                "gregorian_date": gregorian_date,
                "date": display_date,
                "day": display_day(current_day, calendar_type),
                "weekday": display_weekday,
                "holiday": holiday_name,
                "main": main_name,
                "backup": backup_name,
            }
        )

        stats[main_name]["main_count"] += 1
        stats[main_name]["total_count"] += 1
        stats[main_name]["weekday_count"][weekday] += 1

        if weekday == "Thursday":
            stats[main_name]["thursday_count"] += 1

        if backup is not None:
            stats[backup_name]["backup_count"] += 1
            stats[backup_name]["total_count"] += 1
            stats[backup_name]["weekday_count"][weekday] += 1

            if weekday == "Thursday":
                stats[backup_name]["thursday_count"] += 1

        previous_main = main_name

    return schedule, stats


def stats_to_plain_dict(stats: dict) -> dict[str, dict[str, int]]:
    return {
        name: {
            "main_count": stat["main_count"],
            "backup_count": stat["backup_count"],
            "total_count": stat["total_count"],
            "thursday_count": stat["thursday_count"],
        }
        for name, stat in stats.items()
    }
