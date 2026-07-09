WEEKDAY_NAMES = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}

PERSIAN_WEEKDAY_NAMES = {
    0: "دوشنبه",
    1: "سه‌شنبه",
    2: "چهارشنبه",
    3: "پنجشنبه",
    4: "جمعه",
    5: "شنبه",
    6: "یکشنبه",
}

PERSIAN_WEEKDAY_ALIASES = {
    "دوشنبه": "monday",
    "سه‌شنبه": "tuesday",
    "سه شنبه": "tuesday",
    "چهارشنبه": "wednesday",
    "پنجشنبه": "thursday",
    "پنج‌شنبه": "thursday",
    "پنج شنبه": "thursday",
    "جمعه": "friday",
    "شنبه": "saturday",
    "یکشنبه": "sunday",
    "یک‌شنبه": "sunday",
    "یک شنبه": "sunday",
}

VALID_WEEKDAYS = {name.lower() for name in WEEKDAY_NAMES.values()}

SCORE_WEIGHTS = {
    "current_main": 20,
    "history_main": 5,
    "current_backup": 10,
    "history_backup": 3,
    "current_total": 5,
    "history_total": 2,
    "current_thursday": 30,
    "history_thursday": 10,
    "consecutive_main": 100,
    "same_weekday": 4,
}

DEFAULT_CONFIG_PATH = "adhoc_config.toml"
DEFAULT_DB_PATH = "adhoc_history.sqlite3"

HTML_CALENDAR_WEEKDAYS = [5, 6, 0, 1, 2, 3]
HTML_WEEKDAY_COLUMNS = {
    weekday: index for index, weekday in enumerate(HTML_CALENDAR_WEEKDAYS)
}
