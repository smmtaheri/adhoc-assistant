import argparse
import json
import logging
import re
import time
from copy import deepcopy
from dataclasses import replace
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
from adhoc_assistant.config import load_config
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
    Survey,
    normalize_username,
    utc_now,
)
from adhoc_assistant.telegram_bot.settings import (
    BotSettings,
    Member,
    load_bot_settings,
)
from adhoc_assistant.telegram_bot.telegram import TelegramClient
from adhoc_assistant.telegram_bot.telegram import TelegramApiError


logger = logging.getLogger(__name__)

STATUS_SCHEDULED = "scheduled"
STATUS_COLLECTING = "collecting"
STATUS_PENDING_ADMIN_REVIEW = "pending_admin_review"
STATUS_BLOCKED = "blocked"
STATUS_REVISION_REQUESTED = "revision_requested"
STATUS_APPROVED = "approved"
STATUS_CANCELED = "canceled"
TERMINAL_STATUSES = {STATUS_APPROVED, STATUS_CANCELED}
EDITABLE_SURVEY_STATUSES = {STATUS_COLLECTING, STATUS_REVISION_REQUESTED}
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


def default_response(member: Member) -> AvailabilityResponse:
    return AvailabilityResponse(
        telegram_id=member.telegram_id,
        name=member.name,
        unavailable_days=list(member.unavailable_days),
        unavailable_weekdays=list(member.unavailable_weekdays),
        confirmed=not can_receive_telegram_messages(member),
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
                "name": member.name,
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
        "name": member.name,
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
    people_by_name = {
        member.name: response_person(
            member,
            member_responses[member.telegram_id],
            year,
            month,
            calendar_type,
        )
        for member in active
    }
    member_by_name = {member.name: member for member in active}
    member_by_id = {member.telegram_id: member for member in active}
    flagged_member_ids: set[int] = set()
    blockers = []

    for item in schedule:
        missing_main = item["main"] == NO_MAIN
        missing_backup = item["backup"] == NO_BACKUP
        if not missing_main and not missing_backup:
            continue

        workday = date.fromisoformat(item.get("gregorian_date", item["date"]))
        available = [
            name for name, person in people_by_name.items() if is_available(person, workday)
        ]
        unavailable = sorted(set(people_by_name) - set(available))
        for name in unavailable:
            member = member_by_name[name]
            if can_receive_telegram_messages(member):
                flagged_member_ids.add(member.telegram_id)

        blockers.append(
            {
                "date": item["date"],
                "weekday": item["weekday"],
                "missing_main": missing_main,
                "missing_backup": missing_backup,
                "available": sorted(available),
                "unavailable": unavailable,
            }
        )

    plain_stats = stats_to_plain_dict(stats)
    main_counts = [values["main_count"] for values in plain_stats.values()]
    main_spread = max(main_counts) - min(main_counts) if main_counts else 0
    warnings = []
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

    status = "ready"
    if blockers:
        status = STATUS_BLOCKED
    elif warnings:
        status = "needs_review"

    return {
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "flagged_member_ids": sorted(flagged_member_ids),
        "flagged_member_names": [
            member_by_id[member_id].name
            for member_id in sorted(flagged_member_ids)
            if member_id in member_by_id
        ],
        "availability": sorted(availability, key=lambda item: item["name"]),
        "main_spread": main_spread,
        "average_unavailable": round(average_unavailable, 2),
    }


def review_text(review: dict, calendar_type: str) -> str:
    if calendar_type == "jalali":
        lines = ["گزارش بررسی:"]
        if review["blockers"]:
            lines.append("مشکل ظرفیت:")
            for item in review["blockers"]:
                missing = []
                if item["missing_main"]:
                    missing.append("نفر اصلی")
                if item["missing_backup"]:
                    missing.append("پشتیبان")
                lines.append(
                    f"- {item['date']} ({item['weekday']}): "
                    f"{' و '.join(missing)} ندارد؛ افراد حاضر: "
                    f"{', '.join(item['available']) or 'هیچ‌کس'}"
                )
        if review["warnings"]:
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
    if review["blockers"]:
        lines.append("Coverage blockers:")
        for item in review["blockers"]:
            missing = []
            if item["missing_main"]:
                missing.append("main")
            if item["missing_backup"]:
                missing.append("helper")
            lines.append(
                f"- {item['date']} ({item['weekday']}): missing "
                f"{' and '.join(missing)}; available: "
                f"{', '.join(item['available']) or 'nobody'}"
            )
    if review["warnings"]:
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
    if review.get("blockers"):
        status = "مسدود" if calendar_type == "jalali" else "blocked"
    elif review.get("warnings"):
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


def debug_inactive_message(calendar_type: str) -> str:
    if calendar_type == "jalali":
        return "بات فعلا غیر فعال است."
    return "The bot is currently inactive."


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
        settings: BotSettings,
        members: list[Member],
        repo: BotRepository,
        telegram: TelegramClient,
    ) -> None:
        self.settings = settings
        self.members = members
        self.repo = repo
        self.telegram = telegram
        self.calendar_type = normalize_calendar_type(settings.calendar)
        self.scheduled_work_interval_seconds = 30
        self.next_scheduled_work_at: datetime | None = None

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
        if self.settings.debug_enabled:
            return self.is_admin(telegram_id)
        return self.is_admin(telegram_id) or self.find_active_member(telegram_id) is not None

    def member_by_name(self) -> dict[str, Member]:
        return {member.name: member for member in self.members if can_receive_telegram_messages(member)}

    def can_message_member_in_current_mode(self, member: Member) -> bool:
        if not can_receive_telegram_messages(member):
            return False
        if self.settings.debug_enabled:
            return self.is_admin(member.telegram_id)
        return True

    def messageable_active_members(self) -> list[Member]:
        return [
            member
            for member in schedule_members(self.members)
            if self.can_message_member_in_current_mode(member)
        ]

    def member_can_receive_revision(self, telegram_id: int) -> bool:
        member = self.find_active_member_by_telegram_id(telegram_id)
        return member is not None and self.can_message_member_in_current_mode(member)

    def member_names_for_ids(self, telegram_ids: list[int]) -> list[str]:
        members_by_id = {member.telegram_id: member for member in schedule_members(self.members)}
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

    def responses_with_current_mode_defaults(
        self,
        responses: dict[int, AvailabilityResponse],
    ) -> dict[int, AvailabilityResponse]:
        effective = dict(responses)
        for member in schedule_members(self.members):
            response = effective.get(member.telegram_id) or default_response(member)
            if not self.can_message_member_in_current_mode(member):
                response = AvailabilityResponse(
                    telegram_id=response.telegram_id,
                    name=response.name,
                    unavailable_days=list(response.unavailable_days),
                    unavailable_weekdays=list(response.unavailable_weekdays),
                    confirmed=True,
                    mode=response.mode,
                )
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

    def active_survey_record(self) -> Survey | None:
        survey = self.repo.get_active_survey()
        if survey is not None:
            return survey

        state = self.repo.get_state("active_survey")
        if not state or state.get("id"):
            return None
        now = datetime.now(ZoneInfo(self.settings.timezone))
        year = int(state["year"])
        month = int(state["month"])
        try:
            survey = self.create_survey_record(
                year,
                month,
                now,
                starts_at=now,
                status=str(state.get("phase", STATUS_COLLECTING)),
                created_by="legacy-active-state",
            )
        except ValueError:
            return self.repo.get_active_survey()
        collect_until = str(state.get("collect_until") or survey.closes_at)
        allowed_member_ids = state.get("allowed_member_ids", [])
        if collect_until != survey.closes_at:
            self.repo.update_survey(survey.id, closes_at=collect_until)
            survey = self.repo.get_survey(survey.id) or survey
        self.repo.set_state(
            "active_survey",
            {
                "id": survey.id,
                "calendar": survey.calendar_type,
                "year": survey.year,
                "month": survey.month,
                "phase": survey.status,
                "collect_until": survey.closes_at,
                "allowed_member_ids": allowed_member_ids,
            },
        )
        return survey

    def active_or_latest_survey_for_month(self, year: int, month: int) -> Survey | None:
        survey = self.active_survey_record()
        if survey and survey.year == year and survey.month == month:
            return survey
        return self.repo.get_latest_survey_for_month(self.calendar_type, year, month)

    def responses_for_survey(self, survey: Survey) -> dict[int, AvailabilityResponse]:
        responses = self.repo.list_survey_responses(survey.id)
        legacy = self.repo.list_responses(survey.calendar_type, survey.year, survey.month)
        for response in legacy.values():
            self.repo.save_survey_response(survey.id, response)
            responses[response.telegram_id] = response
        return responses

    def configured_closes_at(self, now: datetime, raw_value: str | None = None) -> str:
        raw = self.settings.survey_collect_for if raw_value is None else raw_value
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
            participants=schedule_members(self.members),
        )
        self.repo.set_state(
            "active_survey",
            {
                "id": survey.id,
                "calendar": survey.calendar_type,
                "year": survey.year,
                "month": survey.month,
                "phase": survey.status,
                "collect_until": survey.closes_at,
                "allowed_member_ids": [],
            },
        )
        return survey

    def start_collecting(self, year: int, month: int, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
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
        self.repo.set_state(
            "active_survey",
            {
                "id": survey.id,
                "calendar": survey.calendar_type,
                "year": survey.year,
                "month": survey.month,
                "phase": STATUS_COLLECTING,
                "collect_until": survey.closes_at,
                "allowed_member_ids": [],
            },
        )

    def start_revision(
        self,
        year: int,
        month: int,
        member_ids: list[int],
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        deadline_at = self.deadline_at(self.settings.revision_collect_for, now)
        survey = self.active_or_latest_survey_for_month(year, month)
        if survey is None:
            raise ValueError(f"No survey found for {year}-{month:02d}.")
        self.repo.update_survey(
            survey.id,
            status=STATUS_REVISION_REQUESTED,
            closes_at=deadline_at.isoformat() if deadline_at else "",
        )
        self.repo.set_state(
            "active_survey",
            {
                "id": survey.id,
                "calendar": survey.calendar_type,
                "year": survey.year,
                "month": survey.month,
                "phase": STATUS_REVISION_REQUESTED,
                "collect_until": deadline_at.isoformat() if deadline_at else "",
                "allowed_member_ids": sorted(set(member_ids)),
            },
        )

    def deadline_at(self, raw_value: str, now: datetime) -> datetime | None:
        if not raw_value:
            return None
        return parse_scheduled_datetime(raw_value, now)

    def target_month(self, today: date | None = None) -> tuple[int, int]:
        if self.settings.target_year is not None and self.settings.target_month is not None:
            return self.settings.target_year, self.settings.target_month

        today = today or datetime.now(ZoneInfo(self.settings.timezone)).date()
        return survey_target_month(
            today=today,
            calendar_type=self.calendar_type,
            days_before=self.settings.survey_days_before_month,
        )

    def scheduled_survey_due(self, now: datetime | None = None) -> tuple[int, int] | None:
        raw_start = self.settings.survey_start_at
        if not raw_start:
            return None

        now = now or datetime.now(ZoneInfo(self.settings.timezone))
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
        today = today or datetime.now(ZoneInfo(self.settings.timezone)).date()
        return due_survey_month(
            today=today,
            calendar_type=self.calendar_type,
            days_before=self.settings.survey_days_before_month,
        )

    def ensure_survey_started(self, today: date | datetime | None = None) -> None:
        if isinstance(today, datetime):
            now = today.astimezone(ZoneInfo(self.settings.timezone))
        elif today is not None:
            now = datetime.combine(today, day_time.min, tzinfo=ZoneInfo(self.settings.timezone))
        else:
            now = datetime.now(ZoneInfo(self.settings.timezone))

        active = self.active_survey_record()
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
        run = self.repo.get_monthly_run(self.calendar_type, year, month)
        if run and run["status"] not in {STATUS_CANCELED}:
            logger.info(
                "Survey for %s/%s already exists with status %s; not sending requests again.",
                year,
                month,
                run["status"],
            )
            return

        survey = self.create_survey_record(
            year,
            month,
            now,
            starts_at=now,
            status=STATUS_COLLECTING,
            created_by="auto",
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
            state["consumed_at"] = datetime.now(ZoneInfo(self.settings.timezone)).isoformat()
            self.repo.set_state("scheduled_survey_start", state)

    def scheduled_survey_start_at(self, now: datetime) -> datetime | None:
        raw = self.settings.survey_start_at
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
        self.repo.delete_state("active_survey")

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
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
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
            if not self.can_message_member_in_current_mode(member):
                self.repo.save_survey_response(survey.id, response)
                self.repo.save_response(
                    survey.calendar_type,
                    survey.year,
                    survey.month,
                    response,
                )
                continue
            self.repo.save_survey_response(survey.id, response)
            self.repo.save_response(
                survey.calendar_type,
                survey.year,
                survey.month,
                response,
            )
            try:
                logger.info("Sending availability request to %s (%s).", member.name, member.telegram_id)
                self.send_or_edit_member_form(survey, member, response)
            except TelegramApiError as exc:
                failed_members.append(f"{member.name}: {exc}")

        self.repo.set_state(
            "active_survey",
            {
                "id": survey.id,
                "calendar": survey.calendar_type,
                "year": survey.year,
                "month": survey.month,
                "phase": STATUS_COLLECTING,
                "collect_until": survey.closes_at,
                "allowed_member_ids": [],
            },
        )
        state = self.active_survey_state() or {}
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
        today = today or datetime.now(ZoneInfo(self.settings.timezone)).date()
        year, month = current_calendar_month(today, self.calendar_type)
        if today != calendar_month_start(year, month, self.calendar_type):
            return

        if self.repo.get_monthly_run(self.calendar_type, year, month):
            return

        self.start_collecting(year, month)
        self.create_preview(year, month)

    def active_survey(self) -> tuple[str, int, int] | None:
        survey = self.active_survey_record()
        if survey is None:
            state = self.repo.get_state("active_survey")
            if not state:
                return None
            return state["calendar"], int(state["year"]), int(state["month"])
        return survey.calendar_type, survey.year, survey.month

    def active_survey_id(self) -> str | None:
        survey = self.active_survey_record()
        if survey is not None:
            return survey.id
        state = self.repo.get_state("active_survey")
        if state:
            return state.get("id")
        return None

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
        active = self.active_survey_record()
        return (
            active is not None
            and active.id == survey.id
            and active.status in EDITABLE_SURVEY_STATUSES
        )

    def active_survey_state(self) -> dict | None:
        survey = self.active_survey_record()
        if survey is None:
            return self.repo.get_state("active_survey")
        legacy = self.repo.get_state("active_survey") or {}
        return {
            "id": survey.id,
            "calendar": survey.calendar_type,
            "year": survey.year,
            "month": survey.month,
            "phase": survey.status,
            "collect_until": survey.closes_at,
            "allowed_member_ids": legacy.get("allowed_member_ids", []),
        }

    def survey_closed_message(self) -> str:
        if self.calendar_type == "jalali":
            return "مهلت ثبت availability تمام شده است. اگر نیاز به اصلاح داری با ادمین هماهنگ کن."
        return "The availability window is closed. Contact an admin if you need a change."

    def member_can_edit_active_survey(
        self,
        member: Member,
        state: dict,
        now: datetime | None = None,
    ) -> bool:
        if self.collection_deadline_reached(now):
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
        if self.settings.debug_enabled and not self.is_admin(chat_id):
            self.telegram.send_message(chat_id, debug_inactive_message(self.calendar_type))
            return
        if member is None and not self.is_authorized_user(chat_id):
            message = (
                debug_inactive_message(self.calendar_type)
                if self.settings.debug_enabled
                else unauthorized_message(self.calendar_type)
            )
            self.telegram.send_message(chat_id, message)
            return

        member = member or self.find_active_member(chat_id)
        if member is None:
            self.telegram.send_message(chat_id, ACTIVE_MEMBER_ERROR)
            return

        survey = self.active_survey_record()
        if survey and not self.settings.debug_enabled:
            state = self.active_survey_state() or {}
            if not self.member_can_edit_active_survey(member, state):
                self.telegram.send_message(chat_id, self.survey_closed_message())
                return
            response = response_for_survey_member(self.repo, survey.id, member)
            self.repo.save_survey_response(survey.id, response)
            self.repo.save_response(
                survey.calendar_type,
                survey.year,
                survey.month,
                response,
            )
            self.send_or_edit_member_form(survey, member, response)
            return

        if not self.settings.debug_enabled:
            self.telegram.send_message(chat_id, "There is no active availability survey right now.")
            return

        year, month = self.target_month()
        if self.settings.debug_enabled and self.is_admin(chat_id):
            self.reset_debug_survey(year, month)
        elif self.active_survey_record() is None:
            self.start_collecting(year, month)

        survey = self.active_or_latest_survey_for_month(year, month)
        if survey is None:
            self.telegram.send_message(chat_id, "There is no active availability survey right now.")
            return
        response = response_for_survey_member(self.repo, survey.id, member)
        self.repo.save_survey_response(survey.id, response)
        self.repo.save_response(
            survey.calendar_type,
            survey.year,
            survey.month,
            response,
        )
        self.send_or_edit_member_form(survey, member, response)

    def reset_debug_survey(self, year: int, month: int) -> None:
        survey = self.active_or_latest_survey_for_month(year, month)
        if survey and survey.status != STATUS_APPROVED:
            self.repo.update_survey(survey.id, status=STATUS_CANCELED)
            self.repo.delete_survey_responses(survey.id)
        self.repo.delete_month_responses(self.calendar_type, year, month)
        self.create_survey_record(
            year,
            month,
            datetime.now(ZoneInfo(self.settings.timezone)),
            starts_at=datetime.now(ZoneInfo(self.settings.timezone)),
            status=STATUS_COLLECTING,
            created_by="debug-start",
        )

    def handle_private_text(self, chat_id: int, text: str = "", user: dict | None = None) -> None:
        if text.startswith("/start"):
            self.start_member_survey(chat_id, user)
            return

        if not self.is_authorized_user(chat_id):
            message = (
                debug_inactive_message(self.calendar_type)
                if self.settings.debug_enabled
                else unauthorized_message(self.calendar_type)
            )
            self.telegram.send_message(chat_id, message)
            return

        if self.is_today_command(text):
            self.send_today_bug_day(chat_id)
            return

        survey = self.active_survey_record()
        if not survey:
            self.telegram.send_message(chat_id, "There is no active availability survey right now.")
            return

        member = self.find_active_member(chat_id)
        if member is None:
            self.telegram.send_message(chat_id, ACTIVE_MEMBER_ERROR)
            return
        state = self.active_survey_state() or {}
        if not self.member_can_edit_active_survey(member, state):
            self.telegram.send_message(chat_id, self.survey_closed_message())
            return

        response = response_for_survey_member(self.repo, survey.id, member)
        self.repo.save_survey_response(survey.id, response)
        self.repo.save_response(
            survey.calendar_type,
            survey.year,
            survey.month,
            response,
        )
        self.send_or_edit_member_form(survey, member, response)

    def is_today_command(self, text: str) -> bool:
        normalized = text.strip().lower()
        return normalized in {"/today", "today", "امروز", "روز باگ امروز"}

    def send_today_bug_day(self, chat_id: int, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
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
        if self.settings.debug_enabled and not self.is_admin(telegram_id):
            self.safe_answer_callback_query(
                callback["id"],
                debug_inactive_message(self.calendar_type),
                show_alert=True,
            )
            return

        member = self.find_active_member(telegram_id)
        if member is None:
            self.safe_answer_callback_query(callback["id"], unauthorized_message(self.calendar_type))
            return
        if not self.survey_is_current_and_editable(survey):
            self.stale_survey_callback(callback, self.survey_closed_message())
            return

        state = self.active_survey_state() or {}
        if not self.member_can_edit_active_survey(member, state):
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
            self.repo.save_survey_response(survey.id, response)
            self.repo.save_response(
                survey.calendar_type,
                survey.year,
                survey.month,
                response,
            )
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
        if (
            self.settings.debug_enabled
            and self.settings.debug_auto_preview_on_confirm
            and response.confirmed
        ):
            try:
                self.create_debug_preview_if_needed(survey.calendar_type, survey.year, survey.month)
            except TelegramApiError as exc:
                logger.warning("Debug preview generation failed for %s: %s", member.name, exc)
                self.safe_send_message(
                    chat_id=telegram_id,
                    text=TEMPORARY_TELEGRAM_ERROR,
                )

    def create_debug_preview_if_needed(self, calendar_type: str, year: int, month: int) -> None:
        self.create_preview(year, month)

    def required_response_members(self, survey: Survey | None = None) -> list[Member]:
        state = self.active_survey_state() or {}
        phase = state.get("phase", STATUS_COLLECTING)
        if survey is not None:
            source = self.repo.list_survey_participants(survey.id)
        else:
            source = self.messageable_active_members()
        members = [member for member in source if self.can_message_member_in_current_mode(member)]
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

    def collection_deadline_reached(self, now: datetime | None = None) -> bool:
        state = self.active_survey_state()
        if not state or not state.get("collect_until"):
            return False

        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        collect_until = parse_stored_datetime(state["collect_until"], now)
        if collect_until is None:
            return False
        return now >= collect_until

    def maybe_create_preview(self, today: date | datetime | None = None) -> None:
        survey = self.active_survey_record()
        if not survey:
            return

        if survey.status in {
            STATUS_SCHEDULED,
            STATUS_PENDING_ADMIN_REVIEW,
            STATUS_BLOCKED,
            STATUS_APPROVED,
            STATUS_CANCELED,
        }:
            return

        if isinstance(today, datetime):
            now = today.astimezone(ZoneInfo(self.settings.timezone))
            today = now.date()
        else:
            now = datetime.now(ZoneInfo(self.settings.timezone))
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
                datetime.now(ZoneInfo(self.settings.timezone)),
                status=STATUS_COLLECTING,
                created_by="legacy-preview",
            )
        base_config = load_config(self.settings.schedule_config_path)
        responses = self.responses_with_current_mode_defaults(
            self.responses_for_survey(survey)
        )
        participants = self.repo.list_survey_participants(survey.id)
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
            people_names=[member.name for member in schedule_members(participants)],
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
        status = (
            STATUS_BLOCKED
            if review["blockers"]
            else STATUS_PENDING_ADMIN_REVIEW
        )

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
        for admin_id in self.admin_telegram_ids():
            self.telegram.send_photo(
                chat_id=admin_id,
                photo_path=image_path,
                caption=caption,
                reply_markup=approval_markup,
            )
            self.safe_send_long_message(admin_id, report_message)

        self.repo.update_survey(
            survey.id,
            status=status,
            preview_sent_at=utc_now(),
            image_path=str(image_path),
            schedule_json=json.dumps(schedule),
            stats_json=json.dumps(stats_to_plain_dict(stats)),
            review_json=json.dumps(review),
        )
        self.repo.set_state(
            "active_survey",
            {
                "id": survey.id,
                "calendar": survey.calendar_type,
                "year": survey.year,
                "month": survey.month,
                "phase": status,
                "collect_until": survey.closes_at,
                "allowed_member_ids": [],
            },
        )

    def resolve_output_dir(self) -> Path:
        try:
            return self.ensure_writable_dir(self.settings.output_dir)
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
            survey = self.repo.get_latest_survey_for_month(calendar_type, year, month)

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
            now = datetime.now(ZoneInfo(self.settings.timezone))
            if survey.status != STATUS_APPROVED:
                self.repo.update_survey(survey.id, status=STATUS_CANCELED)
            new_survey = self.create_survey_record(
                year,
                month,
                now,
                starts_at=now,
                status=STATUS_COLLECTING,
                created_by=f"admin:{user_id}:restart:{survey.id}",
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
            return

        if action == "regen":
            try:
                self.create_preview(year, month, survey_id=survey.id)
                self.remove_callback_buttons(callback)
            except TelegramApiError as exc:
                logger.warning("Preview regeneration failed for %s/%s: %s", year, month, exc)
                self.safe_send_message(user_id, TEMPORARY_TELEGRAM_ERROR)
            return

        if action == "cancel":
            if survey.status == STATUS_APPROVED:
                self.safe_send_message(user_id, "This schedule is already approved.")
                return
            self.repo.update_survey(survey.id, status=STATUS_CANCELED)
            self.repo.delete_state("active_survey")
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
                    if self.member_can_receive_revision(member_id)
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
                    for member in self.messageable_active_members()
                ]

            self.start_revision(year, month, member_ids)
            self.send_revision_requests(year, month, member_ids, survey.id)
            self.remove_callback_buttons(callback)
            self.safe_send_message(
                user_id,
                self.revision_started_message(member_ids),
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
        review = json.loads(survey.review_json or "{}")
        if review.get("blockers"):
            self.repo.update_survey(survey.id, status=STATUS_BLOCKED)
            self.safe_send_message(
                user_id,
                "This schedule has coverage blockers and cannot be approved.",
            )
            return

        schedule = json.loads(survey.schedule_json)
        stats = json.loads(survey.stats_json or "{}")
        image_path = Path(survey.image_path or "")
        caption = group_schedule_caption(year, month, calendar_type)

        try:
            group_chat_id, topic_id = self.telegram_destination()
            sent_message = self.telegram.send_photo(
                chat_id=group_chat_id,
                message_thread_id=topic_id,
                photo_path=image_path,
                caption=caption,
            )
            message_id = telegram_message_id(sent_message)
            pin_error = None
            if message_id is not None:
                pin_error = self.safe_pin_chat_message(
                    group_chat_id,
                    message_id,
                )
            save_month_to_db(self.settings.database_path, year, month, schedule, stats)
            self.repo.update_survey(
                survey.id,
                status=STATUS_APPROVED,
                approved_at=utc_now(),
                group_sent_at=utc_now(),
            )
            self.repo.delete_state("active_survey")
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

    def revision_started_message(self, member_ids: list[int]) -> str:
        names = format_names(self.member_names_for_ids(member_ids), self.calendar_type)
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
            if not self.can_message_member_in_current_mode(member):
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
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        reminder_state = self.repo.get_state("daily_reminder_time") or {}
        target_time = day_time.fromisoformat(
            str(reminder_state.get("time") or self.settings.daily_reminder_time)
        )
        if now.time().replace(second=0, microsecond=0) < target_time:
            return

        work_date = now.date().isoformat()
        if self.repo.daily_was_sent(work_date):
            return

        entry = self.repo.get_schedule_entry(work_date)
        if entry is None:
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
            return

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
        self.repo.mark_daily_sent(work_date)

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
        now = datetime.now(ZoneInfo(self.settings.timezone))
        today = now.date()
        for task in (
            lambda now=now: self.ensure_survey_started(now),
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
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        if self.next_scheduled_work_at is None or now >= self.next_scheduled_work_at:
            self.next_scheduled_work_at = now + timedelta(seconds=self.scheduled_work_interval_seconds)
            return True
        return False

    def run_loop_once(self, offset: int | None) -> int | None:
        try:
            updates = self.telegram.get_updates(
                offset=offset,
                timeout=self.settings.poll_interval_seconds,
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Adhoc Assistant Telegram bot.")
    parser.add_argument(
        "--config",
        default="bot_config.toml",
        help="Path to the bot TOML config file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable interactive debug flow: /start opens the form and Confirm creates a preview.",
    )
    parser.add_argument(
        "--survey-start-at",
        help="Override survey_start_at, e.g. '+2m', '+2h', '+2d', or '2026-07-10 14:30'.",
    )
    parser.add_argument(
        "--survey-collect-for",
        help="Override survey_collect_for, e.g. '+2m', '+2h', '+2d', or an exact timestamp.",
    )
    parser.add_argument(
        "--revision-collect-for",
        help="Override revision_collect_for, e.g. '+2h' or an exact timestamp.",
    )
    parser.add_argument(
        "--target-month",
        help="Override target month in the configured calendar, formatted as YYYY-MM.",
    )
    parser.add_argument(
        "--database",
        help="Override SQLite database path for isolated test runs.",
    )
    parser.add_argument(
        "--output-dir",
        help="Override output directory for generated preview images.",
    )
    parser.add_argument(
        "--reset-target-month",
        action="store_true",
        help="Clear bot state for the target month before starting. Intended for timed test runs.",
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
        help="Mark --upsert-user as inactive.",
    )
    parser.add_argument(
        "--user-schedule-participant",
        action="store_true",
        help="Mark --upsert-user as part of the bug-day schedule/survey.",
    )
    parser.add_argument(
        "--user-manager-only",
        action="store_true",
        help="Allow --upsert-user to manage the bot without being scheduled or surveyed.",
    )
    parser.add_argument(
        "--list-users",
        action="store_true",
        help="Print allowed users from the bot database, then exit.",
    )
    parser.add_argument(
        "--list-surveys",
        action="store_true",
        help="Print surveys from the bot database, then exit.",
    )
    parser.add_argument(
        "--create-survey",
        action="store_true",
        help="Create a survey row from the active DB roster, then exit unless --send-now is used.",
    )
    parser.add_argument(
        "--send-now",
        action="store_true",
        help="Send the survey immediately when used with --create-survey.",
    )
    parser.add_argument(
        "--send-survey-id",
        help="Send forms for an existing scheduled survey ID, then exit.",
    )
    parser.add_argument(
        "--survey-id",
        help="Survey ID for local survey edits.",
    )
    parser.add_argument(
        "--set-survey-status",
        choices=[
            STATUS_SCHEDULED,
            STATUS_COLLECTING,
            STATUS_PENDING_ADMIN_REVIEW,
            STATUS_BLOCKED,
            STATUS_REVISION_REQUESTED,
            STATUS_APPROVED,
            STATUS_CANCELED,
        ],
        help="Set a survey status locally. Use with --survey-id.",
    )
    parser.add_argument(
        "--set-survey-starts-at",
        help="Set survey starts_at locally. Use with --survey-id. Accepts +2m/+2h/+2d or datetime.",
    )
    parser.add_argument(
        "--set-survey-closes-at",
        help="Set survey closes_at locally. Use with --survey-id. Accepts +2m/+2h/+2d or datetime.",
    )
    parser.add_argument(
        "--cancel-survey-id",
        help="Mark a survey canceled locally, then exit.",
    )
    parser.add_argument(
        "--restart-survey-id",
        help="Cancel a non-approved survey and create a new collecting survey for the same month.",
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
        help="Store a DB override for the daily reminder time, formatted HH:MM.",
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


def apply_cli_overrides(settings: BotSettings, args: argparse.Namespace) -> BotSettings:
    updates = {}
    if args.debug:
        updates["debug_enabled"] = True
    if args.survey_start_at:
        updates["survey_start_at"] = args.survey_start_at
    if args.survey_collect_for:
        updates["survey_collect_for"] = args.survey_collect_for
    if args.revision_collect_for:
        updates["revision_collect_for"] = args.revision_collect_for
    if args.target_month:
        year, month = args.target_month.split("-", maxsplit=1)
        updates["target_year"] = int(year)
        updates["target_month"] = int(month)
    if args.database:
        updates["database_path"] = Path(args.database)
    if args.output_dir:
        updates["output_dir"] = Path(args.output_dir)
    return replace(settings, **updates) if updates else settings


def anchor_scheduled_survey_start(
    bot: AdhocTelegramBot,
    anchor: datetime | None = None,
) -> None:
    raw = bot.settings.survey_start_at.strip()
    if not raw or not is_relative_schedule(raw):
        return

    anchor = anchor or datetime.now(ZoneInfo(bot.settings.timezone))
    scheduled = parse_scheduled_datetime(raw, anchor)
    if scheduled is None:
        return

    bot.repo.set_state(
        "scheduled_survey_start",
        {"raw": raw, "start_at": scheduled.isoformat(), "consumed": False},
    )


def reset_target_month_state(bot: AdhocTelegramBot) -> None:
    year, month = bot.target_month()
    survey = bot.active_or_latest_survey_for_month(year, month)
    if survey and survey.status != STATUS_APPROVED:
        bot.repo.update_survey(survey.id, status=STATUS_CANCELED)
        bot.repo.delete_survey_responses(survey.id)
    bot.repo.delete_month_responses(bot.calendar_type, year, month)
    bot.repo.delete_monthly_run(bot.calendar_type, year, month)
    bot.repo.delete_state("active_survey")
    bot.repo.delete_state("active_survey_id")
    bot.repo.delete_state("scheduled_survey_start")
    anchor_scheduled_survey_start(bot)
    logger.info("Reset target month state for %s/%s.", year, month)


def local_datetime_value(raw_value: str, settings: BotSettings) -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    parsed = parse_scheduled_datetime(raw_value, now)
    if parsed is None:
        raise ValueError(f"Invalid datetime value: {raw_value}")
    return parsed.isoformat()


def survey_month_from_args(settings: BotSettings, args: argparse.Namespace) -> tuple[int, int]:
    if args.target_month:
        year, month = args.target_month.split("-", maxsplit=1)
        return int(year), int(month)
    if settings.target_year is not None and settings.target_month is not None:
        return settings.target_year, settings.target_month
    now = datetime.now(ZoneInfo(settings.timezone)).date()
    return survey_target_month(now, settings.calendar, settings.survey_days_before_month)


def create_local_survey(
    repo: BotRepository,
    settings: BotSettings,
    args: argparse.Namespace,
) -> Survey:
    year, month = survey_month_from_args(settings, args)
    now = datetime.now(ZoneInfo(settings.timezone))
    starts_at = (
        parse_scheduled_datetime(args.survey_start_at, now)
        if args.survey_start_at
        else now
    )
    if starts_at is None:
        starts_at = now
    closes_at = (
        parse_scheduled_datetime(args.survey_collect_for, starts_at)
        if args.survey_collect_for
        else None
    )
    return repo.create_survey(
        survey_id=survey_identity(settings.calendar, year, month),
        calendar_type=settings.calendar,
        year=year,
        month=month,
        status=STATUS_SCHEDULED if starts_at > now and not args.send_now else STATUS_COLLECTING,
        starts_at=starts_at.isoformat(),
        closes_at=closes_at.isoformat() if closes_at else "",
        created_by="local-cli",
        participants=repo.list_schedule_members(active_only=True),
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


def handle_local_db_command(
    repo: BotRepository,
    settings: BotSettings,
    args: argparse.Namespace,
) -> bool:
    if args.show_runtime_settings:
        destination = repo.get_telegram_destination()
        if destination:
            print(
                "Telegram destination: "
                f"group_chat_id={destination['group_chat_id']} "
                f"topic_id={destination['topic_id'] if destination['topic_id'] is not None else '-'}"
            )
        else:
            print("Telegram destination: not configured")
        reminder_state = repo.get_state("daily_reminder_time") or {}
        print(f"Daily reminder time: {reminder_state.get('time') or settings.daily_reminder_time}")
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
                f"{survey.id} | {survey.calendar_type} {survey.year}-{survey.month:02d} | "
                f"{survey.status} | starts_at={survey.starts_at or '-'} | "
                f"closes_at={survey.closes_at or '-'} | created_by={survey.created_by}"
            )
        return True

    if args.create_survey and not args.send_now:
        survey = create_local_survey(repo, settings, args)
        print(
            f"Survey created: {survey.id} "
            f"({survey.calendar_type} {survey.year}-{survey.month:02d}, {survey.status})"
        )
        return True

    if args.cancel_survey_id:
        repo.update_survey(args.cancel_survey_id, status=STATUS_CANCELED)
        print(f"Survey canceled: {args.cancel_survey_id}")
        return True

    if args.restart_survey_id:
        old = repo.get_survey(args.restart_survey_id)
        if old is None:
            raise SystemExit(f"Survey not found: {args.restart_survey_id}")
        if old.status == STATUS_APPROVED:
            raise SystemExit(f"Cannot restart approved survey: {old.id}")
        repo.update_survey(old.id, status=STATUS_CANCELED)
        now = datetime.now(ZoneInfo(settings.timezone))
        new_survey = repo.create_survey(
            survey_id=survey_identity(old.calendar_type, old.year, old.month),
            calendar_type=old.calendar_type,
            year=old.year,
            month=old.month,
            status=STATUS_COLLECTING,
            starts_at=now.isoformat(),
            closes_at=local_datetime_value(settings.survey_collect_for, settings)
            if settings.survey_collect_for
            else "",
            created_by=f"local-restart:{old.id}",
            participants=repo.list_schedule_members(active_only=True),
        )
        print(f"Survey restarted: old={old.id} new={new_survey.id}")
        return True

    if args.survey_id and (
        args.set_survey_status or args.set_survey_starts_at or args.set_survey_closes_at
    ):
        updates = {}
        if args.set_survey_status:
            updates["status"] = args.set_survey_status
        if args.set_survey_starts_at:
            updates["starts_at"] = local_datetime_value(args.set_survey_starts_at, settings)
        if args.set_survey_closes_at:
            updates["closes_at"] = local_datetime_value(args.set_survey_closes_at, settings)
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

    if args.set_daily_reminder_time:
        day_time.fromisoformat(args.set_daily_reminder_time)
        repo.set_state("daily_reminder_time", {"time": args.set_daily_reminder_time})
        print(f"Daily reminder time set to: {args.set_daily_reminder_time}")
        return True

    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    settings = load_bot_settings(Path(args.config))
    settings = apply_cli_overrides(settings, args)
    repo = BotRepository(settings.database_path)
    if handle_user_admin_command(repo, args):
        return
    if handle_local_db_command(repo, settings, args):
        return

    members = repo.list_members(active_only=True)
    telegram = TelegramClient(settings.token)
    bot = AdhocTelegramBot(settings, members, repo, telegram)
    if args.create_survey and args.send_now:
        survey = create_local_survey(repo, settings, args)
        bot.send_survey_by_id(survey.id)
        print(f"Survey created and sent: {survey.id}")
        return
    if args.send_survey_id:
        bot.send_survey_by_id(args.send_survey_id)
        print(f"Survey sent: {args.send_survey_id}")
        return
    if args.reset_target_month:
        reset_target_month_state(bot)
    bot.run_forever()


if __name__ == "__main__":
    main()
