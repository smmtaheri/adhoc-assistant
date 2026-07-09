from datetime import date

from .constants import PERSIAN_WEEKDAY_NAMES, WEEKDAY_NAMES

SUPPORTED_CALENDARS = {"jalali", "gregorian"}


def normalize_calendar_type(raw_value: str | None) -> str:
    calendar_type = (raw_value or "jalali").strip().lower()
    if calendar_type in {"shamsi", "persian"}:
        return "jalali"
    if calendar_type in {"gregorian", "miladi"}:
        return "gregorian"
    if calendar_type not in SUPPORTED_CALENDARS:
        allowed = ", ".join(sorted(SUPPORTED_CALENDARS))
        raise ValueError(f"Unsupported calendar '{raw_value}'. Allowed values: {allowed}")
    return calendar_type


def is_gregorian_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def jalali_to_gregorian(year: int, month: int, day: int) -> date:
    jy = year + 1595
    days = (
        -355668
        + 365 * jy
        + (jy // 33) * 8
        + ((jy % 33 + 3) // 4)
        + day
    )

    if month < 7:
        days += (month - 1) * 31
    else:
        days += (month - 7) * 30 + 186

    gy = 400 * (days // 146097)
    days %= 146097

    if days > 36524:
        gy += 100 * ((days - 1) // 36524)
        days = (days - 1) % 36524
        if days >= 365:
            days += 1

    gy += 4 * (days // 1461)
    days %= 1461

    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365

    gd = days + 1
    month_lengths = [
        0,
        31,
        29 if is_gregorian_leap(gy) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    gm = 1
    while gm <= 12 and gd > month_lengths[gm]:
        gd -= month_lengths[gm]
        gm += 1

    return date(gy, gm, gd)


def gregorian_to_jalali(current_date: date) -> tuple[int, int, int]:
    gy = current_date.year
    gm = current_date.month
    gd = current_date.day
    g_day_offsets = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]

    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621

    gy2 = gy + 1 if gm > 2 else gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_day_offsets[gm - 1]
    )

    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461

    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365

    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30

    return jy, jm, jd


def is_jalali_leap(year: int) -> bool:
    current_start = jalali_to_gregorian(year, 1, 1)
    next_start = jalali_to_gregorian(year + 1, 1, 1)
    return (next_start - current_start).days == 366


def jalali_month_length(year: int, month: int) -> int:
    if month < 1 or month > 12:
        raise ValueError("Jalali month must be between 1 and 12.")
    if month <= 6:
        return 31
    if month <= 11:
        return 30
    return 30 if is_jalali_leap(year) else 29


def parse_calendar_date(
    raw_value: str | int,
    year: int,
    month: int,
    calendar_type: str,
) -> date:
    if isinstance(raw_value, int):
        day = raw_value
        parsed_year = year
        parsed_month = month
    elif isinstance(raw_value, str):
        if raw_value.isdigit():
            day = int(raw_value)
            parsed_year = year
            parsed_month = month
        else:
            parsed_year, parsed_month, day = (
                int(part) for part in raw_value.split("-", maxsplit=2)
            )
    else:
        raise ValueError(f"Invalid date value: {raw_value!r}")

    if calendar_type == "gregorian":
        return date(parsed_year, parsed_month, day)

    month_length = jalali_month_length(parsed_year, parsed_month)
    if day < 1 or day > month_length:
        raise ValueError(
            f"Invalid Jalali date {parsed_year:04d}-{parsed_month:02d}-{day:02d}."
        )
    return jalali_to_gregorian(parsed_year, parsed_month, day)


def month_days(year: int, month: int, calendar_type: str) -> list[date]:
    if calendar_type == "gregorian":
        import calendar

        _, last_day = calendar.monthrange(year, month)
        return [date(year, month, day) for day in range(1, last_day + 1)]

    last_day = jalali_month_length(year, month)
    return [jalali_to_gregorian(year, month, day) for day in range(1, last_day + 1)]


def date_in_month(
    current_date: date,
    year: int,
    month: int,
    calendar_type: str,
) -> bool:
    if calendar_type == "gregorian":
        return current_date.year == year and current_date.month == month

    jalali_year, jalali_month, _ = gregorian_to_jalali(current_date)
    return jalali_year == year and jalali_month == month


def format_date(current_date: date, calendar_type: str) -> str:
    if calendar_type == "gregorian":
        return current_date.isoformat()

    year, month, day = gregorian_to_jalali(current_date)
    return f"{year:04d}-{month:02d}-{day:02d}"


def format_weekday(current_date: date, calendar_type: str) -> str:
    if calendar_type == "gregorian":
        return WEEKDAY_NAMES[current_date.weekday()]
    return PERSIAN_WEEKDAY_NAMES[current_date.weekday()]


def display_day(current_date: date, calendar_type: str) -> int:
    if calendar_type == "gregorian":
        return current_date.day
    return gregorian_to_jalali(current_date)[2]
