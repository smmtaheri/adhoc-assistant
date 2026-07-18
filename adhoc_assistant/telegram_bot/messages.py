"""DB-backed user-facing bot messages for access control and flow gating."""

from __future__ import annotations

from dataclasses import dataclass


BOT_MESSAGE_KEYS = (
    "unauthorized",
    "not_active_member",
    "no_active_survey",
    "survey_closed",
    "survey_ended",
    "no_schedule_today",
)


def default_bot_messages(calendar: str) -> dict[str, str]:
    if calendar == "jalali":
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
    }


@dataclass(frozen=True)
class BotMessages:
    unauthorized: str
    not_active_member: str
    no_active_survey: str
    survey_closed: str
    survey_ended: str
    no_schedule_today: str

    def as_dict(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in BOT_MESSAGE_KEYS}

    @classmethod
    def resolve(cls, raw: dict | None, calendar: str) -> "BotMessages":
        """Merge DB overrides onto calendar defaults. Missing keys use defaults."""
        defaults = default_bot_messages(calendar)
        overrides = raw if isinstance(raw, dict) else {}
        merged: dict[str, str] = {}
        for key in BOT_MESSAGE_KEYS:
            value = overrides.get(key)
            if value is None or str(value).strip() == "":
                merged[key] = defaults[key]
            else:
                merged[key] = str(value)
        return cls(**merged)
