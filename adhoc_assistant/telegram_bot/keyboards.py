from adhoc_assistant.calendars import display_day, month_days
from adhoc_assistant.constants import PERSIAN_WEEKDAY_NAMES, WEEKDAY_NAMES
from adhoc_assistant.exporters import visual_weekdays
from adhoc_assistant.telegram_bot.repository import AvailabilityResponse


def button(text: str, callback_data: str) -> dict:
    return {"text": text, "callback_data": callback_data}


def markup(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def weekday_names(calendar_type: str) -> dict[int, str]:
    if calendar_type == "jalali":
        return PERSIAN_WEEKDAY_NAMES
    return WEEKDAY_NAMES


def availability_summary(
    response: AvailabilityResponse,
    calendar_type: str,
    survey_label: str = "",
) -> str:
    weekdays = weekday_names(calendar_type)
    selected_weekdays = [
        weekdays[index]
        for index, english_name in WEEKDAY_NAMES.items()
        if english_name.lower() in response.unavailable_weekdays
    ]
    days = "، ".join(str(day) for day in sorted(response.unavailable_days)) or "none"
    recurring = "، ".join(selected_weekdays) or "none"
    status = "confirmed" if response.confirmed else "not confirmed"
    mode = "fully available" if response.mode == "full" else "custom availability"
    prefix = f"{survey_label}\n" if survey_label else ""
    return prefix + (
        f"Availability for {response.name}\n"
        f"Mode: {mode}\n"
        f"Recurring unavailable weekdays: {recurring}\n"
        f"Specific unavailable dates: {days}\n"
        f"Status: {status}"
    )


def callback(survey_id: str, action: str) -> str:
    return f"av:{survey_id}:{action}"


def main_availability_keyboard(
    response: AvailabilityResponse | None = None,
    survey_id: str = "",
) -> dict:
    if response and response.mode == "full":
        return markup(
            [
                [button("Change availability", callback(survey_id, "custom"))],
                [button("Confirm", callback(survey_id, "confirm"))],
            ]
        )

    return markup(
        [
            [button("I am fully available", callback(survey_id, "full"))],
            [button("Select recurring weekdays", callback(survey_id, "weekdays"))],
            [button("Select specific dates", callback(survey_id, "dates"))],
            [button("Confirm", callback(survey_id, "confirm"))],
        ]
    )


def weekdays_keyboard(
    response: AvailabilityResponse,
    calendar_type: str,
    survey_id: str = "",
) -> dict:
    names = weekday_names(calendar_type)
    rows = []
    row = []
    for weekday in visual_weekdays(calendar_type):
        english_name = WEEKDAY_NAMES[weekday].lower()
        selected = english_name in response.unavailable_weekdays
        prefix = "✅ " if selected else ""
        verb = "remove" if selected else "add"
        row.append(button(f"{prefix}{names[weekday]}", callback(survey_id, f"w:{verb}:{english_name}")))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            button("Back", callback(survey_id, "back")),
            button("Confirm", callback(survey_id, "confirm")),
        ]
    )
    return markup(rows)


def dates_keyboard(
    response: AvailabilityResponse,
    year: int,
    month: int,
    calendar_type: str,
    survey_id: str = "",
) -> dict:
    weekdays = visual_weekdays(calendar_type)
    weekday_columns = {weekday: index for index, weekday in enumerate(weekdays)}
    rows = []
    current_row = [button(" ", callback(survey_id, "noop")) for _ in weekdays]

    for current_date in month_days(year, month, calendar_type):
        if current_date.weekday() == 4:
            continue

        if current_date.weekday() == 5 and any(item["text"] != " " for item in current_row):
            rows.append(current_row)
            current_row = [button(" ", callback(survey_id, "noop")) for _ in weekdays]

        column = weekday_columns[current_date.weekday()]
        day = display_day(current_date, calendar_type)
        selected = day in response.unavailable_days
        prefix = "✅ " if selected else ""
        verb = "remove" if selected else "add"
        current_row[column] = button(f"{prefix}{day}", callback(survey_id, f"d:{verb}:{day}"))

    if any(item["text"] != " " for item in current_row):
        rows.append(current_row)

    rows.append(
        [
            button("Back", callback(survey_id, "back")),
            button("Confirm", callback(survey_id, "confirm")),
        ]
    )
    return markup(rows)


def admin_approval_keyboard(
    year: int,
    month: int,
    calendar_type: str,
    status: str,
    has_flagged_members: bool = True,
    survey_id: str = "",
) -> dict:
    key = survey_id or f"{calendar_type}:{year}:{month}"
    rows = []
    if status != "blocked":
        rows.append([button("Approve", f"admin:approve:{key}")])
    if has_flagged_members:
        rows.append([button("Request corrections", f"admin:correct:{key}")])
    rows.append([button("Reopen for everyone", f"admin:reopen:{key}")])
    rows.append(
        [
            button("Rebuild preview", f"admin:regen:{key}"),
        ]
    )
    rows.append([button("Cancel", f"admin:cancel:{key}")])
    return markup(rows)


def admin_revision_keyboard(
    year: int,
    month: int,
    calendar_type: str,
    survey_id: str = "",
) -> dict:
    key = survey_id or f"{calendar_type}:{year}:{month}"
    return markup(
        [
            [button("Close revision now", f"admin:close:{key}")],
            [button("Reopen for everyone", f"admin:reopen:{key}")],
            [button("Cancel", f"admin:cancel:{key}")],
        ]
    )


def admin_canceled_keyboard(
    year: int,
    month: int,
    calendar_type: str,
    survey_id: str = "",
) -> dict:
    key = survey_id or f"{calendar_type}:{year}:{month}"
    return markup([[button("Restart survey", f"admin:restart:{key}")]])
