"""DB-backed user-facing bot messages for access control and flow gating."""

from __future__ import annotations

from dataclasses import dataclass


SUPPORTED_LANGUAGES = ("en", "fa", "ar", "ru")


BOT_MESSAGE_KEYS = (
    "unauthorized",
    "not_active_member",
    "no_active_survey",
    "survey_closed",
    "survey_ended",
    "no_schedule_today",
    "already_submitted_contact_admin",
    "survey_invite",
    "survey_reminder",
    "survey_last_day_group_reminder",
)


def default_language_for_calendar(calendar: str) -> str:
    return "en"


def normalize_language(language: str | None, calendar: str = "gregorian") -> str:
    normalized = str(language or "").strip().lower()
    aliases = {
        "english": "en",
        "فارسی": "fa",
        "farsi": "fa",
        "persian": "fa",
        "arabic": "ar",
        "العربية": "ar",
        "عربی": "ar",
        "russian": "ru",
        "русский": "ru",
        "روسی": "ru",
    }
    normalized = aliases.get(normalized, normalized)
    if not normalized:
        return default_language_for_calendar(calendar)
    if normalized not in SUPPORTED_LANGUAGES:
        allowed = ", ".join(SUPPORTED_LANGUAGES)
        raise ValueError(f"Unsupported language: {language}. Allowed: {allowed}.")
    return normalized


def default_bot_messages(language: str | None, calendar: str = "gregorian") -> dict[str, str]:
    lang = normalize_language(language, calendar)
    if lang == "fa":
        return {
            "unauthorized": "شما اجازه ندارید با این بات صحبت کنید.",
            "not_active_member": "شما در لیست اعضای فعال نیستید.",
            "no_active_survey": "در حال حاضر نظرسنجی فعالی وجود ندارد.",
            "survey_closed": (
                "مهلت ثبت availability تمام شده است. "
                "اگر نیاز به اصلاح داری با ادمین هماهنگ کن."
            ),
            "survey_ended": "این نظرسنجی به پایان رسیده است.",
            "no_schedule_today": "برای امروز برنامه‌ی ادهاک ثبت نشده است.",
            "already_submitted_contact_admin": "پاسخ شما قبلاً ثبت شده است. اگر نیاز به اصلاح دارید با ادمین هماهنگ کنید.",
            "survey_invite": "نظرسنجی دسترسی ماهانه فعال شده است. برای ثبت روزها دکمه شروع را بزنید.",
            "survey_reminder": "یادآوری: لطفاً نظرسنجی دسترسی ماهانه را تکمیل کنید.",
            "survey_last_day_group_reminder": "یادآوری روز آخر: لطفاً برای تکمیل نظرسنجی دسترسی اقدام کنید: {mentions}",
        }
    if lang == "ar":
        return {
            "unauthorized": "ليس لديك صلاحية لاستخدام هذا البوت.",
            "not_active_member": "أنت لست ضمن قائمة الأعضاء النشطين.",
            "no_active_survey": "لا يوجد استبيان توافر نشط حاليا.",
            "survey_closed": "انتهت مهلة تسجيل التوافر. تواصل مع المسؤول إذا احتجت إلى تعديل.",
            "survey_ended": "انتهى هذا الاستبيان.",
            "no_schedule_today": "لا يوجد جدول مناوبة مسجل لليوم.",
            "already_submitted_contact_admin": "تم تسجيل إجابتك مسبقا. إذا احتجت إلى تعديل فتواصل مع المسؤول.",
            "survey_invite": "بدأ استبيان التوافر الشهري. اضغط زر البدء لتسجيل أيامك.",
            "survey_reminder": "تذكير: يرجى إكمال استبيان التوافر الشهري.",
            "survey_last_day_group_reminder": "تذكير اليوم الأخير: يرجى إكمال استبيان التوافر: {mentions}",
        }
    if lang == "ru":
        return {
            "unauthorized": "У вас нет доступа к этому боту.",
            "not_active_member": "Вас нет в списке активных участников.",
            "no_active_survey": "Сейчас нет активного опроса доступности.",
            "survey_closed": "Окно заполнения доступности закрыто. Если нужно изменить ответ, обратитесь к администратору.",
            "survey_ended": "Этот опрос завершен.",
            "no_schedule_today": "На сегодня нет расписания дежурства.",
            "already_submitted_contact_admin": "Ваш ответ уже сохранен. Если нужно изменить его, обратитесь к администратору.",
            "survey_invite": "Опрос месячной доступности открыт. Нажмите кнопку старта, чтобы заполнить дни.",
            "survey_reminder": "Напоминание: пожалуйста, заполните опрос месячной доступности.",
            "survey_last_day_group_reminder": "Последний день: пожалуйста, заполните опрос доступности: {mentions}",
        }
    return {
        "unauthorized": "You are not allowed to use this bot.",
        "not_active_member": "You are not in the active member list.",
        "no_active_survey": "There is no active availability survey right now.",
        "survey_closed": (
            "The availability window is closed. Contact an admin if you need a change."
        ),
        "survey_ended": "This survey has ended.",
        "no_schedule_today": "There is no bug day schedule for today.",
        "already_submitted_contact_admin": "Your response has already been submitted. Contact an admin if you need to edit it.",
        "survey_invite": "The monthly availability survey is open. Tap Start to enter your days.",
        "survey_reminder": "Reminder: please complete the monthly availability survey.",
        "survey_last_day_group_reminder": "Last-day reminder: please complete the availability survey: {mentions}",
    }


@dataclass(frozen=True)
class BotMessages:
    unauthorized: str
    not_active_member: str
    no_active_survey: str
    survey_closed: str
    survey_ended: str
    no_schedule_today: str
    already_submitted_contact_admin: str
    survey_invite: str
    survey_reminder: str
    survey_last_day_group_reminder: str

    def as_dict(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in BOT_MESSAGE_KEYS}

    @classmethod
    def resolve(
        cls,
        raw: dict | None,
        language: str | None = None,
        calendar: str = "gregorian",
    ) -> "BotMessages":
        """Merge DB overrides onto language defaults. Missing keys use defaults."""
        defaults = default_bot_messages(language, calendar)
        overrides = raw if isinstance(raw, dict) else {}
        merged: dict[str, str] = {}
        for key in BOT_MESSAGE_KEYS:
            value = overrides.get(key)
            if value is None or str(value).strip() == "":
                merged[key] = defaults[key]
            else:
                merged[key] = str(value)
        return cls(**merged)
