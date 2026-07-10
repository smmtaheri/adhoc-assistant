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
    utc_now,
)
from adhoc_assistant.telegram_bot.settings import (
    BotSettings,
    Member,
    load_bot_members,
    load_bot_settings,
)
from adhoc_assistant.telegram_bot.telegram import TelegramClient
from adhoc_assistant.telegram_bot.telegram import TelegramApiError


logger = logging.getLogger(__name__)

STATUS_COLLECTING = "collecting"
STATUS_PENDING_ADMIN_REVIEW = "pending_admin_review"
STATUS_BLOCKED = "blocked"
STATUS_REVISION_REQUESTED = "revision_requested"
STATUS_APPROVED = "approved"
STATUS_CANCELED = "canceled"
ACTIVE_MEMBER_ERROR = "You are not in the active member list."
TEMPORARY_TELEGRAM_ERROR = "Temporary Telegram problem. Please try again in a moment."
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


def relative_schedule_delta(raw_value: str) -> timedelta | None:
    match = re.fullmatch(r"\+?\s*(\d+)\s*([mhd])", raw_value.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


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

    for member in active_members(members):
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
    active = active_members(members)
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

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.settings.admin_telegram_ids

    def is_authorized_user(self, telegram_id: int) -> bool:
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
            for member in active_members(self.members)
            if self.can_message_member_in_current_mode(member)
        ]

    def member_can_receive_revision(self, telegram_id: int) -> bool:
        member = self.find_active_member_by_telegram_id(telegram_id)
        return member is not None and self.can_message_member_in_current_mode(member)

    def member_names_for_ids(self, telegram_ids: list[int]) -> list[str]:
        members_by_id = {member.telegram_id: member for member in active_members(self.members)}
        return [
            members_by_id[telegram_id].name
            for telegram_id in telegram_ids
            if telegram_id in members_by_id
        ]

    def responses_with_current_mode_defaults(
        self,
        responses: dict[int, AvailabilityResponse],
    ) -> dict[int, AvailabilityResponse]:
        effective = dict(responses)
        for member in active_members(self.members):
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

    def start_collecting(self, year: int, month: int, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        deadline_at = self.deadline_at(self.settings.survey_collect_for, now)
        self.repo.set_state(
            "active_survey",
            {
                "calendar": self.calendar_type,
                "year": year,
                "month": month,
                "phase": STATUS_COLLECTING,
                "collect_until": deadline_at.isoformat() if deadline_at else "",
                "allowed_member_ids": [],
            },
        )
        self.repo.upsert_monthly_run(
            self.calendar_type,
            year,
            month,
            STATUS_COLLECTING,
            requested_at=utc_now(),
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
        self.repo.set_state(
            "active_survey",
            {
                "calendar": self.calendar_type,
                "year": year,
                "month": month,
                "phase": STATUS_REVISION_REQUESTED,
                "collect_until": deadline_at.isoformat() if deadline_at else "",
                "allowed_member_ids": sorted(set(member_ids)),
            },
        )
        self.repo.upsert_monthly_run(
            self.calendar_type,
            year,
            month,
            STATUS_REVISION_REQUESTED,
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
                {"raw": raw_start, "start_at": scheduled.isoformat()},
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

        interval = relative_schedule_delta(self.settings.survey_start_at)
        if interval is not None:
            self.ensure_recurring_survey(now, interval)
            return

        due_month = self.scheduled_survey_due(now) or self.survey_month_due(now.date())
        if due_month is None:
            return

        year, month = due_month
        run = self.repo.get_monthly_run(self.calendar_type, year, month)
        if run:
            logger.info(
                "Survey for %s/%s already exists with status %s; not sending requests again.",
                year,
                month,
                run["status"],
            )
            return

        self.send_survey(year, month, now)

    def ensure_recurring_survey(self, now: datetime, interval: timedelta) -> None:
        scheduled = self.scheduled_survey_start_at(now)
        if scheduled is None or now < scheduled:
            return

        year, month = self.target_month(now.date())
        self.reset_cycle_state(year, month)
        self.send_survey(year, month, now)

        next_start = scheduled + interval
        while next_start <= now:
            next_start += interval
        self.repo.set_state(
            "scheduled_survey_start",
            {"raw": self.settings.survey_start_at, "start_at": next_start.isoformat()},
        )
        logger.info("Next recurring survey scheduled for %s.", next_start.isoformat())

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
                {"raw": raw, "start_at": scheduled.isoformat()},
            )
        return scheduled

    def reset_cycle_state(self, year: int, month: int) -> None:
        self.repo.delete_month_responses(self.calendar_type, year, month)
        self.repo.delete_monthly_run(self.calendar_type, year, month)
        self.repo.delete_state("active_survey")

    def send_survey(self, year: int, month: int, now: datetime) -> None:
        logger.info("Starting availability survey for %s/%s.", year, month)
        failed_members = []
        for member in active_members(self.members):
            response = response_for_member(
                self.repo,
                member,
                self.calendar_type,
                year,
                month,
            )
            if not self.can_message_member_in_current_mode(member):
                continue
            self.repo.save_response(self.calendar_type, year, month, response)
            try:
                logger.info("Sending availability request to %s (%s).", member.name, member.telegram_id)
                self.telegram.send_message(
                    chat_id=member.telegram_id,
                    text=(
                        f"Please set your availability for "
                        f"{month_label(year, month, self.calendar_type)}.\n"
                        "Use the buttons only."
                    ),
                    reply_markup=self.response_keyboard(response),
                )
            except TelegramApiError as exc:
                failed_members.append(f"{member.name}: {exc}")

        self.start_collecting(year, month, now=now)
        state = self.active_survey_state() or {}
        logger.info(
            "Availability survey for %s/%s is collecting; collect_until=%s.",
            year,
            month,
            state.get("collect_until") or "month-start/all-responses",
        )
        if failed_members:
            message = (
                "Availability survey started, but these members could not be messaged:\n"
                + "\n".join(failed_members)
            )
            for admin_id in self.settings.admin_telegram_ids:
                self.telegram.send_message(admin_id, message)

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
        state = self.repo.get_state("active_survey")
        if not state:
            return None
        return state["calendar"], int(state["year"]), int(state["month"])

    def active_survey_state(self) -> dict | None:
        return self.repo.get_state("active_survey")

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

    def response_keyboard(self, response: AvailabilityResponse) -> dict:
        return main_availability_keyboard(response)

    def start_member_survey(self, chat_id: int) -> None:
        if not self.is_authorized_user(chat_id):
            message = (
                debug_inactive_message(self.calendar_type)
                if self.settings.debug_enabled
                else unauthorized_message(self.calendar_type)
            )
            self.telegram.send_message(chat_id, message)
            return

        member = self.find_active_member(chat_id)
        if member is None:
            self.telegram.send_message(chat_id, ACTIVE_MEMBER_ERROR)
            return

        active = self.active_survey()
        if active and not self.settings.debug_enabled:
            calendar_type, year, month = active
            state = self.active_survey_state() or {}
            if not self.member_can_edit_active_survey(member, state):
                self.telegram.send_message(chat_id, self.survey_closed_message())
                return
            response = response_for_member(self.repo, member, calendar_type, year, month)
            self.telegram.send_message(
                chat_id,
                availability_summary(response, calendar_type),
                reply_markup=self.response_keyboard(response),
            )
            return

        if not self.settings.debug_enabled:
            self.telegram.send_message(chat_id, "There is no active availability survey right now.")
            return

        year, month = self.target_month()
        run = self.repo.get_monthly_run(self.calendar_type, year, month)
        if self.settings.debug_enabled and chat_id in self.settings.admin_telegram_ids:
            self.reset_debug_survey(year, month)
        elif self.settings.debug_enabled:
            self.repo.delete_response(self.calendar_type, year, month, member.telegram_id)
            if not run:
                self.start_collecting(year, month)
        elif not run:
            self.start_collecting(year, month)

        response = response_for_member(self.repo, member, self.calendar_type, year, month)
        self.repo.save_response(self.calendar_type, year, month, response)
        self.telegram.send_message(
            chat_id,
            availability_summary(response, self.calendar_type),
            reply_markup=self.response_keyboard(response),
        )

    def reset_debug_survey(self, year: int, month: int) -> None:
        self.repo.delete_month_responses(self.calendar_type, year, month)
        self.repo.delete_monthly_run(self.calendar_type, year, month)
        self.start_collecting(year, month)

    def handle_private_text(self, chat_id: int, text: str = "") -> None:
        if not self.is_authorized_user(chat_id):
            message = (
                debug_inactive_message(self.calendar_type)
                if self.settings.debug_enabled
                else unauthorized_message(self.calendar_type)
            )
            self.telegram.send_message(chat_id, message)
            return

        if text.startswith("/start"):
            self.start_member_survey(chat_id)
            return

        if self.is_today_command(text):
            self.send_today_bug_day(chat_id)
            return

        active = self.active_survey()
        if not active:
            self.telegram.send_message(chat_id, "There is no active availability survey right now.")
            return

        calendar_type, year, month = active
        member = self.find_active_member(chat_id)
        if member is None:
            self.telegram.send_message(chat_id, ACTIVE_MEMBER_ERROR)
            return
        state = self.active_survey_state() or {}
        if not self.member_can_edit_active_survey(member, state):
            self.telegram.send_message(chat_id, self.survey_closed_message())
            return

        response = response_for_member(self.repo, member, calendar_type, year, month)
        self.telegram.send_message(
            chat_id,
            availability_summary(response, calendar_type),
            reply_markup=self.response_keyboard(response),
        )

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
        active = self.active_survey()
        if not active:
            self.safe_answer_callback_query(callback["id"], "No active survey.")
            return

        calendar_type, year, month = active
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
        state = self.active_survey_state() or {}
        if not self.member_can_edit_active_survey(member, state):
            self.safe_answer_callback_query(
                callback["id"],
                self.survey_closed_message(),
                show_alert=True,
            )
            return

        response = response_for_member(self.repo, member, calendar_type, year, month)
        data = callback["data"]

        if data == "av:full":
            response.unavailable_days = []
            response.unavailable_weekdays = []
            response.confirmed = True
            response.mode = "full"
            reply_markup = self.response_keyboard(response)
        elif data == "av:custom":
            response.mode = "custom"
            response.confirmed = False
            reply_markup = self.response_keyboard(response)
        elif data == "av:weekdays":
            if response.mode == "full":
                self.safe_answer_callback_query(callback["id"], "Use Change availability first.")
                return
            reply_markup = weekdays_keyboard(response, calendar_type)
        elif data == "av:dates":
            if response.mode == "full":
                self.safe_answer_callback_query(callback["id"], "Use Change availability first.")
                return
            reply_markup = dates_keyboard(response, year, month, calendar_type)
        elif data == "av:back":
            reply_markup = self.response_keyboard(response)
        elif data == "av:confirm":
            response.confirmed = True
            reply_markup = self.response_keyboard(response)
        elif data.startswith("av:w:"):
            response.mode = "custom"
            weekday = data.split(":", maxsplit=2)[2]
            if weekday in response.unavailable_weekdays:
                response.unavailable_weekdays.remove(weekday)
            else:
                response.unavailable_weekdays.append(weekday)
            response.confirmed = False
            reply_markup = weekdays_keyboard(response, calendar_type)
        elif data.startswith("av:d:"):
            response.mode = "custom"
            day = int(data.split(":", maxsplit=2)[2])
            if day in response.unavailable_days:
                response.unavailable_days.remove(day)
            else:
                response.unavailable_days.append(day)
            response.confirmed = False
            reply_markup = dates_keyboard(response, year, month, calendar_type)
        else:
            self.safe_answer_callback_query(callback["id"])
            return

        self.safe_answer_callback_query(callback["id"])

        try:
            self.repo.save_response(calendar_type, year, month, response)
            self.telegram.edit_message_text(
                chat_id=callback["message"]["chat"]["id"],
                message_id=callback["message"]["message_id"],
                text=availability_summary(response, calendar_type),
                reply_markup=reply_markup,
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
                self.create_debug_preview_if_needed(calendar_type, year, month)
            except TelegramApiError as exc:
                logger.warning("Debug preview generation failed for %s: %s", member.name, exc)
                self.safe_send_message(
                    chat_id=telegram_id,
                    text=TEMPORARY_TELEGRAM_ERROR,
                )

    def create_debug_preview_if_needed(self, calendar_type: str, year: int, month: int) -> None:
        self.create_preview(year, month)

    def required_response_members(self) -> list[Member]:
        state = self.active_survey_state() or {}
        phase = state.get("phase", STATUS_COLLECTING)
        members = [
            member for member in self.messageable_active_members()
        ]
        if phase != STATUS_REVISION_REQUESTED:
            return members
        allowed = {int(item) for item in state.get("allowed_member_ids", [])}
        return [member for member in members if member.telegram_id in allowed]

    def all_members_responded(self, year: int, month: int) -> bool:
        responses = self.repo.list_responses(self.calendar_type, year, month)
        for member in self.required_response_members():
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
        collect_until = datetime.fromisoformat(state["collect_until"])
        if collect_until.tzinfo is None:
            collect_until = collect_until.replace(tzinfo=now.tzinfo)
        collect_until = collect_until.astimezone(now.tzinfo)
        return now >= collect_until

    def maybe_create_preview(self, today: date | datetime | None = None) -> None:
        active = self.active_survey()
        if not active:
            return

        calendar_type, year, month = active
        run = self.repo.get_monthly_run(calendar_type, year, month)
        if run and run["status"] in {
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
            not self.all_members_responded(year, month)
            and not self.deadline_reached(today, year, month)
            and not self.collection_deadline_reached(now)
        ):
            return

        self.create_preview(year, month)

    def create_preview(self, year: int, month: int) -> None:
        base_config = load_config(self.settings.schedule_config_path)
        responses = self.responses_with_current_mode_defaults(
            self.repo.list_responses(self.calendar_type, year, month)
        )
        schedule_config = build_schedule_config(
            base_config=base_config,
            members=self.members,
            responses=responses,
            calendar_type=self.calendar_type,
            year=year,
            month=month,
        )
        history = load_history_from_db(
            self.settings.database_path,
            target_year=year,
            target_month=month,
            people_names=[member.name for member in active_members(self.members)],
        )
        schedule, stats = build_schedule(schedule_config, db_history=history)
        review = build_review_report(
            schedule=schedule,
            stats=stats,
            members=self.members,
            responses=responses,
            calendar_type=self.calendar_type,
            year=year,
            month=month,
        )
        status = (
            STATUS_BLOCKED
            if review["blockers"]
            else STATUS_PENDING_ADMIN_REVIEW
        )

        output_dir = self.resolve_output_dir()
        image_path = output_dir / (
            f"adhoc_schedule_{year}_{month:02d}_{uuid4().hex[:8]}.jpg"
        )
        export_image_calendar(schedule, year, month, self.calendar_type, image_path, stats=stats)

        caption = (
            f"Preview for {month_label(year, month, self.calendar_type)}\n\n"
            f"{summary_text(stats, self.calendar_type)}\n\n"
            f"{review_text(review, self.calendar_type)}"
        )
        approval_markup = admin_approval_keyboard(
            year,
            month,
            self.calendar_type,
            status,
            has_flagged_members=bool(review["flagged_member_ids"]),
        )
        for admin_id in self.settings.admin_telegram_ids:
            self.telegram.send_photo(
                chat_id=admin_id,
                photo_path=image_path,
                caption=caption,
                reply_markup=approval_markup,
            )

        self.repo.upsert_monthly_run(
            self.calendar_type,
            year,
            month,
            status,
            preview_sent_at=utc_now(),
            image_path=str(image_path),
            schedule_json=json.dumps(schedule),
            stats_json=json.dumps(stats_to_plain_dict(stats)),
            review_json=json.dumps(review),
        )
        self.repo.delete_state("active_survey")

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
        user_id = int(callback["from"]["id"])
        if user_id not in self.settings.admin_telegram_ids:
            self.safe_answer_callback_query(callback["id"], "Admins only.")
            return

        parts = callback["data"].split(":")
        action = parts[1]
        calendar_type = parts[2]
        year = int(parts[3])
        month = int(parts[4])

        if action not in {"regen", "cancel", "approve", "correct", "reopen", "close", "restart"}:
            self.safe_answer_callback_query(callback["id"])
            return

        self.safe_answer_callback_query(callback["id"])

        run = self.repo.get_monthly_run(calendar_type, year, month)
        if run and run["status"] == STATUS_APPROVED and action != "approve":
            self.safe_send_message(user_id, "This schedule is already approved.")
            return
        if run and run["status"] == STATUS_CANCELED and action not in {"cancel", "restart"}:
            self.safe_send_message(user_id, "This schedule was canceled.")
            return

        if action == "restart":
            now = datetime.now(ZoneInfo(self.settings.timezone))
            self.reset_cycle_state(year, month)
            self.send_survey(year, month, now)
            self.remove_callback_buttons(callback)
            self.safe_send_message(
                user_id,
                self.survey_restarted_message(year, month, calendar_type),
            )
            return

        if action == "close":
            try:
                self.repo.delete_state("active_survey")
                self.create_preview(year, month)
                self.remove_callback_buttons(callback)
            except TelegramApiError as exc:
                logger.warning("Closing revision failed for %s/%s: %s", year, month, exc)
                self.safe_send_message(user_id, TEMPORARY_TELEGRAM_ERROR)
            return

        if action == "regen":
            try:
                self.create_preview(year, month)
                self.remove_callback_buttons(callback)
            except TelegramApiError as exc:
                logger.warning("Preview regeneration failed for %s/%s: %s", year, month, exc)
                self.safe_send_message(user_id, TEMPORARY_TELEGRAM_ERROR)
            return

        if action == "cancel":
            if run and run["status"] == STATUS_APPROVED:
                self.safe_send_message(user_id, "This schedule is already approved.")
                return
            self.repo.upsert_monthly_run(calendar_type, year, month, STATUS_CANCELED)
            self.repo.delete_state("active_survey")
            self.remove_callback_buttons(callback)
            self.safe_send_message(
                user_id,
                self.canceled_message(year, month, calendar_type),
                reply_markup=admin_canceled_keyboard(year, month, calendar_type),
            )
            return

        if action in {"correct", "reopen"}:
            if not run or not run.get("review_json"):
                self.safe_send_message(user_id, "No review report found for corrections.")
                return

            review = json.loads(run["review_json"])
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
            self.send_revision_requests(year, month, member_ids)
            self.remove_callback_buttons(callback)
            self.safe_send_message(
                user_id,
                self.revision_started_message(member_ids),
                reply_markup=admin_revision_keyboard(year, month, calendar_type),
            )
            return

        if not run or not run.get("schedule_json"):
            self.safe_send_message(user_id, "No preview found for approval.")
            return
        if run["status"] == STATUS_CANCELED:
            self.safe_send_message(user_id, "This schedule was canceled.")
            return
        if run["status"] == STATUS_APPROVED:
            self.safe_send_message(user_id, "This schedule is already approved.")
            return
        review = json.loads(run["review_json"] or "{}")
        if review.get("blockers"):
            self.repo.upsert_monthly_run(calendar_type, year, month, STATUS_BLOCKED)
            self.safe_send_message(
                user_id,
                "This schedule has coverage blockers and cannot be approved.",
            )
            return

        schedule = json.loads(run["schedule_json"])
        stats = json.loads(run["stats_json"])
        image_path = Path(run["image_path"])
        caption = group_schedule_caption(year, month, calendar_type)

        try:
            sent_message = self.telegram.send_photo(
                chat_id=self.settings.group_chat_id,
                message_thread_id=self.settings.topic_id,
                photo_path=image_path,
                caption=caption,
            )
            message_id = telegram_message_id(sent_message)
            pin_error = None
            if message_id is not None:
                pin_error = self.safe_pin_chat_message(
                    self.settings.group_chat_id,
                    message_id,
                )
            save_month_to_db(self.settings.database_path, year, month, schedule, stats)
            self.repo.upsert_monthly_run(
                calendar_type,
                year,
                month,
                STATUS_APPROVED,
                approved_at=utc_now(),
                group_sent_at=utc_now(),
            )
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

    def send_revision_requests(self, year: int, month: int, member_ids: list[int]) -> None:
        member_ids_set = set(member_ids)
        for member in active_members(self.members):
            if member.telegram_id not in member_ids_set:
                continue
            if not self.can_message_member_in_current_mode(member):
                continue
            response = response_for_member(
                self.repo,
                member,
                self.calendar_type,
                year,
                month,
            )
            try:
                self.telegram.send_message(
                    chat_id=member.telegram_id,
                    text=self.revision_request_message(year, month),
                    reply_markup=self.response_keyboard(response),
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
        target_time = day_time.fromisoformat(self.settings.daily_reminder_time)
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

        self.telegram.send_message(
            chat_id=self.settings.group_chat_id,
            message_thread_id=self.settings.topic_id,
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
            self.handle_private_text(int(chat["id"]), message.get("text", ""))

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
        {"raw": raw, "start_at": scheduled.isoformat()},
    )


def reset_target_month_state(bot: AdhocTelegramBot) -> None:
    year, month = bot.target_month()
    bot.repo.delete_month_responses(bot.calendar_type, year, month)
    bot.repo.delete_monthly_run(bot.calendar_type, year, month)
    bot.repo.delete_state("active_survey")
    bot.repo.delete_state("scheduled_survey_start")
    anchor_scheduled_survey_start(bot)
    logger.info("Reset target month state for %s/%s.", year, month)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    settings = load_bot_settings(Path(args.config))
    settings = apply_cli_overrides(settings, args)
    members = load_bot_members(settings)
    repo = BotRepository(settings.database_path)
    telegram = TelegramClient(settings.token)
    bot = AdhocTelegramBot(settings, members, repo, telegram)
    if args.reset_target_month:
        reset_target_month_state(bot)
    bot.run_forever()


if __name__ == "__main__":
    main()
