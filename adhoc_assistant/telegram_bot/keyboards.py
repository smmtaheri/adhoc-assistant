from adhoc_assistant.calendars import display_day, month_days
from adhoc_assistant.constants import PERSIAN_WEEKDAY_NAMES, WEEKDAY_NAMES
from adhoc_assistant.exporters import visual_weekdays
from adhoc_assistant.telegram_bot.messages import normalize_language
from adhoc_assistant.telegram_bot.repository import AvailabilityResponse


TEXTS = {
    "en": {
        "none": "none",
        "confirmed": "confirmed",
        "not_confirmed": "not confirmed",
        "fully_available": "fully available",
        "custom_availability": "custom availability",
        "availability_for": "Availability for {name}",
        "mode": "Mode: {mode}",
        "recurring_weekdays": "Recurring unavailable weekdays: {value}",
        "specific_dates": "Specific unavailable dates: {value}",
        "status": "Status: {status}",
        "change_availability": "Change availability",
        "confirm": "Confirm",
        "i_am_fully_available": "I am fully available",
        "select_weekdays": "Select recurring weekdays",
        "select_dates": "Select specific dates",
        "back": "Back",
        "approve": "Approve",
        "request_corrections": "Request corrections",
        "reopen_everyone": "Reopen for everyone",
        "rebuild_preview": "Rebuild preview",
        "cancel": "Cancel",
        "close_revision": "Close revision now",
        "restart_survey": "Restart survey",
        "start_survey": "Start",
    },
    "fa": {
        "none": "هیچ‌کدام",
        "confirmed": "تایید شده",
        "not_confirmed": "تایید نشده",
        "fully_available": "کاملا در دسترس",
        "custom_availability": "دسترسی سفارشی",
        "availability_for": "وضعیت دسترسی برای {name}",
        "mode": "حالت: {mode}",
        "recurring_weekdays": "روزهای تکرارشونده‌ی عدم دسترسی: {value}",
        "specific_dates": "تاریخ‌های خاص عدم دسترسی: {value}",
        "status": "وضعیت: {status}",
        "change_availability": "تغییر دسترسی",
        "confirm": "تایید",
        "i_am_fully_available": "کل ماه هستم",
        "select_weekdays": "انتخاب روزهای هفته",
        "select_dates": "انتخاب تاریخ‌های خاص",
        "back": "بازگشت",
        "approve": "تایید برنامه",
        "request_corrections": "درخواست اصلاح",
        "reopen_everyone": "بازکردن برای همه",
        "rebuild_preview": "ساخت دوباره پیش‌نمایش",
        "cancel": "لغو",
        "close_revision": "بستن اصلاحات",
        "restart_survey": "شروع دوباره نظرسنجی",
        "start_survey": "شروع",
    },
    "ar": {
        "none": "لا شيء",
        "confirmed": "تم التأكيد",
        "not_confirmed": "غير مؤكد",
        "fully_available": "متاح بالكامل",
        "custom_availability": "توافر مخصص",
        "availability_for": "التوافر لـ {name}",
        "mode": "الوضع: {mode}",
        "recurring_weekdays": "أيام الأسبوع غير المتاحة: {value}",
        "specific_dates": "التواريخ غير المتاحة: {value}",
        "status": "الحالة: {status}",
        "change_availability": "تعديل التوافر",
        "confirm": "تأكيد",
        "i_am_fully_available": "أنا متاح طوال الشهر",
        "select_weekdays": "اختيار أيام الأسبوع",
        "select_dates": "اختيار تواريخ محددة",
        "back": "رجوع",
        "approve": "اعتماد",
        "request_corrections": "طلب تعديلات",
        "reopen_everyone": "إعادة الفتح للجميع",
        "rebuild_preview": "إعادة بناء المعاينة",
        "cancel": "إلغاء",
        "close_revision": "إغلاق التعديل الآن",
        "restart_survey": "إعادة بدء الاستبيان",
        "start_survey": "بدء",
    },
    "ru": {
        "none": "нет",
        "confirmed": "подтверждено",
        "not_confirmed": "не подтверждено",
        "fully_available": "полностью доступен",
        "custom_availability": "выборочная доступность",
        "availability_for": "Доступность для {name}",
        "mode": "Режим: {mode}",
        "recurring_weekdays": "Недоступные дни недели: {value}",
        "specific_dates": "Недоступные даты: {value}",
        "status": "Статус: {status}",
        "change_availability": "Изменить доступность",
        "confirm": "Подтвердить",
        "i_am_fully_available": "Я доступен весь месяц",
        "select_weekdays": "Выбрать дни недели",
        "select_dates": "Выбрать даты",
        "back": "Назад",
        "approve": "Утвердить",
        "request_corrections": "Запросить исправления",
        "reopen_everyone": "Открыть для всех",
        "rebuild_preview": "Пересобрать превью",
        "cancel": "Отменить",
        "close_revision": "Закрыть правки",
        "restart_survey": "Перезапустить опрос",
        "start_survey": "Начать",
    },
}


WEEKDAY_TRANSLATIONS = {
    "en": WEEKDAY_NAMES,
    "fa": PERSIAN_WEEKDAY_NAMES,
    "ar": {
        0: "الاثنين",
        1: "الثلاثاء",
        2: "الأربعاء",
        3: "الخميس",
        4: "الجمعة",
        5: "السبت",
        6: "الأحد",
    },
    "ru": {
        0: "Понедельник",
        1: "Вторник",
        2: "Среда",
        3: "Четверг",
        4: "Пятница",
        5: "Суббота",
        6: "Воскресенье",
    },
}


def text(key: str, language: str | None = None, calendar_type: str = "gregorian") -> str:
    lang = normalize_language(language, calendar_type)
    return TEXTS[lang][key]


def button(text: str, callback_data: str) -> dict:
    return {"text": text, "callback_data": callback_data}


def markup(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def weekday_names(calendar_type: str, language: str | None = None) -> dict[int, str]:
    lang = normalize_language(language, calendar_type)
    return WEEKDAY_TRANSLATIONS[lang]


def availability_summary(
    response: AvailabilityResponse,
    calendar_type: str,
    survey_label: str = "",
    language: str | None = None,
) -> str:
    weekdays = weekday_names(calendar_type, language)
    lang = normalize_language(language, calendar_type)
    joiner = "، " if lang in {"fa", "ar"} else ", "
    selected_weekdays = [
        weekdays[index]
        for index, english_name in WEEKDAY_NAMES.items()
        if english_name.lower() in response.unavailable_weekdays
    ]
    days = joiner.join(str(day) for day in sorted(response.unavailable_days)) or text("none", lang, calendar_type)
    recurring = joiner.join(selected_weekdays) or text("none", lang, calendar_type)
    status = text("confirmed" if response.confirmed else "not_confirmed", lang, calendar_type)
    mode = text("fully_available" if response.mode == "full" else "custom_availability", lang, calendar_type)
    prefix = f"{survey_label}\n" if survey_label else ""
    labels = TEXTS[lang]
    return prefix + "\n".join(
        [
            labels["availability_for"].format(name=response.name),
            labels["mode"].format(mode=mode),
            labels["recurring_weekdays"].format(value=recurring),
            labels["specific_dates"].format(value=days),
            labels["status"].format(status=status),
        ]
    )


def callback(survey_id: str, action: str) -> str:
    return f"av:{survey_id}:{action}"


def main_availability_keyboard(
    response: AvailabilityResponse | None = None,
    survey_id: str = "",
    language: str | None = None,
    calendar_type: str = "gregorian",
) -> dict:
    if response and response.mode == "full":
        return markup(
            [
                [button(text("change_availability", language, calendar_type), callback(survey_id, "custom"))],
                [button(text("confirm", language, calendar_type), callback(survey_id, "confirm"))],
            ]
        )

    return markup(
        [
            [button(text("i_am_fully_available", language, calendar_type), callback(survey_id, "full"))],
            [button(text("select_weekdays", language, calendar_type), callback(survey_id, "weekdays"))],
            [button(text("select_dates", language, calendar_type), callback(survey_id, "dates"))],
            [button(text("confirm", language, calendar_type), callback(survey_id, "confirm"))],
        ]
    )


def start_survey_keyboard(
    survey_id: str,
    language: str | None = None,
    calendar_type: str = "gregorian",
) -> dict:
    return markup([[button(text("start_survey", language, calendar_type), callback(survey_id, "start"))]])


def weekdays_keyboard(
    response: AvailabilityResponse,
    calendar_type: str,
    survey_id: str = "",
    language: str | None = None,
) -> dict:
    names = weekday_names(calendar_type, language)
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
            button(text("back", language, calendar_type), callback(survey_id, "back")),
            button(text("confirm", language, calendar_type), callback(survey_id, "confirm")),
        ]
    )
    return markup(rows)


def dates_keyboard(
    response: AvailabilityResponse,
    year: int,
    month: int,
    calendar_type: str,
    survey_id: str = "",
    language: str | None = None,
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
            button(text("back", language, calendar_type), callback(survey_id, "back")),
            button(text("confirm", language, calendar_type), callback(survey_id, "confirm")),
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
    language: str | None = None,
) -> dict:
    key = survey_id or f"{calendar_type}:{year}:{month}"
    rows = [[button(text("approve", language, calendar_type), f"admin:approve:{key}")]]
    if has_flagged_members:
        rows.append([button(text("request_corrections", language, calendar_type), f"admin:correct:{key}")])
    rows.append([button(text("reopen_everyone", language, calendar_type), f"admin:reopen:{key}")])
    rows.append(
        [
            button(text("rebuild_preview", language, calendar_type), f"admin:regen:{key}"),
        ]
    )
    rows.append([button(text("cancel", language, calendar_type), f"admin:cancel:{key}")])
    return markup(rows)


def admin_revision_keyboard(
    year: int,
    month: int,
    calendar_type: str,
    survey_id: str = "",
    language: str | None = None,
) -> dict:
    key = survey_id or f"{calendar_type}:{year}:{month}"
    return markup(
        [
            [button(text("close_revision", language, calendar_type), f"admin:close:{key}")],
            [button(text("reopen_everyone", language, calendar_type), f"admin:reopen:{key}")],
            [button(text("cancel", language, calendar_type), f"admin:cancel:{key}")],
        ]
    )


def admin_canceled_keyboard(
    year: int,
    month: int,
    calendar_type: str,
    survey_id: str = "",
    language: str | None = None,
) -> dict:
    key = survey_id or f"{calendar_type}:{year}:{month}"
    return markup([[button(text("restart_survey", language, calendar_type), f"admin:restart:{key}")]])
