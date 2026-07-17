import argparse
import json
import logging
import re
import time
from copy import deepcopy
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from adhoc_assistant.calendars import (
    format_month_title,
    gregorian_to_jalali,
    jalali_to_gregorian,
    normalize_calendar_type,
    parse_calendar_date,
)
from adhoc_assistant.config import normalize_holidays
from adhoc_assistant.exporters import export_image_calendar
from adhoc_assistant.scheduler import build_schedule, is_available, month_workdays, stats_to_plain_dict
from adhoc_assistant.storage import load_history_from_db, save_month_to_db
from adhoc_assistant.telegram_bot.keyboards import (
    admin_canceled_keyboard,
    admin_approval_keyboard,
    admin_revision_keyboard,
    availability_summary,
    dates_keyboard,
    main_availability_keyboard,
    weekdays_keyboard,
)
from adhoc_assistant.telegram_bot.repository import (
    AvailabilityResponse,
    BotRepository,
    SURVEY_KIND_DEBUG,
    SURVEY_KIND_PRODUCTION,
    Survey,
    normalize_username,
    schedule_person_key,
    stable_participant_id,
    utc_now,
)
from adhoc_assistant.telegram_bot.settings import (
    InfraSettings,
    Member,
    RuntimeSettings,
    load_infra_settings,
    resolve_database_path,
)
from adhoc_assistant.telegram_bot.telegram import TelegramClient
from adhoc_assistant.telegram_bot.telegram import TelegramApiError


logger = logging.getLogger(__name__)

STATUS_SCHEDULED = "scheduled"
STATUS_COLLECTING = "collecting"
STATUS_PENDING_ADMIN_REVIEW = "pending_admin_review"
STATUS_BLOCKED = "blocked"
STATUS_REVISION_REQUESTED = "revision_requested"
STATUS_PUBLISHING = "publishing"
STATUS_APPROVED = "approved"
STATUS_CANCELED = "canceled"
TERMINAL_STATUSES = {STATUS_APPROVED, STATUS_CANCELED}
EDITABLE_SURVEY_STATUSES = {STATUS_COLLECTING, STATUS_REVISION_REQUESTED}
PREVIEW_SOURCE_STATUSES = {
    STATUS_COLLECTING,
    STATUS_REVISION_REQUESTED,
    STATUS_PENDING_ADMIN_REVIEW,
    STATUS_BLOCKED,
}
APPROVABLE_STATUSES = {STATUS_PENDING_ADMIN_REVIEW}
CANCELABLE_STATUSES = {
    STATUS_SCHEDULED,
    STATUS_COLLECTING,
    STATUS_PENDING_ADMIN_REVIEW,
    STATUS_BLOCKED,
    STATUS_REVISION_REQUESTED,
    STATUS_PUBLISHING,
}
ACTIVE_MEMBER_ERROR = "You are not in the active member list."
TEMPORARY_TELEGRAM_ERROR = "Temporary Telegram problem. Please try again in a moment."
TELEGRAM_PHOTO_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_TEXT_LIMIT = 4096
MAIN_IMBALANCE_THRESHOLD = 2
HIGH_UNAVAILABLE_MIN_DAYS = 5
HIGH_UNAVAILABLE_EXTRA_DAYS = 3
HIGH_UNAVAILABLE_RATIO = 0.35
NO_MAIN = "NO_AVAILABLE_PERSON"
NO_BACKUP = "NO_AVAILABLE_BACKUP"


def next_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def current_calendar_month(today: date, calendar_type: str) -> tuple[int, int]:
    if calendar_type == "jalali":
        year, month, _ = gregorian_to_jalali(today)
        return year, month
    return today.year, today.month


def calendar_month_start(year: int, month: int, calendar_type: str) -> date:
    if calendar_type == "jalali":
        return jalali_to_gregorian(year, month, 1)
    return date(year, month, 1)


def survey_target_month(today: date, calendar_type: str, days_before: int) -> tuple[int, int]:
    year, month = current_calendar_month(today, calendar_type)
    next_year, next_month_value = next_month(year, month)
    next_start = calendar_month_start(next_year, next_month_value, calendar_type)
    if today >= next_start - timedelta(days=days_before):
        return next_year, next_month_value
    return year, month


def due_survey_month(
    today: date,
    calendar_type: str,
    days_before: int,
) -> tuple[int, int] | None:
    year, month = current_calendar_month(today, calendar_type)
    next_year, next_month_value = next_month(year, month)
    next_start = calendar_month_start(next_year, next_month_value, calendar_type)
    if next_start - timedelta(days=days_before) <= today < next_start:
        return next_year, next_month_value
    return None


def is_relative_schedule(raw_value: str) -> bool:
    return bool(re.fullmatch(r"\+?\s*(\d+)\s*([mhd])", raw_value.strip().lower()))


def parse_scheduled_datetime(raw_value: str, now: datetime) -> datetime | None:
    value = raw_value.strip()
    if not value:
        return None

    relative = re.fullmatch(r"\+?\s*(\d+)\s*([mhd])", value.lower())
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit == "m":
            return now + timedelta(minutes=amount)
        if unit == "h":
            return now + timedelta(hours=amount)
        return now + timedelta(days=amount)

    normalized = value.replace(" ", "T", 1)
    scheduled = datetime.fromisoformat(normalized)
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=now.tzinfo)
    return scheduled.astimezone(now.tzinfo)


def active_members(members: list[Member]) -> list[Member]:
    return [member for member in members if member.active]


def schedule_members(members: list[Member]) -> list[Member]:
    return [
        member
        for member in members
        if member.active and member.participates_in_schedule
    ]


def can_receive_telegram_messages(member: Member) -> bool:
    return member.telegram_id > 0


def is_local_only_member(member: Member) -> bool:
    return member.telegram_id < 0 and not normalize_username(member.username)


def is_unregistered_member(member: Member) -> bool:
    return bool(normalize_username(member.username)) and not can_receive_telegram_messages(member)


def default_response(member: Member) -> AvailabilityResponse:
    return AvailabilityResponse(
        telegram_id=stable_participant_id(member) if member.telegram_id == 0 and normalize_username(member.username) else member.telegram_id,
        name=member.name,
        unavailable_days=list(member.unavailable_days),
        unavailable_weekdays=list(member.unavailable_weekdays),
        confirmed=is_local_only_member(member),
        mode="custom",
    )


def response_for_member(
    repo: BotRepository,
    member: Member,
    calendar_type: str,
    year: int,
    month: int,
) -> AvailabilityResponse:
    return (
        repo.get_response(calendar_type, year, month, member.telegram_id)
        or default_response(member)
    )


def response_for_survey_member(
    repo: BotRepository,
    survey_id: str,
    member: Member,
) -> AvailabilityResponse:
    return repo.get_survey_response(survey_id, member.telegram_id) or default_response(member)


def survey_identity(calendar_type: str, year: int, month: int) -> str:
    return f"S-{calendar_type}-{year}-{month:02d}-{uuid4().hex[:6]}"


def local_only_member(name: str, index: int, role: str = "backend") -> Member:
    return Member(
        name=name.strip(),
        telegram_id=-(index + 1),
        username="",
        role=role,
        active=True,
    )


def parse_survey_participants(raw: str, repo: BotRepository) -> list[Member]:
    participants: list[Member] = []
    local_index = 0
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        username = normalize_username(token)
        user = repo.get_user_by_username(username)
        if user is not None:
            participants.append(repo.user_to_member(user))
            continue
        matched = next(
            (member for member in repo.list_members(active_only=False) if member.name == token),
            None,
        )
        if matched is not None:
            participants.append(matched)
            continue
        participants.append(local_only_member(token, local_index))
        local_index += 1
    return participants


def parse_stored_datetime(raw_value: str, now: datetime) -> datetime | None:
    if not raw_value:
        return None
    value = datetime.fromisoformat(raw_value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=now.tzinfo)
    return value.astimezone(now.tzinfo)


def build_schedule_config(
    base_config: dict,
    members: list[Member],
    responses: dict[int, AvailabilityResponse],
    calendar_type: str,
    year: int,
    month: int,
) -> dict:
    config = deepcopy(base_config)
    config["calendar"] = calendar_type
    config["year"] = year
    config["month"] = month
    config["people"] = []

    for member in schedule_members(members):
        response = responses.get(member.telegram_id) or default_response(member)
        unavailable_dates = [
            parse_calendar_date(day, year, month, calendar_type).isoformat()
            for day in response.unavailable_days
        ]
        config["people"].append(
            {
                "name": schedule_person_key(member),
                "role": member.role,
                "unavailable_dates": sorted(unavailable_dates),
                "unavailable_weekdays": sorted(response.unavailable_weekdays),
            }
        )

    return config


def response_person(
    member: Member,
    response: AvailabilityResponse,
    year: int,
    month: int,
    calendar_type: str,
) -> dict:
    unavailable_dates = [
        parse_calendar_date(day, year, month, calendar_type).isoformat()
        for day in response.unavailable_days
    ]
    return {
        "name": schedule_person_key(member),
        "role": member.role,
        "unavailable_dates": sorted(unavailable_dates),
        "unavailable_weekdays": sorted(response.unavailable_weekdays),
    }


def member_unavailability_report(
    member: Member,
    response: AvailabilityResponse,
    year: int,
    month: int,
    calendar_type: str,
) -> dict:
    workdays = month_workdays(year, month, calendar_type)
    person = response_person(member, response, year, month, calendar_type)
    unavailable_count = sum(1 for workday in workdays if not is_available(person, workday))
    workday_count = len(workdays)
    availability_percent = (
        round(((workday_count - unavailable_count) / workday_count) * 100)
        if workday_count
        else 100
    )
    return {
        "telegram_id": member.telegram_id,
        "name": member.name,
        "unavailable_count": unavailable_count,
        "workday_count": workday_count,
        "availability_percent": availability_percent,
        "confirmed": response.confirmed,
        "mode": response.mode,
    }


def build_review_report(
    schedule: list[dict],
    stats: dict,
    members: list[Member],
    responses: dict[int, AvailabilityResponse],
    calendar_type: str,
    year: int,
    month: int,
) -> dict:
    active = schedule_members(members)
    member_responses = {
        member.telegram_id: responses.get(member.telegram_id) or default_response(member)
        for member in active
    }
    people_by_key = {
        schedule_person_key(member): response_person(
            member,
            member_responses[member.telegram_id],
            year,
            month,
            calendar_type,
        )
        for member in active
    }
    member_by_key = {schedule_person_key(member): member for member in active}
    member_by_id = {member.telegram_id: member for member in active}
    flagged_member_ids: set[int] = set()
    coverage_warnings = []
    warnings = []

    unregistered = [member for member in active if is_unregistered_member(member)]
    for member in unregistered:
        label = f"@{normalize_username(member.username)}"
        warnings.append(
            f"Unregistered member {label} ({member.name}) was included but cannot be contacted."
        )

    for item in schedule:
        missing_main = item["main"] == NO_MAIN
        missing_backup = item["backup"] == NO_BACKUP
        if not missing_main and not missing_backup:
            continue

        workday = date.fromisoformat(item.get("gregorian_date", item["date"]))
        available = [
            name for name, person in people_by_key.items() if is_available(person, workday)
        ]
        unavailable = sorted(set(people_by_key) - set(available))
        for name in unavailable:
            member = member_by_key[name]
            if can_receive_telegram_messages(member):
                flagged_member_ids.add(member.telegram_id)

        missing_parts = []
        if missing_main:
            missing_parts.append("main")
        if missing_backup:
            missing_parts.append("backup")
        coverage_warnings.append(
            {
                "date": item["date"],
                "weekday": item["weekday"],
                "missing_main": missing_main,
                "missing_backup": missing_backup,
                "available": sorted(available),
                "unavailable": unavailable,
            }
        )
        warnings.append(
            "Coverage gap on "
            f"{item['date']} ({item['weekday']}): missing "
            f"{' and '.join(missing_parts)}; available: "
            f"{', '.join(available) or 'nobody'}"
        )

    plain_stats = stats_to_plain_dict(stats)
    main_counts = [values["main_count"] for values in plain_stats.values()]
    main_spread = max(main_counts) - min(main_counts) if main_counts else 0
    if main_spread > MAIN_IMBALANCE_THRESHOLD:
        min_main = min(main_counts)
        max_main = max(main_counts)
        overloaded = [
            name for name, values in plain_stats.items() if values["main_count"] == max_main
        ]
        underloaded = [
            name for name, values in plain_stats.items() if values["main_count"] == min_main
        ]
        warnings.append(
            "Main assignment spread is "
            f"{main_spread}; overloaded: {', '.join(sorted(overloaded))}; "
            f"underloaded: {', '.join(sorted(underloaded))}."
        )

    availability = [
        member_unavailability_report(
            member,
            member_responses[member.telegram_id],
            year,
            month,
            calendar_type,
        )
        for member in active
    ]
    average_unavailable = (
        sum(item["unavailable_count"] for item in availability) / len(availability)
        if availability
        else 0
    )
    for item in availability:
        if not item["confirmed"] and item["telegram_id"] > 0:
            flagged_member_ids.add(item["telegram_id"])
        high_unavailable = (
            item["unavailable_count"] >= HIGH_UNAVAILABLE_MIN_DAYS
            and item["unavailable_count"] >= average_unavailable + HIGH_UNAVAILABLE_EXTRA_DAYS
            and item["unavailable_count"] / max(item["workday_count"], 1) >= HIGH_UNAVAILABLE_RATIO
        )
        if high_unavailable:
            warnings.append(
                f"{item['name']} is unavailable for "
                f"{item['unavailable_count']}/{item['workday_count']} workdays."
            )
            if item["telegram_id"] > 0:
                flagged_member_ids.add(item["telegram_id"])

    status = "needs_review" if warnings else "ready"

    return {
        "status": status,
        "coverage_warnings": coverage_warnings,
        "warnings": warnings,
        "flagged_member_ids": sorted(flagged_member_ids),
        "flagged_member_names": [
            member_by_id[member_id].name
            for member_id in sorted(flagged_member_ids)
            if member_id in member_by_id
        ],
        "unregistered_member_names": [member.name for member in unregistered],
        "availability": sorted(availability, key=lambda item: item["name"]),
        "main_spread": main_spread,
        "average_unavailable": round(average_unavailable, 2),
    }


def review_text(review: dict, calendar_type: str) -> str:
    if calendar_type == "jalali":
        lines = ["گزارش بررسی:"]
        if review.get("warnings"):
            lines.append("نیازمند بررسی:")
            lines.extend(f"- {warning}" for warning in review["warnings"])
        if review.get("flagged_member_names"):
            lines.append(
                "در صورت زدن Request corrections برای این افراد ارسال می‌شود: "
                f"{format_names(review['flagged_member_names'], calendar_type)}"
            )
        lines.append("وضعیت افراد:")
        for item in review["availability"]:
            confirmed = "تایید شده" if item["confirmed"] else "تایید نشده"
            lines.append(
                f"- {item['name']}: {item['unavailable_count']}/"
                f"{item['workday_count']} روز نیست، {item['availability_percent']}٪ حاضر، "
                f"{confirmed}"
            )
        return "\n".join(lines)

    lines = ["Review report:"]
    if review.get("warnings"):
        lines.append("Needs review:")
        lines.extend(f"- {warning}" for warning in review["warnings"])
    if review.get("flagged_member_names"):
        lines.append(
            "Request corrections will be sent to: "
            f"{format_names(review['flagged_member_names'], calendar_type)}"
        )
    lines.append("Availability:")
    for item in review["availability"]:
        confirmed = "confirmed" if item["confirmed"] else "not confirmed"
        lines.append(
            f"- {item['name']}: unavailable {item['unavailable_count']}/"
            f"{item['workday_count']}, {item['availability_percent']}% available, "
            f"{confirmed}"
        )
    return "\n".join(lines)


def summary_text(stats: dict, calendar_type: str) -> str:
    thursday = "پنجشنبه" if calendar_type == "jalali" else "Thursday"
    lines = ["Monthly summary:"]
    for name, values in sorted(stats_to_plain_dict(stats).items()):
        lines.append(
            f"{name}: main {values['main_count']}, backup {values['backup_count']}, "
            f"total {values['total_count']}, {thursday} {values['thursday_count']}"
        )
    return "\n".join(lines)


def month_label(year: int, month: int, calendar_type: str) -> str:
    return format_month_title(year, month, calendar_type)


def group_schedule_caption(year: int, month: int, calendar_type: str) -> str:
    if calendar_type == "jalali":
        return f"برنامه‌ی ادهاک {month_label(year, month, calendar_type)}"
    return f"Adhoc schedule - {month_label(year, month, calendar_type)}"


def approval_sent_message(year: int, month: int, calendar_type: str) -> str:
    if calendar_type == "jalali":
        return f"برنامه‌ی {month_label(year, month, calendar_type)} ارسال و پین شد."
    return f"{month_label(year, month, calendar_type)} schedule was posted and pinned."


def approval_posted_pin_failed_message(
    year: int,
    month: int,
    calendar_type: str,
    error: str,
) -> str:
    if calendar_type == "jalali":
        return (
            f"برنامه‌ی {month_label(year, month, calendar_type)} ارسال شد، "
            f"ولی به‌دلیل خطای زیر پین نشد:\n{error}"
        )
    return (
        f"{month_label(year, month, calendar_type)} schedule was posted, "
        f"but pinning failed with this error:\n{error}"
    )


def preview_photo_caption(year: int, month: int, calendar_type: str, review: dict) -> str:
    if review.get("warnings"):
        status = "نیازمند بررسی" if calendar_type == "jalali" else "needs review"
    else:
        status = "آماده بررسی" if calendar_type == "jalali" else "ready for review"

    if calendar_type == "jalali":
        caption = f"پیش‌نمایش برنامه‌ی {month_label(year, month, calendar_type)}\nوضعیت: {status}"
    else:
        caption = f"Preview for {month_label(year, month, calendar_type)}\nStatus: {status}"
    return caption[:TELEGRAM_PHOTO_CAPTION_LIMIT]


def preview_report_message(
    year: int,
    month: int,
    calendar_type: str,
    stats: dict,
    review: dict,
) -> str:
    if calendar_type == "jalali":
        title = f"گزارش پیش‌نمایش {month_label(year, month, calendar_type)}"
    else:
        title = f"Preview report for {month_label(year, month, calendar_type)}"
    return (
        f"{title}\n\n"
        f"{summary_text(stats, calendar_type)}\n\n"
        f"{review_text(review, calendar_type)}"
    )


def split_telegram_text(text: str, limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.splitlines():
        next_line = f"{line}\n"
        if len(next_line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.extend(
                next_line[index : index + limit].rstrip()
                for index in range(0, len(next_line), limit)
            )
            continue
        if len(current) + len(next_line) > limit:
            chunks.append(current.rstrip())
            current = next_line
        else:
            current += next_line
    if current:
        chunks.append(current.rstrip())
    return chunks


def unauthorized_message(calendar_type: str) -> str:
    if calendar_type == "jalali":
        return "شما اجازه ندارید با این بات صحبت کنید."
    return "You are not allowed to use this bot."


def no_schedule_for_today_message(calendar_type: str) -> str:
    if calendar_type == "jalali":
        return "برای امروز برنامه‌ی ادهاک ثبت نشده است."
    return "There is no bug day schedule for today."


def today_bug_day_message(
    main_member: Member | None,
    backup_member: Member | None,
    main_name: str,
    backup_name: str,
    calendar_type: str,
) -> str:
    main_id = main_member.telegram_id if main_member else "unknown"
    backup_id = backup_member.telegram_id if backup_member else "unknown"
    if calendar_type == "jalali":
        return (
            "برنامه‌ی امروز:\n"
            f"روز باگ: {main_name} ({main_id})\n"
            f"پشتیبان: {backup_name} ({backup_id})"
        )
    return (
        "Today's schedule:\n"
        f"Bug day: {main_name} ({main_id})\n"
        f"Helper: {backup_name} ({backup_id})"
    )


def telegram_message_id(response: dict) -> int | None:
    if "message_id" in response:
        return int(response["message_id"])
    result = response.get("result")
    if isinstance(result, dict) and "message_id" in result:
        return int(result["message_id"])
    return None


def mention(member: Member) -> str:
    if member.username:
        return f"@{member.username.lstrip('@')}"
    return member.name


def telegram_display_name(user: dict | None) -> str:
    if not user:
        return ""
    return str(user.get("first_name", "")).strip()


def format_names(names: list[str], calendar_type: str) -> str:
    if not names:
        return "هیچ‌کس" if calendar_type == "jalali" else "nobody"
    return "، ".join(names) if calendar_type == "jalali" else ", ".join(names)


class AdhocTelegramBot:
    def __init__(
        self,
        settings: InfraSettings,
        members: list[Member],
        repo: BotRepository,
        telegram: TelegramClient,
    ) -> None:
        self.settings = settings
        self.members = members
        self.repo = repo
        self.telegram = telegram
        self.scheduled_work_interval_seconds = 30
        self.next_scheduled_work_at: datetime | None = None
        self._runtime_cache: RuntimeSettings | None = None
        # Domain policy must already exist in DB; no hidden seed.
        _ = self.runtime

    @property
    def runtime(self) -> RuntimeSettings:
        if self._runtime_cache is None:
            self._runtime_cache = self.repo.get_runtime_settings()
        return self._runtime_cache

    def refresh_runtime(self) -> RuntimeSettings:
        self._runtime_cache = self.repo.get_runtime_settings()
        return self._runtime_cache

    @property
    def calendar_type(self) -> str:
        return normalize_calendar_type(self.runtime.calendar)

    @property
    def timezone_name(self) -> str:
        return self.runtime.timezone

    def find_active_member(self, telegram_id: int) -> Member | None:
        return next(
            (
                member
                for member in self.members
                if member.telegram_id == telegram_id and member.active
            ),
            None,
        )

    def find_active_member_by_telegram_id(self, telegram_id: int) -> Member | None:
        return self.find_active_member(telegram_id)

    def refresh_members(self) -> None:
        db_members = self.repo.list_members(active_only=True)
        if db_members or not self.members:
            self.members = db_members

    def is_admin(self, telegram_id: int) -> bool:
        member = self.find_active_member(telegram_id)
        return member is not None and member.access_level == "admin"

    def admin_telegram_ids(self) -> list[int]:
        self.refresh_members()
        ids = set()
        for member in active_members(self.members):
            if member.access_level == "admin" and can_receive_telegram_messages(member):
                ids.add(member.telegram_id)
        return sorted(ids)

    def is_authorized_user(self, telegram_id: int) -> bool:
        self.refresh_members()
        return self.is_admin(telegram_id) or self.find_active_member(telegram_id) is not None

    def member_by_name(self) -> dict[str, Member]:
        """Lookup by schedule key (username) and display name for legacy rows."""
        self.refresh_members()
        mapping: dict[str, Member] = {}
        for member in self.members:
            mapping[schedule_person_key(member)] = member
            mapping[member.name] = member
        return mapping

    def messageable_survey_participants(self, survey: Survey) -> list[Member]:
        return [
            member
            for member in self.repo.list_survey_participants(survey.id)
            if can_receive_telegram_messages(member)
        ]

    def messageable_active_members(self) -> list[Member]:
        return [
            member
            for member in schedule_members(self.members)
            if can_receive_telegram_messages(member)
        ]

    def member_can_receive_revision(
        self,
        telegram_id: int,
        survey: Survey | None = None,
    ) -> bool:
        if survey is not None:
            participants = {
                member.telegram_id: member
                for member in self.repo.list_survey_participants(survey.id)
            }
            member = participants.get(telegram_id)
            return member is not None and can_receive_telegram_messages(member)
        member = self.find_active_member_by_telegram_id(telegram_id)
        return member is not None and can_receive_telegram_messages(member)

    def member_names_for_ids(
        self,
        telegram_ids: list[int],
        survey: Survey | None = None,
    ) -> list[str]:
        if survey is not None:
            members_by_id = {
                member.telegram_id: member
                for member in self.repo.list_survey_participants(survey.id)
            }
        else:
            members_by_id = {
                member.telegram_id: member for member in schedule_members(self.members)
            }
        return [
            members_by_id[telegram_id].name
            for telegram_id in telegram_ids
            if telegram_id in members_by_id
        ]

    def telegram_destination(self) -> tuple[int, int | None]:
        destination = self.repo.get_telegram_destination()
        if not destination:
            raise RuntimeError(
                "Telegram group destination is not configured in SQLite. "
                "Set it with --set-telegram-group-chat-id."
            )
        return destination["group_chat_id"], destination["topic_id"]

    def responses_with_survey_defaults(
        self,
        responses: dict[int, AvailabilityResponse],
        participants: list[Member],
    ) -> dict[int, AvailabilityResponse]:
        effective = dict(responses)
        for member in schedule_members(participants):
            response = effective.get(member.telegram_id) or default_response(member)
            effective[member.telegram_id] = response
        return effective

    def survey_label(self, survey: Survey) -> str:
        return (
            f"{month_label(survey.year, survey.month, survey.calendar_type)}"
            f" · {survey.id} · {survey.status}"
        )

    def survey_form_text(self, survey: Survey, response: AvailabilityResponse) -> str:
        return availability_summary(
            response,
            survey.calendar_type,
            survey_label=self.survey_label(survey),
        )

    def active_survey_record(self, kind: str = SURVEY_KIND_PRODUCTION) -> Survey | None:
        return self.repo.get_active_survey(kind)

    def editable_survey_for_member(self, member: Member) -> Survey | None:
        for kind in (SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG):
            survey = self.repo.get_active_survey(kind)
            if survey is None or survey.status not in EDITABLE_SURVEY_STATUSES:
                continue
            participant_ids = {
                participant.telegram_id
                for participant in self.repo.list_survey_participants(survey.id)
            }
            if member.telegram_id not in participant_ids:
                continue
            state = self.survey_state_for(survey)
            if self.member_can_edit_survey(member, survey, state):
                return survey
        return None

    def survey_state_for(self, survey: Survey) -> dict:
        phase_state = self.repo.get_active_survey_phase(survey.kind) or {}
        if phase_state.get("id") == survey.id:
            return phase_state
        return {
            "id": survey.id,
            "kind": survey.kind,
            "calendar": survey.calendar_type,
            "year": survey.year,
            "month": survey.month,
            "phase": survey.status,
            "collect_until": survey.closes_at,
            "allowed_member_ids": phase_state.get("allowed_member_ids", []),
        }

    def active_or_latest_survey_for_month(
        self,
        year: int,
        month: int,
        *,
        kind: str = SURVEY_KIND_PRODUCTION,
    ) -> Survey | None:
        survey = self.active_survey_record(kind)
        if survey and survey.year == year and survey.month == month:
            return survey
        return self.repo.get_latest_survey_for_month(
            self.calendar_type,
            year,
            month,
            kind=kind,
        )

    def persist_member_response(self, survey: Survey, response: AvailabilityResponse) -> None:
        self.repo.save_survey_response(survey.id, response)
        if survey.kind == SURVEY_KIND_PRODUCTION:
            self.repo.save_response(
                survey.calendar_type,
                survey.year,
                survey.month,
                response,
            )

    def responses_for_survey(self, survey: Survey) -> dict[int, AvailabilityResponse]:
        responses = self.repo.list_survey_responses(survey.id)
        if survey.kind != SURVEY_KIND_PRODUCTION:
            return responses
        legacy = self.repo.list_responses(survey.calendar_type, survey.year, survey.month)
        for telegram_id, response in legacy.items():
            if telegram_id in responses:
                continue
            self.repo.save_survey_response(survey.id, response)
            responses[telegram_id] = response
        return responses

    def configured_closes_at(self, now: datetime, raw_value: str | None = None) -> str:
        raw = self.runtime.survey_collect_for if raw_value is None else raw_value
        deadline = self.deadline_at(raw, now) if raw else None
        return deadline.isoformat() if deadline else ""

    def create_survey_record(
        self,
        year: int,
        month: int,
        now: datetime,
        *,
        starts_at: datetime | None = None,
        closes_at: datetime | None = None,
        status: str = STATUS_SCHEDULED,
        created_by: str = "auto",
        kind: str = SURVEY_KIND_PRODUCTION,
        participants: list[Member] | None = None,
    ) -> Survey:
        self.refresh_members()
        starts_at = starts_at or now
        closes_at_value = (
            closes_at.isoformat()
            if closes_at is not None
            else self.configured_closes_at(starts_at)
        )
        survey = self.repo.create_survey(
            survey_id=survey_identity(self.calendar_type, year, month),
            calendar_type=self.calendar_type,
            year=year,
            month=month,
            status=status,
            starts_at=starts_at.isoformat(),
            closes_at=closes_at_value,
            created_by=created_by,
            participants=participants or schedule_members(self.members),
            kind=kind,
        )
        self.repo.set_active_survey_phase(survey)
        return survey

    def start_collecting(self, year: int, month: int, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        survey = self.active_or_latest_survey_for_month(year, month)
        if survey is None or survey.status in TERMINAL_STATUSES:
            survey = self.create_survey_record(
                year,
                month,
                now,
                starts_at=now,
                status=STATUS_COLLECTING,
                created_by="legacy-start_collecting",
            )
        else:
            self.repo.update_survey(
                survey.id,
                status=STATUS_COLLECTING,
                starts_at=survey.starts_at or now.isoformat(),
                closes_at=survey.closes_at or self.configured_closes_at(now),
            )
            survey = self.repo.get_survey(survey.id) or survey
        self.repo.set_active_survey_phase(
            survey,
            phase=STATUS_COLLECTING,
            collect_until=survey.closes_at,
            allowed_member_ids=[],
        )

    def start_revision(
        self,
        survey: Survey,
        member_ids: list[int],
        now: datetime | None = None,
    ) -> Survey:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        deadline_at = self.deadline_at(self.runtime.revision_collect_for, now)
        closes_at = deadline_at.isoformat() if deadline_at else ""
        if not self.repo.transition_survey(
            survey.id,
            from_statuses={STATUS_PENDING_ADMIN_REVIEW},
            to_status=STATUS_REVISION_REQUESTED,
            closes_at=closes_at,
        ):
            current = self.repo.get_survey(survey.id)
            status = current.status if current else "unknown"
            raise RuntimeError(
                f"Cannot start revision from status {status}; expected pending_admin_review."
            )
        survey = self.repo.get_survey(survey.id) or survey
        self.repo.set_active_survey_phase(
            survey,
            phase=STATUS_REVISION_REQUESTED,
            collect_until=closes_at,
            allowed_member_ids=sorted(set(member_ids)),
        )
        return survey

    def deadline_at(self, raw_value: str, now: datetime) -> datetime | None:
        if not raw_value:
            return None
        return parse_scheduled_datetime(raw_value, now)

    def target_month(self, today: date | None = None) -> tuple[int, int]:
        if self.runtime.target_year is not None and self.runtime.target_month is not None:
            return self.runtime.target_year, self.runtime.target_month

        today = today or datetime.now(ZoneInfo(self.timezone_name)).date()
        return survey_target_month(
            today=today,
            calendar_type=self.calendar_type,
            days_before=self.runtime.survey_days_before_month,
        )

    def scheduled_survey_due(self, now: datetime | None = None) -> tuple[int, int] | None:
        raw_start = self.runtime.survey_start_at
        if not raw_start:
            return None

        now = now or datetime.now(ZoneInfo(self.timezone_name))
        state = self.repo.get_state("scheduled_survey_start")
        if state and state.get("consumed"):
            return None
        if state and state.get("raw") == raw_start:
            scheduled = datetime.fromisoformat(state["start_at"])
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=now.tzinfo)
            scheduled = scheduled.astimezone(now.tzinfo)
        else:
            scheduled = parse_scheduled_datetime(raw_start, now)
            if scheduled is None:
                return None
            self.repo.set_state(
                "scheduled_survey_start",
                {"raw": raw_start, "start_at": scheduled.isoformat(), "consumed": False},
            )

        if now < scheduled:
            return None
        return self.target_month(now.date())

    def survey_month_due(self, today: date | None = None) -> tuple[int, int] | None:
        today = today or datetime.now(ZoneInfo(self.timezone_name)).date()
        return due_survey_month(
            today=today,
            calendar_type=self.calendar_type,
            days_before=self.runtime.survey_days_before_month,
        )

    def ensure_survey_started(self, today: date | datetime | None = None) -> None:
        if isinstance(today, datetime):
            now = today.astimezone(ZoneInfo(self.timezone_name))
        elif today is not None:
            now = datetime.combine(today, day_time.min, tzinfo=ZoneInfo(self.timezone_name))
        else:
            now = datetime.now(ZoneInfo(self.timezone_name))

        active = self.active_survey_record(SURVEY_KIND_PRODUCTION)
        if active is not None:
            if active.status == STATUS_SCHEDULED and self.survey_start_reached(active, now):
                self.send_survey_by_id(active.id, now)
            else:
                logger.info(
                    "Survey %s is still %s; not starting another survey.",
                    active.id,
                    active.status,
                )
            return

        due_month = self.scheduled_survey_due(now) or self.survey_month_due(now.date())
        if due_month is None:
            return

        year, month = due_month
        existing = self.repo.get_latest_survey_for_month(
            self.calendar_type,
            year,
            month,
            kind=SURVEY_KIND_PRODUCTION,
        )
        if existing is not None and existing.status != STATUS_CANCELED:
            logger.info(
                "Survey for %s/%s already exists (%s) with status %s; not sending requests again.",
                year,
                month,
                existing.id,
                existing.status,
            )
            return

        survey = self.create_survey_record(
            year,
            month,
            now,
            starts_at=now,
            status=STATUS_COLLECTING,
            created_by="auto",
            kind=SURVEY_KIND_PRODUCTION,
        )
        self.mark_scheduled_survey_consumed()
        self.send_survey_by_id(survey.id, now)

    def survey_start_reached(self, survey: Survey, now: datetime) -> bool:
        starts_at = parse_stored_datetime(survey.starts_at, now)
        return starts_at is None or now >= starts_at

    def mark_scheduled_survey_consumed(self) -> None:
        state = self.repo.get_state("scheduled_survey_start")
        if state:
            state["consumed"] = True
            state["consumed_at"] = datetime.now(ZoneInfo(self.timezone_name)).isoformat()
            self.repo.set_state("scheduled_survey_start", state)

    def scheduled_survey_start_at(self, now: datetime) -> datetime | None:
        raw = self.runtime.survey_start_at
        state = self.repo.get_state("scheduled_survey_start")
        if state and state.get("raw") == raw:
            scheduled = datetime.fromisoformat(state["start_at"])
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=now.tzinfo)
            return scheduled.astimezone(now.tzinfo)

        scheduled = parse_scheduled_datetime(raw, now)
        if scheduled is not None:
            self.repo.set_state(
                "scheduled_survey_start",
                {"raw": raw, "start_at": scheduled.isoformat(), "consumed": False},
            )
        return scheduled

    def reset_cycle_state(self, year: int, month: int) -> None:
        survey = self.active_or_latest_survey_for_month(year, month)
        if survey is not None and survey.status == STATUS_APPROVED:
            raise ValueError(f"Cannot reset approved survey {survey.id}.")
        if survey is not None:
            self.repo.update_survey(survey.id, status=STATUS_CANCELED)
            self.repo.delete_survey_responses(survey.id)
        self.repo.delete_month_responses(self.calendar_type, year, month)
        if survey is not None:
            self.repo.clear_active_survey_phase(survey.kind)

    def send_survey(self, year: int, month: int, now: datetime) -> None:
        survey = self.active_or_latest_survey_for_month(year, month)
        if survey is None or survey.status in TERMINAL_STATUSES:
            survey = self.create_survey_record(
                year,
                month,
                now,
                starts_at=now,
                status=STATUS_COLLECTING,
                created_by="legacy-send_survey",
            )
        self.send_survey_by_id(survey.id, now)

    def send_survey_by_id(self, survey_id: str, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        survey = self.repo.get_survey(survey_id)
        if survey is None:
            raise ValueError(f"Survey not found: {survey_id}")
        if survey.status in TERMINAL_STATUSES:
            raise ValueError(f"Cannot send terminal survey {survey.id}.")

        self.repo.update_survey(
            survey.id,
            status=STATUS_COLLECTING,
            starts_at=survey.starts_at or now.isoformat(),
            closes_at=survey.closes_at or self.configured_closes_at(now),
        )
        survey = self.repo.get_survey(survey.id) or survey
        self.refresh_members()
        logger.info("Starting availability survey %s for %s/%s.", survey.id, survey.year, survey.month)
        failed_members = []
        for member in self.repo.list_survey_participants(survey.id):
            response = response_for_survey_member(self.repo, survey.id, member)
            self.persist_member_response(survey, response)
            if not can_receive_telegram_messages(member):
                continue
            try:
                logger.info("Sending availability request to %s (%s).", member.name, member.telegram_id)
                self.send_or_edit_member_form(survey, member, response)
            except TelegramApiError as exc:
                failed_members.append(f"{member.name}: {exc}")

        self.repo.set_active_survey_phase(
            survey,
            phase=STATUS_COLLECTING,
            collect_until=survey.closes_at,
            allowed_member_ids=[],
        )
        state = self.survey_state_for(survey)
        logger.info(
            "Availability survey %s for %s/%s is collecting; collect_until=%s.",
            survey.id,
            survey.year,
            survey.month,
            state.get("collect_until") or "month-start/all-responses",
        )
        if failed_members:
            message = (
                "Availability survey started, but these members could not be messaged:\n"
                + "\n".join(failed_members)
            )
            for admin_id in self.admin_telegram_ids():
                self.telegram.send_message(admin_id, message)

    def send_or_edit_member_form(
        self,
        survey: Survey,
        member: Member,
        response: AvailabilityResponse,
    ) -> None:
        canonical = self.repo.get_survey_message(survey.id, member.telegram_id)
        text = self.survey_form_text(survey, response)
        reply_markup = self.response_keyboard(response, survey.id)
        if canonical:
            try:
                self.telegram.edit_message_text(
                    chat_id=canonical["chat_id"],
                    message_id=canonical["message_id"],
                    text=text,
                    reply_markup=reply_markup,
                )
                return
            except TelegramApiError as exc:
                if self.is_ignorable_telegram_error(exc):
                    return
                logger.warning(
                    "Could not edit canonical form for survey %s member %s: %s",
                    survey.id,
                    member.name,
                    exc,
                )
        sent = self.telegram.send_message(
            chat_id=member.telegram_id,
            text=text,
            reply_markup=reply_markup,
        )
        message_id = telegram_message_id(sent)
        if message_id is not None:
            self.repo.record_survey_message(
                survey.id,
                member.telegram_id,
                member.telegram_id,
                message_id,
            )

    def ensure_month_started_preview(self, today: date | None = None) -> None:
        today = today or datetime.now(ZoneInfo(self.timezone_name)).date()
        year, month = current_calendar_month(today, self.calendar_type)
        if today != calendar_month_start(year, month, self.calendar_type):
            return

        if self.repo.get_monthly_run(self.calendar_type, year, month):
            return

        self.start_collecting(year, month)
        self.create_preview(year, month)

    def survey_by_callback_parts(self, data: str) -> tuple[Survey | None, str, list[str]]:
        parts = data.split(":")
        if len(parts) < 3:
            return None, "", []
        survey_id = parts[1]
        if survey_id in {"full", "custom", "weekdays", "dates", "back", "confirm", "w", "d"}:
            return None, "", []
        survey = self.repo.get_survey(survey_id)
        action = parts[2]
        return survey, action, parts[3:]

    def stale_survey_callback(self, callback: dict, message: str = "This survey has ended.") -> None:
        self.safe_answer_callback_query(callback["id"], message, show_alert=True)
        self.remove_callback_buttons(callback)

    def survey_is_current_and_editable(self, survey: Survey) -> bool:
        active = self.repo.get_active_survey(survey.kind)
        return (
            active is not None
            and active.id == survey.id
            and active.status in EDITABLE_SURVEY_STATUSES
        )

    def active_survey_state(self, survey: Survey | None = None) -> dict | None:
        if survey is not None:
            return self.survey_state_for(survey)
        production = self.active_survey_record(SURVEY_KIND_PRODUCTION)
        if production is not None:
            return self.survey_state_for(production)
        debug = self.active_survey_record(SURVEY_KIND_DEBUG)
        if debug is not None:
            return self.survey_state_for(debug)
        return None

    def survey_closed_message(self) -> str:
        if self.calendar_type == "jalali":
            return "مهلت ثبت availability تمام شده است. اگر نیاز به اصلاح داری با ادمین هماهنگ کن."
        return "The availability window is closed. Contact an admin if you need a change."

    def member_can_edit_survey(
        self,
        member: Member,
        survey: Survey,
        state: dict,
        now: datetime | None = None,
    ) -> bool:
        if self.collection_deadline_reached(now, survey=survey):
            return False
        phase = state.get("phase", STATUS_COLLECTING)
        if phase == STATUS_COLLECTING:
            return True
        if phase == STATUS_REVISION_REQUESTED:
            allowed = {int(item) for item in state.get("allowed_member_ids", [])}
            return member.telegram_id in allowed
        return False

    def response_keyboard(self, response: AvailabilityResponse, survey_id: str = "") -> dict:
        return main_availability_keyboard(response, survey_id=survey_id)

    def authorize_from_start(self, chat_id: int, user: dict | None) -> Member | None:
        existing_member = self.find_active_member(chat_id)
        display_name = telegram_display_name(user)
        if existing_member is not None:
            self.repo.mark_user_seen(chat_id, display_name)
            self.refresh_members()
            return self.find_active_member(chat_id)

        username = normalize_username(str((user or {}).get("username", "")))
        if not username:
            return None

        authorized = self.repo.authorize_user_from_start(username, chat_id, display_name)
        if authorized is None:
            return None

        self.refresh_members()
        return self.find_active_member(chat_id)

    def start_member_survey(self, chat_id: int, user: dict | None = None) -> None:
        self.refresh_members()
        member = self.authorize_from_start(chat_id, user)
        if member is None and not self.is_authorized_user(chat_id):
            self.telegram.send_message(chat_id, unauthorized_message(self.calendar_type))
            return

        member = member or self.find_active_member(chat_id)
        if member is None:
            self.telegram.send_message(chat_id, ACTIVE_MEMBER_ERROR)
            return

        survey = self.editable_survey_for_member(member)
        if survey is not None:
            response = response_for_survey_member(self.repo, survey.id, member)
            self.persist_member_response(survey, response)
            self.send_or_edit_member_form(survey, member, response)
            return

        if self.participant_in_active_survey(member):
            self.telegram.send_message(chat_id, self.survey_closed_message())
            return

        self.telegram.send_message(chat_id, "There is no active availability survey right now.")

    def participant_in_active_survey(self, member: Member) -> bool:
        for kind in (SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG):
            survey = self.repo.get_active_survey(kind)
            if survey is None:
                continue
            participant_ids = {
                participant.telegram_id
                for participant in self.repo.list_survey_participants(survey.id)
            }
            if member.telegram_id in participant_ids:
                return True
        return False

    def handle_private_text(self, chat_id: int, text: str = "", user: dict | None = None) -> None:
        if text.startswith("/start"):
            self.start_member_survey(chat_id, user)
            return

        if not self.is_authorized_user(chat_id):
            self.telegram.send_message(chat_id, unauthorized_message(self.calendar_type))
            return

        if self.is_today_command(text):
            self.send_today_bug_day(chat_id)
            return

        member = self.find_active_member(chat_id)
        if member is None:
            self.telegram.send_message(chat_id, ACTIVE_MEMBER_ERROR)
            return

        survey = self.editable_survey_for_member(member)
        if survey is not None:
            response = response_for_survey_member(self.repo, survey.id, member)
            self.persist_member_response(survey, response)
            self.send_or_edit_member_form(survey, member, response)
            return

        if self.participant_in_active_survey(member):
            self.telegram.send_message(chat_id, self.survey_closed_message())
            return

        self.telegram.send_message(chat_id, "There is no active availability survey right now.")

    def is_today_command(self, text: str) -> bool:
        normalized = text.strip().lower()
        return normalized in {"/today", "today", "امروز", "روز باگ امروز"}

    def send_today_bug_day(self, chat_id: int, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        self.refresh_members()
        entry = self.repo.get_schedule_entry(now.date().isoformat())
        if entry is None:
            self.telegram.send_message(chat_id, no_schedule_for_today_message(self.calendar_type))
            return

        main_name, backup_name = entry
        members_by_name = self.member_by_name()
        self.telegram.send_message(
            chat_id,
            today_bug_day_message(
                main_member=members_by_name.get(main_name),
                backup_member=members_by_name.get(backup_name),
                main_name=main_name,
                backup_name=backup_name,
                calendar_type=self.calendar_type,
            ),
        )

    def handle_availability_callback(self, callback: dict) -> None:
        self.refresh_members()
        survey, action, args = self.survey_by_callback_parts(callback.get("data", ""))
        if survey is None:
            self.stale_survey_callback(callback, "This survey has ended.")
            return

        user = callback["from"]
        telegram_id = int(user["id"])

        member = self.find_active_member(telegram_id)
        if member is None:
            self.safe_answer_callback_query(callback["id"], unauthorized_message(self.calendar_type))
            return
        if not self.survey_is_current_and_editable(survey):
            active = self.repo.get_active_survey(survey.kind)
            if (
                survey.status in TERMINAL_STATUSES
                or active is None
                or active.id != survey.id
            ):
                self.stale_survey_callback(callback, "This survey has ended.")
            else:
                self.stale_survey_callback(callback, self.survey_closed_message())
            return

        state = self.survey_state_for(survey)
        if not self.member_can_edit_survey(member, survey, state):
            self.stale_survey_callback(callback, self.survey_closed_message())
            return

        response = response_for_survey_member(self.repo, survey.id, member)

        if action == "full":
            response.unavailable_days = []
            response.unavailable_weekdays = []
            response.confirmed = True
            response.mode = "full"
            reply_markup = self.response_keyboard(response, survey.id)
        elif action == "custom":
            response.mode = "custom"
            response.confirmed = False
            reply_markup = self.response_keyboard(response, survey.id)
        elif action == "weekdays":
            if response.mode == "full":
                self.safe_answer_callback_query(callback["id"], "Use Change availability first.")
                return
            reply_markup = weekdays_keyboard(response, survey.calendar_type, survey.id)
        elif action == "dates":
            if response.mode == "full":
                self.safe_answer_callback_query(callback["id"], "Use Change availability first.")
                return
            reply_markup = dates_keyboard(response, survey.year, survey.month, survey.calendar_type, survey.id)
        elif action == "back":
            reply_markup = self.response_keyboard(response, survey.id)
        elif action == "confirm":
            response.confirmed = True
            reply_markup = self.response_keyboard(response, survey.id)
        elif action == "noop":
            self.safe_answer_callback_query(callback["id"])
            return
        elif action == "w" and len(args) == 2:
            response.mode = "custom"
            verb, weekday = args
            if verb == "remove" and weekday in response.unavailable_weekdays:
                response.unavailable_weekdays.remove(weekday)
            elif verb == "add" and weekday not in response.unavailable_weekdays:
                response.unavailable_weekdays.append(weekday)
            response.confirmed = False
            reply_markup = weekdays_keyboard(response, survey.calendar_type, survey.id)
        elif action == "d" and len(args) == 2:
            response.mode = "custom"
            verb, raw_day = args
            day = int(raw_day)
            if verb == "remove" and day in response.unavailable_days:
                response.unavailable_days.remove(day)
            elif verb == "add" and day not in response.unavailable_days:
                response.unavailable_days.append(day)
            response.confirmed = False
            reply_markup = dates_keyboard(response, survey.year, survey.month, survey.calendar_type, survey.id)
        else:
            self.safe_answer_callback_query(callback["id"])
            return

        self.safe_answer_callback_query(callback["id"])

        try:
            self.persist_member_response(survey, response)
            self.telegram.edit_message_text(
                chat_id=callback["message"]["chat"]["id"],
                message_id=callback["message"]["message_id"],
                text=self.survey_form_text(survey, response),
                reply_markup=reply_markup,
            )
            self.repo.record_survey_message(
                survey.id,
                telegram_id,
                int(callback["message"]["chat"]["id"]),
                int(callback["message"]["message_id"]),
            )
        except TelegramApiError as exc:
            if not self.is_ignorable_telegram_error(exc):
                logger.warning("Availability callback failed for %s: %s", member.name, exc)
                self.safe_send_message(
                    chat_id=telegram_id,
                    text=TEMPORARY_TELEGRAM_ERROR,
                )
                return

    def required_response_members(self, survey: Survey | None = None) -> list[Member]:
        if survey is None:
            survey = self.active_survey_record(SURVEY_KIND_PRODUCTION)
        if survey is None:
            return self.messageable_active_members()
        state = self.survey_state_for(survey)
        phase = state.get("phase", STATUS_COLLECTING)
        members = [
            member
            for member in self.repo.list_survey_participants(survey.id)
            if can_receive_telegram_messages(member)
        ]
        if phase != STATUS_REVISION_REQUESTED:
            return members
        allowed = {int(item) for item in state.get("allowed_member_ids", [])}
        return [member for member in members if member.telegram_id in allowed]

    def all_members_responded(self, year: int, month: int, survey: Survey | None = None) -> bool:
        survey = survey or self.active_or_latest_survey_for_month(year, month)
        if survey is None:
            responses = self.repo.list_responses(self.calendar_type, year, month)
        else:
            responses = self.responses_for_survey(survey)
        for member in self.required_response_members(survey):
            response = responses.get(member.telegram_id)
            if response is None or not response.confirmed:
                return False
        return True

    def deadline_reached(self, today: date, year: int, month: int) -> bool:
        start = calendar_month_start(year, month, self.calendar_type)
        return today >= start

    def collection_deadline_reached(
        self,
        now: datetime | None = None,
        *,
        survey: Survey | None = None,
    ) -> bool:
        survey = survey or self.active_survey_record(SURVEY_KIND_PRODUCTION)
        if survey is None:
            return False
        state = self.survey_state_for(survey)
        collect_until_raw = state.get("collect_until") or survey.closes_at
        if not collect_until_raw:
            return False

        now = now or datetime.now(ZoneInfo(self.timezone_name))
        collect_until = parse_stored_datetime(collect_until_raw, now)
        if collect_until is None:
            return False
        return now >= collect_until

    def maybe_create_preview(self, today: date | datetime | None = None) -> None:
        survey = self.active_survey_record(SURVEY_KIND_PRODUCTION)
        if not survey:
            return

        if survey.status in {
            STATUS_SCHEDULED,
            STATUS_PENDING_ADMIN_REVIEW,
            STATUS_BLOCKED,
            STATUS_PUBLISHING,
            STATUS_APPROVED,
            STATUS_CANCELED,
        }:
            return

        if isinstance(today, datetime):
            now = today.astimezone(ZoneInfo(self.timezone_name))
            today = now.date()
        else:
            now = datetime.now(ZoneInfo(self.timezone_name))
            today = today or now.date()
        if (
            not self.all_members_responded(survey.year, survey.month, survey)
            and not self.deadline_reached(today, survey.year, survey.month)
            and not self.collection_deadline_reached(now)
        ):
            return

        self.create_preview(survey.year, survey.month, survey_id=survey.id)

    def create_preview(self, year: int, month: int, survey_id: str | None = None) -> None:
        self.refresh_members()
        survey = self.repo.get_survey(survey_id) if survey_id else self.active_or_latest_survey_for_month(year, month)
        if survey is None:
            survey = self.create_survey_record(
                year,
                month,
                datetime.now(ZoneInfo(self.timezone_name)),
                status=STATUS_COLLECTING,
                created_by="legacy-preview",
            )
        base_config = {
            "calendar": survey.calendar_type,
            "year": survey.year,
            "month": survey.month,
            "people": [],
            "history": {},
            "holidays": normalize_holidays(
                self.runtime.holidays,
                survey.year,
                survey.month,
                survey.calendar_type,
                None,
                None,
            ),
        }
        participants = self.repo.list_survey_participants(survey.id)
        responses = self.responses_with_survey_defaults(
            self.responses_for_survey(survey),
            participants,
        )
        schedule_config = build_schedule_config(
            base_config=base_config,
            members=participants,
            responses=responses,
            calendar_type=survey.calendar_type,
            year=survey.year,
            month=survey.month,
        )
        history = load_history_from_db(
            self.settings.database_path,
            target_year=survey.year,
            target_month=survey.month,
            people_names=[schedule_person_key(member) for member in schedule_members(participants)],
        )
        schedule, stats = build_schedule(schedule_config, db_history=history)
        review = build_review_report(
            schedule=schedule,
            stats=stats,
            members=participants,
            responses=responses,
            calendar_type=survey.calendar_type,
            year=survey.year,
            month=survey.month,
        )
        status = STATUS_PENDING_ADMIN_REVIEW

        output_dir = self.resolve_output_dir()
        image_path = output_dir / (
            f"adhoc_schedule_{survey.year}_{survey.month:02d}_{survey.id}_{uuid4().hex[:8]}.jpg"
        )
        export_image_calendar(
            schedule,
            survey.year,
            survey.month,
            survey.calendar_type,
            image_path,
            stats=stats,
        )

        caption = preview_photo_caption(survey.year, survey.month, survey.calendar_type, review)
        report_message = preview_report_message(
            survey.year,
            survey.month,
            survey.calendar_type,
            stats,
            review,
        )
        approval_markup = admin_approval_keyboard(
            survey.year,
            survey.month,
            survey.calendar_type,
            status,
            has_flagged_members=bool(review["flagged_member_ids"]),
            survey_id=survey.id,
        )

        # Persist review state before Telegram fan-out so one bad admin cannot wedge retries.
        if not self.repo.transition_survey(
            survey.id,
            from_statuses=PREVIEW_SOURCE_STATUSES,
            to_status=status,
            preview_sent_at=utc_now(),
            image_path=str(image_path),
            schedule_json=json.dumps(schedule),
            stats_json=json.dumps(stats_to_plain_dict(stats)),
            review_json=json.dumps(review),
        ):
            current = self.repo.get_survey(survey.id)
            raise RuntimeError(
                f"Cannot create preview from status "
                f"{current.status if current else 'missing'} for survey {survey.id}."
            )
        survey = self.repo.get_survey(survey.id) or survey
        self.repo.set_active_survey_phase(
            survey,
            phase=status,
            collect_until=survey.closes_at,
            allowed_member_ids=[],
        )

        failed_admins: list[str] = []
        delivered_admin_ids: list[int] = []
        admin_ids = self.admin_telegram_ids()
        if not admin_ids:
            failed_admins.append("no admin telegram destinations configured")
        for admin_id in admin_ids:
            try:
                self.telegram.send_photo(
                    chat_id=admin_id,
                    photo_path=image_path,
                    caption=caption,
                    reply_markup=approval_markup,
                )
                self.safe_send_long_message(admin_id, report_message)
                delivered_admin_ids.append(admin_id)
            except TelegramApiError as exc:
                logger.warning("Preview delivery failed for admin %s: %s", admin_id, exc)
                failed_admins.append(f"{admin_id}: {exc}")

        if failed_admins and delivered_admin_ids:
            failure_text = (
                "Preview was saved for review, but delivery failed for:\n"
                + "\n".join(failed_admins)
            )
            for admin_id in delivered_admin_ids:
                self.safe_send_message(admin_id, failure_text)
        elif failed_admins and not delivered_admin_ids:
            delivery_note = (
                "Preview saved but no admin could be reached: " + "; ".join(failed_admins)
            )
            logger.error("Preview for survey %s: %s", survey.id, delivery_note)
            review_payload = dict(review)
            review_payload["preview_delivery_failed"] = True
            review_payload["preview_delivery_errors"] = list(failed_admins)
            warnings = list(review_payload.get("warnings") or [])
            if delivery_note not in warnings:
                warnings.append(delivery_note)
            review_payload["warnings"] = warnings
            review_payload["status"] = "needs_review"
            self.repo.update_survey(survey.id, review_json=json.dumps(review_payload))

    def resolve_output_dir(self) -> Path:
        configured = Path(self.runtime.output_dir)
        try:
            return self.ensure_writable_dir(configured)
        except OSError as exc:
            fallback = self.settings.database_path.parent / "output"
            logger.warning(
                "Configured output_dir is not writable: %s. Falling back to %s",
                exc,
                fallback,
            )
            return self.ensure_writable_dir(fallback)

    def ensure_writable_dir(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".adhoc_write_test_{uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return path

    def handle_admin_callback(self, callback: dict) -> None:
        self.refresh_members()
        user_id = int(callback["from"]["id"])
        if not self.is_admin(user_id):
            self.safe_answer_callback_query(callback["id"], "Admins only.")
            return

        parts = callback["data"].split(":")
        action = parts[1]
        survey: Survey | None = None
        if len(parts) == 3:
            survey = self.repo.get_survey(parts[2])
        elif len(parts) >= 5:
            calendar_type = parts[2]
            year = int(parts[3])
            month = int(parts[4])
            # Legacy calendar:year:month callbacks always target production.
            survey = self.repo.get_latest_survey_for_month(
                calendar_type,
                year,
                month,
                kind=SURVEY_KIND_PRODUCTION,
            )

        if action not in {"regen", "cancel", "approve", "correct", "reopen", "close", "restart"}:
            self.safe_answer_callback_query(callback["id"])
            return

        self.safe_answer_callback_query(callback["id"])

        if survey is None:
            self.safe_send_message(user_id, "No survey found for this action.")
            return

        calendar_type = survey.calendar_type
        year = survey.year
        month = survey.month
        run = {
            "status": survey.status,
            "review_json": survey.review_json,
            "schedule_json": survey.schedule_json,
            "stats_json": survey.stats_json,
            "image_path": survey.image_path,
        }
        if survey.status == STATUS_APPROVED and action != "approve":
            self.safe_send_message(user_id, "This schedule is already approved.")
            return
        if survey.status == STATUS_CANCELED and action not in {"cancel", "restart"}:
            self.safe_send_message(user_id, "This schedule was canceled.")
            return

        if action == "restart":
            now = datetime.now(ZoneInfo(self.timezone_name))
            if survey.status == STATUS_APPROVED:
                self.safe_send_message(user_id, "This schedule is already approved.")
                return
            if survey.status != STATUS_CANCELED:
                if not self.repo.transition_survey(
                    survey.id,
                    from_statuses=CANCELABLE_STATUSES,
                    to_status=STATUS_CANCELED,
                ):
                    current = self.repo.get_survey(survey.id)
                    self.safe_send_message(
                        user_id,
                        f"Could not restart; survey is now "
                        f"{current.status if current else 'missing'}.",
                    )
                    return
            new_survey = self.create_survey_record(
                year,
                month,
                now,
                starts_at=now,
                status=STATUS_COLLECTING,
                created_by=f"admin:{user_id}:restart:{survey.id}",
                kind=survey.kind,
                participants=self.repo.list_survey_participants(survey.id),
            )
            self.send_survey_by_id(new_survey.id, now)
            self.remove_callback_buttons(callback)
            self.safe_send_message(
                user_id,
                f"{self.survey_restarted_message(year, month, calendar_type)}\nSurvey: {new_survey.id}",
            )
            return

        if action == "close":
            try:
                self.create_preview(year, month, survey_id=survey.id)
                self.remove_callback_buttons(callback)
            except TelegramApiError as exc:
                logger.warning("Closing revision failed for %s/%s: %s", year, month, exc)
                self.safe_send_message(user_id, TEMPORARY_TELEGRAM_ERROR)
            except RuntimeError as exc:
                self.safe_send_message(user_id, str(exc))
            return

        if action == "regen":
            try:
                self.create_preview(year, month, survey_id=survey.id)
                self.remove_callback_buttons(callback)
            except TelegramApiError as exc:
                logger.warning("Preview regeneration failed for %s/%s: %s", year, month, exc)
                self.safe_send_message(user_id, TEMPORARY_TELEGRAM_ERROR)
            except RuntimeError as exc:
                self.safe_send_message(user_id, str(exc))
            return

        if action == "cancel":
            if survey.status == STATUS_APPROVED:
                self.safe_send_message(user_id, "This schedule is already approved.")
                return
            if not self.repo.transition_survey(
                survey.id,
                from_statuses=CANCELABLE_STATUSES,
                to_status=STATUS_CANCELED,
            ):
                current = self.repo.get_survey(survey.id)
                self.safe_send_message(
                    user_id,
                    f"Could not cancel; survey is now {current.status if current else 'missing'}.",
                )
                return
            self.repo.clear_active_survey_phase(survey.kind)
            self.remove_callback_buttons(callback)
            self.safe_send_message(
                user_id,
                self.canceled_message(year, month, calendar_type),
                reply_markup=admin_canceled_keyboard(year, month, calendar_type, survey.id),
            )
            return

        if action in {"correct", "reopen"}:
            if not survey.review_json:
                self.safe_send_message(user_id, "No review report found for corrections.")
                return

            review = json.loads(survey.review_json)
            if action == "correct":
                all_flagged_names = review.get("flagged_member_names", [])
                member_ids = [
                    member_id
                    for member_id in review.get("flagged_member_ids", [])
                    if self.member_can_receive_revision(member_id, survey)
                ]
                if not member_ids:
                    self.safe_send_message(
                        user_id,
                        self.no_reachable_flagged_members_message(all_flagged_names),
                    )
                    return
            else:
                member_ids = [
                    member.telegram_id
                    for member in self.messageable_survey_participants(survey)
                ]

            try:
                self.start_revision(survey, member_ids)
            except RuntimeError as exc:
                self.safe_send_message(user_id, str(exc))
                return
            self.send_revision_requests(year, month, member_ids, survey.id)
            self.remove_callback_buttons(callback)
            self.safe_send_message(
                user_id,
                self.revision_started_message(member_ids, survey),
                reply_markup=admin_revision_keyboard(year, month, calendar_type, survey.id),
            )
            return

        survey = self.repo.get_survey(survey.id) or survey
        if not survey.schedule_json:
            self.safe_send_message(user_id, "No preview found for approval.")
            return
        if survey.status == STATUS_CANCELED:
            self.safe_send_message(user_id, "This schedule was canceled.")
            return
        if survey.status == STATUS_APPROVED:
            self.safe_send_message(user_id, "This schedule is already approved.")
            return
        if survey.status == STATUS_REVISION_REQUESTED:
            self.safe_send_message(
                user_id,
                "Cannot approve while revision is in progress. Close revision first.",
            )
            return
        try:
            pin_error = self.publish_survey(survey)
            self.remove_callback_buttons(callback)
            if pin_error is None:
                admin_message = approval_sent_message(year, month, calendar_type)
            else:
                admin_message = approval_posted_pin_failed_message(
                    year,
                    month,
                    calendar_type,
                    str(pin_error),
                )
            self.safe_send_message(user_id, admin_message)
        except TelegramApiError as exc:
            logger.warning("Posting approved schedule failed for %s/%s: %s", year, month, exc)
            self.safe_send_message(user_id, TEMPORARY_TELEGRAM_ERROR)
        except RuntimeError as exc:
            logger.warning("Posting approved schedule failed for %s/%s: %s", year, month, exc)
            self.safe_send_message(user_id, str(exc))

    def ensure_debug_publish_destination_allowed(self, survey: Survey) -> None:
        if survey.kind != SURVEY_KIND_DEBUG:
            return
        self.refresh_runtime()
        if self.runtime.allow_production_destination:
            return
        raise RuntimeError(
            "Debug publish to the configured production Telegram destination is blocked. "
            "Enable it in DB runtime settings with --allow-production-destination "
            "(or --set-allow-production-destination), then approve/publish again."
        )

    def finalize_published_survey(self, survey: Survey) -> None:
        if not self.repo.complete_publish(survey.id):
            current = self.repo.get_survey(survey.id)
            if current is None or current.status != STATUS_APPROVED:
                raise RuntimeError(
                    f"Failed to finalize publish for survey {survey.id} "
                    f"(status={current.status if current else 'missing'})."
                )

    def publish_survey(self, survey: Survey) -> TelegramApiError | None:
        """Post survey schedule to the configured group and mark approved."""
        if not survey.schedule_json:
            raise RuntimeError("No preview found for approval.")
        if survey.status == STATUS_APPROVED:
            raise RuntimeError("This schedule is already approved.")
        if survey.status == STATUS_CANCELED:
            raise RuntimeError("This schedule was canceled.")
        if survey.status == STATUS_REVISION_REQUESTED:
            raise RuntimeError("Cannot approve while revision is in progress.")

        self.ensure_debug_publish_destination_allowed(survey)

        if survey.status == STATUS_PUBLISHING:
            if survey.group_sent_at:
                self.finalize_published_survey(survey)
                return None
            raise RuntimeError(
                f"Survey {survey.id} is stuck in publishing without a confirmed Telegram "
                "delivery receipt. Check the group first, then either cancel/restart it or "
                "manually activate/finalize it with local DB commands."
            )

        if not self.repo.transition_survey(
            survey.id,
            from_statuses=APPROVABLE_STATUSES,
            to_status=STATUS_PUBLISHING,
        ):
            current = self.repo.get_survey(survey.id)
            raise RuntimeError(
                f"Cannot publish from status {current.status if current else 'missing'}."
            )

        image_path = Path(survey.image_path or "")
        caption = group_schedule_caption(survey.year, survey.month, survey.calendar_type)
        try:
            group_chat_id, topic_id = self.telegram_destination()
            sent_message = self.telegram.send_photo(
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                photo_path=image_path,
                caption=caption,
            )
        except Exception as exc:
            # Keep publishing. Reverting would allow a blind retry to duplicate a
            # Telegram post that may already have succeeded despite a timeout.
            logger.error(
                "Publish send failed for survey %s; leaving status=publishing "
                "(retry finalize without re-send): %s",
                survey.id,
                exc,
            )
            raise RuntimeError(
                f"Publish send failed for survey {survey.id}; left in publishing. "
                "Do not auto-approve it; verify the group post first, then resolve it manually "
                "if needed."
            ) from exc

        self.repo.update_survey(survey.id, group_sent_at=utc_now())
        message_id = telegram_message_id(sent_message)
        pin_error = None
        if message_id is not None:
            pin_error = self.safe_pin_chat_message(group_chat_id, message_id)

        survey = self.repo.get_survey(survey.id) or survey
        self.finalize_published_survey(survey)
        return pin_error

    def canceled_message(self, year: int, month: int, calendar_type: str) -> str:
        if calendar_type == "jalali":
            return f"برنامه‌ی {month_label(year, month, calendar_type)} کنسل شد."
        return f"{month_label(year, month, calendar_type)} schedule was canceled."

    def survey_restarted_message(self, year: int, month: int, calendar_type: str) -> str:
        if calendar_type == "jalali":
            return f"نظرسنجی {month_label(year, month, calendar_type)} دوباره شروع شد."
        return f"{month_label(year, month, calendar_type)} survey was restarted."

    def no_reachable_flagged_members_message(self, flagged_names: list[str]) -> str:
        names = format_names(flagged_names, self.calendar_type)
        if self.calendar_type == "jalali":
            return (
                f"افراد نیازمند اصلاح: {names}\n"
                "در حالت فعلی نمی‌شود به هیچ‌کدام پیام داد. "
                "می‌توانی Reopen for everyone را بزنی یا کانفیگ اعضا را بررسی کنی."
            )
        return (
            f"Members needing corrections: {names}\n"
            "None of them can be messaged in the current mode. "
            "Use Reopen for everyone or check the member config."
        )

    def revision_started_message(
        self,
        member_ids: list[int],
        survey: Survey | None = None,
    ) -> str:
        names = format_names(self.member_names_for_ids(member_ids, survey), self.calendar_type)
        if self.calendar_type == "jalali":
            return f"پنجره‌ی اصلاح برای این افراد باز شد: {names}"
        return f"Revision window opened for: {names}"

    def send_revision_requests(
        self,
        year: int,
        month: int,
        member_ids: list[int],
        survey_id: str | None = None,
    ) -> None:
        survey = self.repo.get_survey(survey_id) if survey_id else self.active_or_latest_survey_for_month(year, month)
        if survey is None:
            return
        member_ids_set = set(member_ids)
        for member in self.repo.list_survey_participants(survey.id):
            if member.telegram_id not in member_ids_set:
                continue
            if not can_receive_telegram_messages(member):
                continue
            response = response_for_survey_member(self.repo, survey.id, member)
            try:
                self.telegram.send_message(
                    chat_id=member.telegram_id,
                    text=self.revision_request_message(year, month),
                    reply_markup=self.response_keyboard(response, survey.id),
                )
            except TelegramApiError as exc:
                logger.warning("Revision request failed for %s: %s", member.name, exc)

    def revision_request_message(self, year: int, month: int) -> str:
        if self.calendar_type == "jalali":
            return (
                f"برنامه‌ی {month_label(year, month, self.calendar_type)} نیاز به اصلاح دارد.\n"
                "اگر روزها درست است Confirm را بزن؛ اگر اشتباه است اصلاح کن."
            )
        return (
            f"The {month_label(year, month, self.calendar_type)} schedule needs review.\n"
            "Confirm unchanged if your dates are correct, or update them."
        )

    def send_daily_reminder(self, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        self.refresh_members()
        target_time = day_time.fromisoformat(self.runtime.daily_reminder_time)
        if now.time().replace(second=0, microsecond=0) < target_time:
            return

        work_date = now.date().isoformat()
        # Claim before send so retries/restarts cannot duplicate the reminder.
        if not self.repo.claim_daily_reminder(work_date):
            return

        entry = self.repo.get_schedule_entry(work_date)
        if entry is None:
            self.repo.clear_daily_reminder(work_date)
            return

        main_name, backup_name = entry
        members_by_name = self.member_by_name()
        main_member = members_by_name.get(main_name)
        backup_member = members_by_name.get(backup_name)
        main_text = mention(main_member) if main_member else main_name
        backup_text = mention(backup_member) if backup_member else backup_name

        try:
            group_chat_id, topic_id = self.telegram_destination()
        except RuntimeError as exc:
            logger.warning("Daily reminder skipped: %s", exc)
            self.repo.clear_daily_reminder(work_date)
            return

        try:
            self.telegram.send_message(
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                text=(
                    f"Good morning {main_text}.\n"
                    "Today is your bug day.\n\n"
                    f"{backup_text} is your backup.\n"
                    "Have a good day."
                ),
            )
        except TelegramApiError:
            self.repo.clear_daily_reminder(work_date)
            raise

    def resume_collecting_form_delivery(self) -> None:
        for kind in (SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG):
            survey = self.active_survey_record(kind)
            if survey is None or survey.status != STATUS_COLLECTING:
                continue
            for member in self.messageable_survey_participants(survey):
                if self.repo.get_survey_message(survey.id, member.telegram_id):
                    continue
                response = response_for_survey_member(self.repo, survey.id, member)
                self.persist_member_response(survey, response)
                try:
                    logger.info(
                        "Resuming availability form for survey %s member %s (%s).",
                        survey.id,
                        member.name,
                        member.telegram_id,
                    )
                    self.send_or_edit_member_form(survey, member, response)
                except TelegramApiError as exc:
                    logger.warning(
                        "Resume form delivery failed for %s on survey %s: %s",
                        member.name,
                        survey.id,
                        exc,
                    )

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            callback = update["callback_query"]
            data = callback.get("data", "")
            if data.startswith("av:"):
                self.handle_availability_callback(callback)
            elif data.startswith("admin:"):
                self.handle_admin_callback(callback)
            else:
                self.safe_answer_callback_query(callback["id"])
            return

        message = update.get("message")
        if not message:
            return

        chat = message.get("chat", {})
        if chat.get("type") == "private":
            self.handle_private_text(
                int(chat["id"]),
                message.get("text", ""),
                user=message.get("from", {}),
            )

    def run_once(self) -> None:
        self.refresh_runtime()
        now = datetime.now(ZoneInfo(self.timezone_name))
        today = now.date()
        for task in (
            lambda now=now: self.ensure_survey_started(now),
            self.resume_collecting_form_delivery,
            lambda today=today: self.ensure_month_started_preview(today),
            lambda now=now: self.maybe_create_preview(now),
            self.send_daily_reminder,
        ):
            try:
                task()
            except Exception:
                task_name = getattr(task, "__name__", task.__class__.__name__)
                logger.exception("Scheduled task %s failed", task_name)

    def should_run_scheduled_work(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        if self.next_scheduled_work_at is None or now >= self.next_scheduled_work_at:
            self.next_scheduled_work_at = now + timedelta(seconds=self.scheduled_work_interval_seconds)
            return True
        return False

    def run_loop_once(self, offset: int | None) -> int | None:
        try:
            updates = self.telegram.get_updates(
                offset=offset,
                timeout=self.runtime.poll_interval_seconds,
            )
        except TelegramApiError:
            logger.exception("Fetching Telegram updates failed")
            return offset

        for update in updates:
            update_id = update.get("update_id")
            next_offset = update_id + 1 if isinstance(update_id, int) else offset
            try:
                self.handle_update(update)
            except Exception:
                logger.exception("Handling Telegram update failed: %s", update)
            if next_offset is not None:
                offset = next_offset
                self.repo.set_update_offset(offset)

        if self.should_run_scheduled_work():
            self.run_once()

        return offset

    def run_forever(self) -> None:
        logger.warning(
            "Starting long-polling bot. Do not run another process with the same "
            "Telegram bot token (including timed/debug profiles); getUpdates conflicts."
        )
        offset = self.repo.get_update_offset()
        while True:
            offset = self.run_loop_once(offset)
            time.sleep(1)

    def is_ignorable_telegram_error(self, exc: TelegramApiError) -> bool:
        if exc.ignorable:
            return True
        message = str(exc).lower()
        return "message is not modified" in message or "query is too old" in message

    def safe_answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
        *,
        show_alert: bool = False,
    ) -> None:
        try:
            self.telegram.answer_callback_query(
                callback_query_id,
                text=text,
                show_alert=show_alert,
            )
        except TelegramApiError as exc:
            if not self.is_ignorable_telegram_error(exc):
                logger.warning("answerCallbackQuery failed: %s", exc)

    def safe_send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        try:
            self.telegram.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            )
        except TelegramApiError as exc:
            logger.warning("Fallback sendMessage failed: %s", exc)

    def safe_send_long_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: int | None = None,
    ) -> None:
        for chunk in split_telegram_text(text):
            self.safe_send_message(
                chat_id=chat_id,
                text=chunk,
                message_thread_id=message_thread_id,
            )

    def safe_pin_chat_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> TelegramApiError | None:
        try:
            self.telegram.pin_chat_message(
                chat_id=chat_id,
                message_id=message_id,
            )
            return None
        except TelegramApiError as exc:
            logger.warning("pinChatMessage failed: %s", exc)
            return exc

    def remove_callback_buttons(self, callback: dict) -> None:
        message = callback.get("message")
        if not message:
            return
        try:
            self.telegram.edit_message_reply_markup(
                chat_id=message["chat"]["id"],
                message_id=message["message_id"],
                reply_markup=None,
            )
        except TelegramApiError as exc:
            if not self.is_ignorable_telegram_error(exc):
                logger.warning("editMessageReplyMarkup failed: %s", exc)


def anchor_scheduled_survey_start(
    bot: AdhocTelegramBot,
    anchor: datetime | None = None,
) -> None:
    raw = bot.runtime.survey_start_at.strip()
    if not raw or not is_relative_schedule(raw):
        return

    anchor = anchor or datetime.now(ZoneInfo(bot.timezone_name))
    scheduled = parse_scheduled_datetime(raw, anchor)
    if scheduled is None:
        return

    bot.repo.set_state(
        "scheduled_survey_start",
        {"raw": raw, "start_at": scheduled.isoformat(), "consumed": False},
    )


def reset_target_month_state(
    bot: AdhocTelegramBot,
    *,
    kind: str = SURVEY_KIND_PRODUCTION,
) -> None:
    year, month = bot.target_month()
    kind = bot.repo.normalize_survey_kind(kind)
    survey = bot.repo.get_latest_survey_for_month(
        bot.calendar_type,
        year,
        month,
        kind=kind,
    )
    if survey and survey.status != STATUS_APPROVED:
        bot.repo.transition_survey(
            survey.id,
            from_statuses=CANCELABLE_STATUSES,
            to_status=STATUS_CANCELED,
        )
        bot.repo.delete_survey_responses(survey.id)
        bot.repo.clear_active_survey_phase(kind)
    if kind == SURVEY_KIND_PRODUCTION:
        bot.repo.delete_month_responses(bot.calendar_type, year, month)
        bot.repo.delete_monthly_run(bot.calendar_type, year, month)
        bot.repo.delete_state("scheduled_survey_start")
        anchor_scheduled_survey_start(bot)
    logger.info("Reset target month state for %s/%s kind=%s.", year, month, kind)


def local_datetime_value(raw_value: str, timezone_name: str) -> str:
    now = datetime.now(ZoneInfo(timezone_name))
    parsed = parse_scheduled_datetime(raw_value, now)
    if parsed is None:
        raise ValueError(f"Invalid datetime value: {raw_value}")
    return parsed.isoformat()


def activate_survey_schedule(repo: BotRepository, settings: InfraSettings, survey: Survey) -> None:
    if not survey.schedule_json:
        raise SystemExit(f"Survey {survey.id} has no schedule_json to activate.")
    schedule = json.loads(survey.schedule_json)
    stats = json.loads(survey.stats_json or "{}")
    save_month_to_db(
        settings.database_path,
        survey.year,
        survey.month,
        schedule,
        stats,
    )
    repo.set_active_schedule_source(survey.year, survey.month)


def survey_month_from_args(
    args: argparse.Namespace,
    runtime: RuntimeSettings,
) -> tuple[int, int]:
    if args.target_month:
        year, month = args.target_month.split("-", maxsplit=1)
        return int(year), int(month)
    if runtime.target_year is not None and runtime.target_month is not None:
        return runtime.target_year, runtime.target_month
    now = datetime.now(ZoneInfo(runtime.timezone)).date()
    return survey_target_month(now, runtime.calendar, runtime.survey_days_before_month)


def resolve_local_survey_participants(
    repo: BotRepository,
    args: argparse.Namespace,
) -> list[Member]:
    if args.participants:
        return parse_survey_participants(args.participants, repo)
    return repo.list_schedule_members(active_only=True)


def create_local_survey(
    repo: BotRepository,
    args: argparse.Namespace,
) -> Survey:
    runtime = repo.get_runtime_settings()
    year, month = survey_month_from_args(args, runtime)
    now = datetime.now(ZoneInfo(runtime.timezone))
    starts_at = (
        parse_scheduled_datetime(args.survey_start_at, now)
        if args.survey_start_at
        else now
    )
    if starts_at is None:
        starts_at = now
    closes_raw = args.survey_collect_for or runtime.survey_collect_for
    closes_at = (
        parse_scheduled_datetime(closes_raw, starts_at)
        if closes_raw
        else None
    )
    kind = getattr(args, "survey_kind", SURVEY_KIND_PRODUCTION) or SURVEY_KIND_PRODUCTION
    created_by = "local-cli-debug" if kind == SURVEY_KIND_DEBUG else "local-cli"
    skip_collect = bool(getattr(args, "direct_preview", False) or getattr(args, "publish_now", False))
    status = STATUS_COLLECTING
    if starts_at > now and not args.send_now and not skip_collect:
        status = STATUS_SCHEDULED
    return repo.create_survey(
        survey_id=survey_identity(runtime.calendar, year, month),
        calendar_type=runtime.calendar,
        year=year,
        month=month,
        status=status,
        starts_at=starts_at.isoformat(),
        closes_at=closes_at.isoformat() if closes_at else "",
        created_by=created_by,
        participants=resolve_local_survey_participants(repo, args),
        kind=kind,
    )


def handle_user_admin_command(repo: BotRepository, args: argparse.Namespace) -> bool:
    if args.upsert_user:
        display_name = args.user_display_name or normalize_username(args.upsert_user)
        participates_in_schedule = None
        if args.user_manager_only:
            participates_in_schedule = False
        if args.user_schedule_participant:
            participates_in_schedule = True
        repo.upsert_user(
            username=args.upsert_user,
            display_name=display_name,
            role=args.user_role,
            access_level=args.user_access_level,
            active=not args.user_inactive,
            participates_in_schedule=participates_in_schedule,
            telegram_id=args.user_telegram_id,
        )
        print(
            "User saved: "
            f"@{normalize_username(args.upsert_user)} "
            f"({display_name}, {args.user_access_level})"
        )
        return True

    if args.list_users:
        users = repo.list_members(active_only=False)
        if not users:
            print("No bot users found.")
            return True
        for user in users:
            telegram_id = user.telegram_id or "no telegram id yet"
            active = "active" if user.active else "inactive"
            participant = "schedule" if user.participates_in_schedule else "manager-only"
            print(
                f"@{user.username} | {user.name} | {user.role or '-'} | "
                f"{user.access_level} | {participant} | {active} | {telegram_id}"
            )
        return True

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Adhoc Assistant Telegram bot.")
    parser.add_argument(
        "--database",
        help="SQLite database path (infra only). Overrides DATABASE_URL / SQLITE_PATH.",
    )
    parser.add_argument(
        "--survey-start-at",
        help=(
            "One-shot start time for --create-survey only. "
            "Does not change DB runtime_settings; use --set-survey-start-at for that."
        ),
    )
    parser.add_argument(
        "--survey-collect-for",
        help=(
            "One-shot collect window for --create-survey only. "
            "Does not change DB runtime_settings; use --set-survey-collect-for for that."
        ),
    )
    parser.add_argument(
        "--revision-collect-for",
        help=(
            "Deprecated no-op for bot policy. "
            "Use --set-revision-collect-for to change DB runtime_settings."
        ),
    )
    parser.add_argument(
        "--target-month",
        help=(
            "One-shot target month for --create-survey only (YYYY-MM). "
            "Does not change DB runtime_settings; use --set-target-month."
        ),
    )
    parser.add_argument(
        "--reset-target-month",
        action="store_true",
        help="Clear bot state for the target month before starting. Defaults to production surveys only.",
    )
    parser.add_argument(
        "--reset-survey-kind",
        choices=[SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG],
        default=SURVEY_KIND_PRODUCTION,
        help="Survey kind to reset with --reset-target-month. Default: production.",
    )
    parser.add_argument(
        "--upsert-user",
        help="Create or update an allowed Telegram username in the bot database, then exit.",
    )
    parser.add_argument(
        "--user-display-name",
        help="Display name for --upsert-user.",
    )
    parser.add_argument(
        "--user-role",
        default="",
        help="Team role for --upsert-user, e.g. backend or frontend.",
    )
    parser.add_argument(
        "--user-access-level",
        choices=["member", "admin"],
        default="member",
        help="Access level for --upsert-user.",
    )
    parser.add_argument(
        "--user-telegram-id",
        type=int,
        help="Optional known Telegram numeric ID for --upsert-user.",
    )
    parser.add_argument(
        "--user-inactive",
        action="store_true",
        help="Mark the --upsert-user row inactive.",
    )
    parser.add_argument(
        "--user-schedule-participant",
        action="store_true",
        help="Mark --upsert-user as a schedule/survey participant.",
    )
    parser.add_argument(
        "--user-manager-only",
        action="store_true",
        help="Mark --upsert-user as manager-only (does not participate in schedule).",
    )
    parser.add_argument(
        "--list-users",
        action="store_true",
        help="List bot_users from the database, then exit.",
    )
    parser.add_argument(
        "--create-survey",
        action="store_true",
        help="Create a survey from DB roster or --participants, then exit unless --send-now.",
    )
    parser.add_argument(
        "--survey-kind",
        choices=[SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG],
        default=SURVEY_KIND_PRODUCTION,
        help="Survey kind for --create-survey. Default: production.",
    )
    parser.add_argument(
        "--participants",
        help="Comma-separated usernames/display names/local-only names for --create-survey.",
    )
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="With --create-survey, send forms immediately.",
    )
    parser.add_argument(
        "--direct-preview",
        action="store_true",
        help=(
            "Create a survey (default kind=debug unless overridden), build preview "
            "immediately with default availability (no response wait), then exit."
        ),
    )
    parser.add_argument(
        "--publish-now",
        action="store_true",
        help="With --direct-preview, also post/approve to the configured Telegram group.",
    )
    parser.add_argument(
        "--allow-production-destination",
        action="store_true",
        help=(
            "Persist allow_production_destination=true in DB runtime settings so debug "
            "publish/approve may post to the configured production Telegram destination."
        ),
    )
    parser.add_argument(
        "--clear-allow-production-destination",
        action="store_true",
        help="Persist allow_production_destination=false in DB runtime settings.",
    )
    parser.add_argument(
        "--send-survey-id",
        help="Send forms for an existing survey id, then exit.",
    )
    parser.add_argument(
        "--force-preview",
        action="store_true",
        help="Rebuild preview for --survey-id, then exit.",
    )
    parser.add_argument(
        "--survey-id",
        help="Survey id for --force-preview or survey field edits.",
    )
    parser.add_argument(
        "--set-survey-status",
        help="Update status for --survey-id.",
    )
    parser.add_argument(
        "--set-survey-starts-at",
        help="Update starts_at for --survey-id.",
    )
    parser.add_argument(
        "--set-survey-closes-at",
        help="Update closes_at for --survey-id.",
    )
    parser.add_argument(
        "--cancel-survey-id",
        help="Cancel a survey by id, then exit.",
    )
    parser.add_argument(
        "--restart-survey-id",
        help="Cancel a non-approved survey and create a new collecting survey for the same month.",
    )
    parser.add_argument(
        "--activate-survey-id",
        help="Explicitly copy a survey's stored schedule into live schedule_entries/monthly_stats for reminders.",
    )
    parser.add_argument(
        "--list-surveys",
        action="store_true",
        help="List surveys, then exit.",
    )
    parser.add_argument(
        "--set-schedule-entry-date",
        help="Approved schedule work_date to edit, formatted as Gregorian YYYY-MM-DD.",
    )
    parser.add_argument(
        "--entry-main",
        help="New main person for --set-schedule-entry-date.",
    )
    parser.add_argument(
        "--entry-backup",
        help="New backup person for --set-schedule-entry-date.",
    )
    parser.add_argument(
        "--set-daily-reminder-time",
        help="Store the DB daily reminder time, formatted HH:MM.",
    )
    parser.add_argument(
        "--clear-daily-reminder-date",
        help="Clear daily_reminders marker for a Gregorian YYYY-MM-DD work date.",
    )
    parser.add_argument(
        "--force-daily-reminder",
        action="store_true",
        help="Send today's daily reminder now (ignores already-sent marker after optional clear).",
    )
    parser.add_argument(
        "--set-survey-start-at",
        "--set-runtime-survey-start-at",
        dest="set_runtime_survey_start_at",
        help="Store survey_start_at in DB runtime settings.",
    )
    parser.add_argument(
        "--set-survey-collect-for",
        "--set-runtime-survey-collect-for",
        dest="set_runtime_survey_collect_for",
        help="Store survey_collect_for in DB runtime settings.",
    )
    parser.add_argument(
        "--set-revision-collect-for",
        "--set-runtime-revision-collect-for",
        dest="set_runtime_revision_collect_for",
        help="Store revision_collect_for in DB runtime settings.",
    )
    parser.add_argument(
        "--set-survey-days-before-month",
        "--set-runtime-survey-days-before-month",
        dest="set_runtime_survey_days_before_month",
        type=int,
        help="Store survey_days_before_month in DB runtime settings.",
    )
    parser.add_argument(
        "--set-target-month",
        "--set-runtime-target-month",
        dest="set_runtime_target_month",
        help="Store target month override in DB as YYYY-MM.",
    )
    parser.add_argument(
        "--clear-target-month",
        "--clear-runtime-target-month",
        dest="clear_runtime_target_month",
        action="store_true",
        help="Clear DB target month override.",
    )
    parser.add_argument(
        "--set-poll-interval-seconds",
        "--set-runtime-poll-interval-seconds",
        dest="set_runtime_poll_interval_seconds",
        type=int,
        help="Store poll_interval_seconds in DB runtime settings.",
    )
    parser.add_argument(
        "--set-timezone",
        "--set-runtime-timezone",
        dest="set_runtime_timezone",
        help="Store timezone in DB runtime settings.",
    )
    parser.add_argument(
        "--set-calendar",
        "--set-runtime-calendar",
        dest="set_runtime_calendar",
        choices=["jalali", "gregorian"],
        help="Store calendar in DB runtime settings.",
    )
    parser.add_argument(
        "--set-bot-name",
        help="Store bot display name in DB runtime settings.",
    )
    parser.add_argument(
        "--set-bot-username",
        help="Store bot username in DB runtime settings.",
    )
    parser.add_argument(
        "--set-bot-id",
        type=int,
        help="Store bot Telegram id in DB runtime settings.",
    )
    parser.add_argument(
        "--set-telegram-token",
        help="Store Telegram bot token in DB runtime settings.",
    )
    parser.add_argument(
        "--set-output-dir",
        help="Store preview/output directory in DB runtime settings.",
    )
    parser.add_argument(
        "--set-holidays",
        help='Store holidays JSON list in DB, e.g. \'[{"date":"1405-05-01","name":"Holiday"}]\' or \'[]\'.',
    )
    parser.add_argument(
        "--set-telegram-group-chat-id",
        type=int,
        help="Store the target Telegram group chat ID in SQLite.",
    )
    parser.add_argument(
        "--set-telegram-topic-id",
        type=int,
        help="Store the target Telegram topic/thread ID in SQLite. Omit to keep existing topic.",
    )
    parser.add_argument(
        "--clear-telegram-topic-id",
        action="store_true",
        help="Clear the target Telegram topic/thread ID in SQLite.",
    )
    parser.add_argument(
        "--show-runtime-settings",
        action="store_true",
        help="Print DB-backed runtime settings such as Telegram destination.",
    )
    return parser.parse_args()


def print_runtime_settings(repo: BotRepository) -> None:
    raw = repo.get_runtime_settings_raw()
    destination = repo.get_telegram_destination()
    if destination:
        print(
            "Telegram destination: "
            f"group_chat_id={destination['group_chat_id']} "
            f"topic_id={destination['topic_id'] if destination['topic_id'] is not None else '-'}"
        )
    else:
        print("Telegram destination: not configured")
    if not raw:
        print("runtime_settings: missing")
        print(
            "Set required keys with --set-* "
            f"({', '.join(RuntimeSettings.missing_keys({}))})"
        )
        return
    missing = RuntimeSettings.missing_keys(raw)
    for key in (
        "bot_name",
        "bot_username",
        "bot_id",
        "telegram_token",
        "output_dir",
        "timezone",
        "calendar",
        "survey_days_before_month",
        "survey_start_at",
        "survey_collect_for",
        "revision_collect_for",
        "daily_reminder_time",
        "poll_interval_seconds",
        "holidays",
        "allow_production_destination",
    ):
        if key not in raw:
            if key == "allow_production_destination":
                print(f"{key}: false")
                continue
            print(f"{key}: <missing>")
            continue
        value = raw[key]
        if key == "telegram_token":
            print(f"{key}: {'***' if value else '<empty>'}")
        elif key == "holidays":
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        elif key == "allow_production_destination":
            print(f"{key}: {bool(value)}")
        else:
            print(f"{key}: {value if value not in (None, '') else '-'}")
    if raw.get("target_year") and raw.get("target_month"):
        print(f"target_month: {raw['target_year']}-{int(raw['target_month']):02d}")
    else:
        print("target_month: automatic" if "target_year" in raw or "target_month" in raw else "target_month: <missing>")
    if missing:
        print(f"incomplete: missing {', '.join(missing)}")


def handle_local_db_command(
    repo: BotRepository,
    settings: InfraSettings,
    args: argparse.Namespace,
) -> bool:
    if args.show_runtime_settings:
        print_runtime_settings(repo)
        return True

    if args.set_telegram_group_chat_id is not None:
        existing = repo.get_telegram_destination() or {}
        if args.clear_telegram_topic_id:
            topic_id = None
        elif args.set_telegram_topic_id is not None:
            topic_id = args.set_telegram_topic_id
        else:
            topic_id = existing.get("topic_id")
        repo.set_telegram_destination(
            group_chat_id=args.set_telegram_group_chat_id,
            topic_id=topic_id,
        )
        print(
            "Telegram destination saved: "
            f"group_chat_id={args.set_telegram_group_chat_id} "
            f"topic_id={topic_id if topic_id is not None else '-'}"
        )
        return True

    if args.set_telegram_topic_id is not None or args.clear_telegram_topic_id:
        existing = repo.get_telegram_destination()
        if not existing:
            raise SystemExit("--set-telegram-group-chat-id is required before setting a topic.")
        topic_id = None if args.clear_telegram_topic_id else args.set_telegram_topic_id
        repo.set_telegram_destination(
            group_chat_id=existing["group_chat_id"],
            topic_id=topic_id,
        )
        print(
            "Telegram destination saved: "
            f"group_chat_id={existing['group_chat_id']} "
            f"topic_id={topic_id if topic_id is not None else '-'}"
        )
        return True

    if args.list_surveys:
        surveys = repo.list_surveys()
        if not surveys:
            print("No surveys found.")
            return True
        for survey in surveys:
            print(
                f"{survey.id} | {survey.kind} | {survey.calendar_type} "
                f"{survey.year}-{survey.month:02d} | {survey.status} | "
                f"starts_at={survey.starts_at or '-'} | closes_at={survey.closes_at or '-'} | "
                f"created_by={survey.created_by}"
            )
        return True

    if args.create_survey and not args.send_now and not args.direct_preview and not args.publish_now:
        survey = create_local_survey(repo, args)
        print(
            f"Survey created: {survey.id} "
            f"({survey.calendar_type} {survey.year}-{survey.month:02d}, {survey.status})"
        )
        return True

    if args.cancel_survey_id:
        if not repo.transition_survey(
            args.cancel_survey_id,
            from_statuses=CANCELABLE_STATUSES,
            to_status=STATUS_CANCELED,
        ):
            survey = repo.get_survey(args.cancel_survey_id)
            raise SystemExit(
                f"Could not cancel survey {args.cancel_survey_id}; "
                f"status={survey.status if survey else 'missing'}"
            )
        print(f"Survey canceled: {args.cancel_survey_id}")
        return True

    if args.restart_survey_id:
        old = repo.get_survey(args.restart_survey_id)
        if old is None:
            raise SystemExit(f"Survey not found: {args.restart_survey_id}")
        if old.status == STATUS_APPROVED:
            raise SystemExit(f"Cannot restart approved survey: {old.id}")
        if not repo.transition_survey(
            old.id,
            from_statuses=CANCELABLE_STATUSES,
            to_status=STATUS_CANCELED,
        ):
            raise SystemExit(
                f"Could not cancel survey {old.id} before restart; status={old.status}"
            )
        runtime = repo.get_runtime_settings()
        now = datetime.now(ZoneInfo(runtime.timezone))
        new_survey = repo.create_survey(
            survey_id=survey_identity(old.calendar_type, old.year, old.month),
            calendar_type=old.calendar_type,
            year=old.year,
            month=old.month,
            status=STATUS_COLLECTING,
            starts_at=now.isoformat(),
            closes_at=local_datetime_value(runtime.survey_collect_for, runtime.timezone)
            if runtime.survey_collect_for
            else "",
            created_by=f"local-restart:{old.id}",
            participants=repo.list_survey_participants(old.id),
            kind=old.kind,
        )
        print(f"Survey restarted: old={old.id} new={new_survey.id}")
        return True

    if args.activate_survey_id:
        survey = repo.get_survey(args.activate_survey_id)
        if survey is None:
            raise SystemExit(f"Survey not found: {args.activate_survey_id}")
        activate_survey_schedule(repo, settings, survey)
        print(
            f"Live reminder schedule activated from survey: {survey.id} "
            f"({survey.kind} {survey.calendar_type} {survey.year}-{survey.month:02d})"
        )
        return True

    if args.survey_id and (
        args.set_survey_status or args.set_survey_starts_at or args.set_survey_closes_at
    ):
        runtime = repo.get_runtime_settings()
        updates = {}
        if args.set_survey_status:
            updates["status"] = args.set_survey_status
        if args.set_survey_starts_at:
            updates["starts_at"] = local_datetime_value(args.set_survey_starts_at, runtime.timezone)
        if args.set_survey_closes_at:
            updates["closes_at"] = local_datetime_value(args.set_survey_closes_at, runtime.timezone)
        repo.update_survey(args.survey_id, **updates)
        print(f"Survey updated: {args.survey_id}")
        return True

    if args.set_schedule_entry_date:
        if not args.entry_main or not args.entry_backup:
            raise SystemExit("--entry-main and --entry-backup are required.")
        updated = repo.set_schedule_entry_people(
            args.set_schedule_entry_date,
            args.entry_main,
            args.entry_backup,
        )
        if not updated:
            raise SystemExit(f"No schedule entry found for {args.set_schedule_entry_date}.")
        print(
            f"Schedule entry updated: {args.set_schedule_entry_date} "
            f"main={args.entry_main} backup={args.entry_backup}"
        )
        return True

    if args.clear_daily_reminder_date:
        repo.clear_daily_reminder(args.clear_daily_reminder_date)
        print(f"Daily reminder marker cleared: {args.clear_daily_reminder_date}")
        return True

    if args.set_daily_reminder_time:
        day_time.fromisoformat(args.set_daily_reminder_time)
        repo.update_runtime_settings(daily_reminder_time=args.set_daily_reminder_time)
        print(f"Daily reminder time set to: {args.set_daily_reminder_time}")
        return True

    if args.allow_production_destination and args.clear_allow_production_destination:
        raise SystemExit(
            "Use only one of --allow-production-destination or "
            "--clear-allow-production-destination."
        )
    if args.clear_allow_production_destination:
        repo.update_runtime_settings(allow_production_destination=False)
        print("Runtime setting allow_production_destination=false")
        return True
    if args.allow_production_destination:
        repo.update_runtime_settings(allow_production_destination=True)
        print("Runtime setting allow_production_destination=true")
        # Keep going when this invocation also publishes/previews; otherwise exit.
        if not (
            getattr(args, "publish_now", False)
            or getattr(args, "direct_preview", False)
            or getattr(args, "create_survey", False)
            or getattr(args, "send_now", False)
            or getattr(args, "force_preview", False)
            or getattr(args, "send_survey_id", None)
            or getattr(args, "force_daily_reminder", False)
            or getattr(args, "reset_target_month", False)
        ):
            return True

    runtime_updates = {}
    if args.set_runtime_survey_start_at is not None:
        runtime_updates["survey_start_at"] = args.set_runtime_survey_start_at
    if args.set_runtime_survey_collect_for is not None:
        runtime_updates["survey_collect_for"] = args.set_runtime_survey_collect_for
    if args.set_runtime_revision_collect_for is not None:
        runtime_updates["revision_collect_for"] = args.set_runtime_revision_collect_for
    if args.set_runtime_survey_days_before_month is not None:
        runtime_updates["survey_days_before_month"] = args.set_runtime_survey_days_before_month
    if args.set_runtime_poll_interval_seconds is not None:
        runtime_updates["poll_interval_seconds"] = args.set_runtime_poll_interval_seconds
    if args.set_runtime_timezone is not None:
        runtime_updates["timezone"] = args.set_runtime_timezone
    if args.set_runtime_calendar is not None:
        runtime_updates["calendar"] = args.set_runtime_calendar
    if args.set_bot_name is not None:
        runtime_updates["bot_name"] = args.set_bot_name
    if args.set_bot_username is not None:
        runtime_updates["bot_username"] = args.set_bot_username
    if args.set_bot_id is not None:
        runtime_updates["bot_id"] = args.set_bot_id
    if args.set_telegram_token is not None:
        runtime_updates["telegram_token"] = args.set_telegram_token
    if args.set_output_dir is not None:
        runtime_updates["output_dir"] = args.set_output_dir
    if args.set_holidays is not None:
        try:
            holidays = json.loads(args.set_holidays)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid --set-holidays JSON: {exc}") from exc
        if not isinstance(holidays, list):
            raise SystemExit("--set-holidays must be a JSON list.")
        runtime_updates["holidays"] = holidays
    if args.clear_runtime_target_month:
        runtime_updates["target_year"] = None
        runtime_updates["target_month"] = None
    elif args.set_runtime_target_month:
        year, month = args.set_runtime_target_month.split("-", maxsplit=1)
        runtime_updates["target_year"] = int(year)
        runtime_updates["target_month"] = int(month)
    if runtime_updates:
        updated = repo.update_runtime_settings(**runtime_updates)
        print("Runtime settings updated:")
        for key, value in runtime_updates.items():
            if key == "telegram_token":
                print(f"  {key}=***")
            else:
                print(f"  {key}={value if value is not None else '-'}")
        missing = RuntimeSettings.missing_keys(updated)
        if missing:
            print(f"Still incomplete. Missing: {', '.join(missing)}")
        else:
            print("runtime_settings is complete.")
        return True

    return False


def build_bot(settings: InfraSettings, repo: BotRepository) -> AdhocTelegramBot:
    runtime = repo.get_runtime_settings()
    if not runtime.telegram_token:
        raise SystemExit("telegram_token is empty in runtime_settings. Use --set-telegram-token.")
    members = repo.list_members(active_only=True)
    telegram = TelegramClient(runtime.telegram_token)
    return AdhocTelegramBot(settings, members, repo, telegram)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    settings = load_infra_settings(cli_database=args.database)
    repo = BotRepository(settings.database_path)
    if handle_user_admin_command(repo, args):
        return
    if handle_local_db_command(repo, settings, args):
        return

    if args.direct_preview or args.publish_now:
        if args.survey_kind == SURVEY_KIND_PRODUCTION and not args.create_survey:
            # Keep explicit production if user set --survey-kind production.
            pass
        elif args.survey_kind != SURVEY_KIND_DEBUG:
            args.survey_kind = SURVEY_KIND_DEBUG
        bot = build_bot(settings, repo)
        if args.survey_id and not args.create_survey:
            survey = repo.get_survey(args.survey_id)
            if survey is None:
                raise SystemExit(f"Survey not found: {args.survey_id}")
        else:
            survey = create_local_survey(repo, args)
        bot.create_preview(survey.year, survey.month, survey_id=survey.id)
        print(f"Direct preview built for survey: {survey.id}")
        if args.publish_now:
            survey = repo.get_survey(survey.id) or survey
            bot.publish_survey(survey)
            print(f"Survey published: {survey.id}")
        return

    bot = build_bot(settings, repo)
    if args.create_survey and args.send_now:
        survey = create_local_survey(repo, args)
        bot.send_survey_by_id(survey.id)
        print(f"Survey created and sent: {survey.id}")
        return
    if args.send_survey_id:
        bot.send_survey_by_id(args.send_survey_id)
        print(f"Survey sent: {args.send_survey_id}")
        return
    if args.force_preview:
        if not args.survey_id:
            raise SystemExit("--survey-id is required with --force-preview.")
        survey = repo.get_survey(args.survey_id)
        if survey is None:
            raise SystemExit(f"Survey not found: {args.survey_id}")
        bot.create_preview(survey.year, survey.month, survey_id=survey.id)
        print(f"Preview rebuilt for survey: {survey.id}")
        return
    if args.force_daily_reminder:
        target = day_time.fromisoformat(bot.runtime.daily_reminder_time)
        now = datetime.now(ZoneInfo(bot.timezone_name)).replace(
            hour=target.hour,
            minute=target.minute,
            second=0,
            microsecond=0,
        )
        work_date = now.date().isoformat()
        repo.clear_daily_reminder(work_date)
        bot.send_daily_reminder(now)
        print(f"Forced daily reminder for {work_date}")
        return
    if args.reset_target_month:
        reset_target_month_state(bot, kind=args.reset_survey_kind)
    bot.run_forever()


if __name__ == "__main__":
    main()
