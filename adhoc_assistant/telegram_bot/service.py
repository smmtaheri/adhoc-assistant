import argparse
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta, timezone
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
from adhoc_assistant.config import normalize_holidays, normalize_weekday
from adhoc_assistant.constants import VALID_WEEKDAYS
from adhoc_assistant.exporters import ensure_jpg_export_support, export_image_calendar
from adhoc_assistant.scheduler import build_schedule, is_available, month_workdays, stats_to_plain_dict
from adhoc_assistant.storage import load_history_from_db, save_month_to_db
from adhoc_assistant.telegram_bot.access import (
    access_denied_message,
    resolve_allowlisted_member,
    telegram_display_name,
)
from adhoc_assistant.telegram_bot.keyboards import (
    admin_canceled_keyboard,
    admin_approval_keyboard,
    admin_revision_keyboard,
    availability_summary,
    dates_keyboard,
    main_availability_keyboard,
    start_survey_keyboard,
    weekdays_keyboard,
)
from adhoc_assistant.telegram_bot.messages import BOT_MESSAGE_KEYS, BotMessages, normalize_language
from adhoc_assistant.telegram_bot.repository import (
    AvailabilityResponse,
    BotRepository,
    SURVEY_KIND_DEBUG,
    SURVEY_KIND_PRODUCTION,
    Survey,
    display_name_or_username,
    normalize_username,
    schedule_person_key,
    stable_participant_id,
    utc_now,
)
from adhoc_assistant.telegram_bot.settings import (
    InfraSettings,
    Member,
    RuntimeSettings,
    load_env_file,
    load_infra_settings,
    resolve_database_path,
)
from adhoc_assistant.telegram_bot.telegram import TelegramClient
from adhoc_assistant.telegram_bot.telegram import TelegramApiError


logger = logging.getLogger(__name__)

SERVICE_TEXTS = {
    "en": {
        "monthly_summary": "Monthly summary:",
        "thursday": "Thursday",
        "summary_line": "{name}: main {main}, backup {backup}, total {total}, {thursday} {thursday_count}",
        "schedule_caption": "Adhoc schedule - {month}",
        "approval_sent": "{month} schedule was posted and pinned.",
        "approval_pin_failed": "{month} schedule was posted, but pinning failed with this error:\n{error}",
        "preview_needs_review": "needs review",
        "preview_ready": "ready for review",
        "preview_caption": "Preview for {month}\nStatus: {status}",
        "preview_report_title": "Preview report for {month}",
        "saved": "Availability saved.",
        "saved_refresh_failed": "Saved, but the form could not be refreshed.",
        "schedule_live": "The {month} schedule is live.",
        "main_days": "Main days: {count}",
        "backup_days": "Backup days: {count}",
        "total_days": "Total: {count}",
        "today_schedule": "Today's schedule:",
        "bug_day": "Bug day: {name} ({id})",
        "helper": "Helper: {name} ({id})",
        "nobody": "nobody",
        "canceled": "{month} schedule was canceled.",
        "restarted": "{month} survey was restarted.",
        "correction_members": "Members needing corrections: {names}\nNone of them can be messaged in the current mode. Use Reopen for everyone or check the member config.",
        "revision_started": "Revision window opened for: {names}",
        "revision_request": "The {month} schedule needs review.\nConfirm unchanged if your dates are correct, or update them.",
        "admins_only": "Admins only.",
        "no_survey_action": "No survey found for this action.",
        "already_approved": "This schedule is already approved.",
        "was_canceled": "This schedule was canceled.",
        "no_review": "No review report found for corrections.",
        "no_preview": "No preview found for approval.",
        "cannot_approve_revision": "Cannot approve while revision is in progress. Close revision first.",
        "use_change_first": "Use Change availability first.",
        "review_report": "Review report:",
        "needs_review": "Needs review:",
        "request_corrections_will_be_sent": "Request corrections will be sent to: {names}",
        "availability_heading": "Availability:",
        "review_member_line": "- {name}: unavailable {unavailable}/{workdays}, {percent}% available, {confirmed}",
        "confirmed": "confirmed",
        "not_confirmed": "not confirmed",
    },
    "fa": {
        "monthly_summary": "خلاصه‌ی ماهانه:",
        "thursday": "پنجشنبه",
        "summary_line": "{name}: اصلی {main}، پشتیبان {backup}، مجموع {total}، {thursday} {thursday_count}",
        "schedule_caption": "برنامه‌ی ادهاک {month}",
        "approval_sent": "برنامه‌ی {month} ارسال و پین شد.",
        "approval_pin_failed": "برنامه‌ی {month} ارسال شد، ولی به‌دلیل خطای زیر پین نشد:\n{error}",
        "preview_needs_review": "نیازمند بررسی",
        "preview_ready": "آماده بررسی",
        "preview_caption": "پیش‌نمایش برنامه‌ی {month}\nوضعیت: {status}",
        "preview_report_title": "گزارش پیش‌نمایش {month}",
        "saved": "ثبت شد.",
        "saved_refresh_failed": "ثبت شد، ولی فرم به‌روزرسانی نشد.",
        "schedule_live": "برنامه‌ی {month} منتشر شد.",
        "main_days": "روزهای اصلی: {count}",
        "backup_days": "روزهای پشتیبان: {count}",
        "total_days": "مجموع: {count}",
        "today_schedule": "برنامه‌ی امروز:",
        "bug_day": "روز باگ: {name} ({id})",
        "helper": "پشتیبان: {name} ({id})",
        "nobody": "هیچ‌کس",
        "canceled": "برنامه‌ی {month} کنسل شد.",
        "restarted": "نظرسنجی {month} دوباره شروع شد.",
        "correction_members": "افراد نیازمند اصلاح: {names}\nدر حالت فعلی نمی‌شود به هیچ‌کدام پیام داد. می‌توانی Reopen for everyone را بزنی یا کانفیگ اعضا را بررسی کنی.",
        "revision_started": "پنجره‌ی اصلاح برای این افراد باز شد: {names}",
        "revision_request": "برنامه‌ی {month} نیاز به اصلاح دارد.\nاگر روزها درست است Confirm را بزن؛ اگر اشتباه است اصلاح کن.",
        "admins_only": "فقط ادمین‌ها اجازه دارند.",
        "no_survey_action": "برای این عملیات نظرسنجی پیدا نشد.",
        "already_approved": "این برنامه قبلا تایید شده است.",
        "was_canceled": "این برنامه کنسل شده است.",
        "no_review": "گزارش بررسی برای اصلاح پیدا نشد.",
        "no_preview": "پیش‌نمایشی برای تایید پیدا نشد.",
        "cannot_approve_revision": "در زمان اصلاح نمی‌شود تایید کرد. اول اصلاحات را ببند.",
        "use_change_first": "اول Change availability را بزن.",
        "review_report": "گزارش بررسی:",
        "needs_review": "نیازمند بررسی:",
        "request_corrections_will_be_sent": "در صورت زدن Request corrections برای این افراد ارسال می‌شود: {names}",
        "availability_heading": "وضعیت افراد:",
        "review_member_line": "- {name}: {unavailable}/{workdays} روز نیست، {percent}٪ حاضر، {confirmed}",
        "confirmed": "تایید شده",
        "not_confirmed": "تایید نشده",
    },
    "ar": {
        "monthly_summary": "ملخص شهري:",
        "thursday": "الخميس",
        "summary_line": "{name}: أساسي {main}، احتياطي {backup}، المجموع {total}، {thursday} {thursday_count}",
        "schedule_caption": "جدول Adhoc - {month}",
        "approval_sent": "تم نشر جدول {month} وتثبيته.",
        "approval_pin_failed": "تم نشر جدول {month}، لكن فشل التثبيت بسبب:\n{error}",
        "preview_needs_review": "يحتاج إلى مراجعة",
        "preview_ready": "جاهز للمراجعة",
        "preview_caption": "معاينة جدول {month}\nالحالة: {status}",
        "preview_report_title": "تقرير معاينة {month}",
        "saved": "تم الحفظ.",
        "saved_refresh_failed": "تم الحفظ، لكن تعذر تحديث النموذج.",
        "schedule_live": "تم نشر جدول {month}.",
        "main_days": "الأيام الأساسية: {count}",
        "backup_days": "أيام الاحتياط: {count}",
        "total_days": "المجموع: {count}",
        "today_schedule": "جدول اليوم:",
        "bug_day": "المسؤول الأساسي: {name} ({id})",
        "helper": "الاحتياطي: {name} ({id})",
        "nobody": "لا أحد",
        "canceled": "تم إلغاء جدول {month}.",
        "restarted": "تمت إعادة بدء استبيان {month}.",
        "correction_members": "الأعضاء الذين يحتاجون إلى تعديل: {names}\nلا يمكن مراسلة أي منهم حاليا. استخدم Reopen for everyone أو راجع إعدادات الأعضاء.",
        "revision_started": "تم فتح نافذة التعديل لـ: {names}",
        "revision_request": "جدول {month} يحتاج إلى مراجعة.\nأكد إذا كانت تواريخك صحيحة، أو عدلها.",
        "admins_only": "للمسؤولين فقط.",
        "no_survey_action": "لم يتم العثور على استبيان لهذا الإجراء.",
        "already_approved": "تم اعتماد هذا الجدول مسبقا.",
        "was_canceled": "تم إلغاء هذا الجدول.",
        "no_review": "لا يوجد تقرير مراجعة للتعديلات.",
        "no_preview": "لا توجد معاينة للاعتماد.",
        "cannot_approve_revision": "لا يمكن الاعتماد أثناء التعديل. أغلق التعديل أولا.",
        "use_change_first": "استخدم تعديل التوافر أولا.",
        "review_report": "تقرير المراجعة:",
        "needs_review": "يحتاج إلى مراجعة:",
        "request_corrections_will_be_sent": "سيتم إرسال طلب التعديل إلى: {names}",
        "availability_heading": "التوافر:",
        "review_member_line": "- {name}: غير متاح {unavailable}/{workdays}، متاح {percent}%، {confirmed}",
        "confirmed": "تم التأكيد",
        "not_confirmed": "غير مؤكد",
    },
    "ru": {
        "monthly_summary": "Месячная сводка:",
        "thursday": "Четверг",
        "summary_line": "{name}: основной {main}, резерв {backup}, всего {total}, {thursday} {thursday_count}",
        "schedule_caption": "График Adhoc - {month}",
        "approval_sent": "График {month} опубликован и закреплен.",
        "approval_pin_failed": "График {month} опубликован, но закрепить его не удалось:\n{error}",
        "preview_needs_review": "нужна проверка",
        "preview_ready": "готово к проверке",
        "preview_caption": "Превью графика {month}\nСтатус: {status}",
        "preview_report_title": "Отчет превью {month}",
        "saved": "Сохранено.",
        "saved_refresh_failed": "Сохранено, но форму не удалось обновить.",
        "schedule_live": "График {month} опубликован.",
        "main_days": "Основные дни: {count}",
        "backup_days": "Резервные дни: {count}",
        "total_days": "Всего: {count}",
        "today_schedule": "График на сегодня:",
        "bug_day": "Основной: {name} ({id})",
        "helper": "Резерв: {name} ({id})",
        "nobody": "никто",
        "canceled": "График {month} отменен.",
        "restarted": "Опрос {month} перезапущен.",
        "correction_members": "Участники для исправления: {names}\nСейчас никому из них нельзя отправить сообщение. Используйте Reopen for everyone или проверьте настройки участников.",
        "revision_started": "Окно исправлений открыто для: {names}",
        "revision_request": "График {month} требует проверки.\nПодтвердите, если даты верны, или измените их.",
        "admins_only": "Только для администраторов.",
        "no_survey_action": "Опрос для этого действия не найден.",
        "already_approved": "Этот график уже утвержден.",
        "was_canceled": "Этот график был отменен.",
        "no_review": "Отчет проверки для исправлений не найден.",
        "no_preview": "Превью для утверждения не найдено.",
        "cannot_approve_revision": "Нельзя утвердить во время исправлений. Сначала закройте исправления.",
        "use_change_first": "Сначала нажмите Change availability.",
        "review_report": "Отчет проверки:",
        "needs_review": "Нужна проверка:",
        "request_corrections_will_be_sent": "Запрос исправлений будет отправлен: {names}",
        "availability_heading": "Доступность:",
        "review_member_line": "- {name}: недоступен {unavailable}/{workdays}, доступен {percent}%, {confirmed}",
        "confirmed": "подтверждено",
        "not_confirmed": "не подтверждено",
    },
}


def service_text(key: str, language: str | None, calendar_type: str, **values) -> str:
    lang = normalize_language(language, calendar_type)
    template = SERVICE_TEXTS[lang][key]
    return template.format(**values)

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
ADMIN_MUTABLE_SURVEY_STATUSES = EDITABLE_SURVEY_STATUSES | {STATUS_PENDING_ADMIN_REVIEW}
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
ALL_SURVEY_STATUSES = {
    STATUS_SCHEDULED,
    STATUS_COLLECTING,
    STATUS_PENDING_ADMIN_REVIEW,
    STATUS_BLOCKED,
    STATUS_REVISION_REQUESTED,
    STATUS_PUBLISHING,
    STATUS_APPROVED,
    STATUS_CANCELED,
}
# --set-survey-status only allows these guarded recovery transitions.
# Preview/revision/approve/publish stay on their real business flows.
CLI_SURVEY_STATUS_TRANSITIONS: dict[str, set[str]] = {
    STATUS_SCHEDULED: {STATUS_COLLECTING, STATUS_CANCELED},
    STATUS_COLLECTING: {STATUS_CANCELED},
    STATUS_PENDING_ADMIN_REVIEW: {STATUS_CANCELED},
    STATUS_REVISION_REQUESTED: {STATUS_CANCELED},
    STATUS_BLOCKED: {STATUS_CANCELED},
    STATUS_PUBLISHING: {STATUS_CANCELED},
}
CLI_SETTABLE_SURVEY_STATUSES = sorted(
    {target for targets in CLI_SURVEY_STATUS_TRANSITIONS.values() for target in targets}
)
TEMPORARY_TELEGRAM_ERROR = "Temporary Telegram problem. Please try again in a moment."
TELEGRAM_PHOTO_CAPTION_LIMIT = 1024
TELEGRAM_MESSAGE_TEXT_LIMIT = 4096
MAIN_IMBALANCE_THRESHOLD = 2
HIGH_UNAVAILABLE_MIN_DAYS = 5
HIGH_UNAVAILABLE_EXTRA_DAYS = 3
HIGH_UNAVAILABLE_RATIO = 0.35
FAIRNESS_RISK_UNAVAILABLE_RATIO = 0.7
NO_MAIN = "NO_AVAILABLE_PERSON"
NO_BACKUP = "NO_AVAILABLE_BACKUP"


def configure_logging_from_env() -> None:
    load_env_file(Path(".env"))
    raw_level = (
        os.environ.get("ADHOC_LOG_LEVEL")
        or os.environ.get("LOG_LEVEL")
        or "INFO"
    )
    level_name = raw_level.strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level_name = "INFO"
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).debug("Logging configured level=%s", level_name)


def update_kind(update: dict) -> str:
    if "callback_query" in update:
        return "callback_query"
    if "message" in update:
        return "message"
    return "unknown"


def update_telegram_date(update: dict) -> tuple[float | None, str]:
    if "message" in update:
        raw_date = update.get("message", {}).get("date")
        source = "message.date"
    elif "callback_query" in update:
        callback = update.get("callback_query", {})
        raw_date = callback.get("message", {}).get("date")
        source = "callback.message.date"
    else:
        raw_date = None
        source = "none"
    if raw_date is None:
        return None, source
    try:
        return float(raw_date), source
    except (TypeError, ValueError):
        return None, source


def epoch_utc_label(raw_epoch: float | None) -> str:
    if raw_epoch is None:
        return "unknown"
    return datetime.fromtimestamp(raw_epoch, timezone.utc).isoformat()


def update_age_seconds(update: dict, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    raw_date, _source = update_telegram_date(update)
    if raw_date is None:
        return None
    return max(0.0, now.timestamp() - raw_date)


def callback_query_id(update: dict) -> str | None:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    value = callback.get("id")
    return str(value) if value not in (None, "") else None


def stable_worker_index(partition_key: str, worker_count: int) -> int:
    digest = hashlib.sha256(partition_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % worker_count


@dataclass(frozen=True)
class QueuedTelegramUpdate:
    update: dict
    update_id: int
    partition_key: str
    worker_id: int
    enqueued_monotonic: float


class PartitionedTelegramUpdateDispatcher:
    max_update_attempts = 3

    def __init__(
        self,
        bot: "AdhocTelegramBot",
        *,
        worker_count: int,
        queue_maxsize: int,
    ) -> None:
        self.bot = bot
        self.worker_count = max(1, int(worker_count))
        self.queues = [
            queue.Queue(maxsize=max(1, int(queue_maxsize)))
            for _ in range(self.worker_count)
        ]
        self.stop_event = threading.Event()
        self.offset_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.workers: list[threading.Thread] = []
        self.duplicate_skipped_count = 0
        self.failed_update_count = 0
        self.offset = bot.repo.get_update_offset()

    def start(self) -> None:
        if self.workers:
            return
        self.bot.repo.reset_incomplete_telegram_updates()
        for worker_id in range(self.worker_count):
            worker = threading.Thread(
                target=self.worker_loop,
                args=(worker_id,),
                name=f"telegram-worker-{worker_id}",
                daemon=True,
            )
            worker.start()
            self.workers.append(worker)

    def stop(self, timeout: float | None = None) -> None:
        self.stop_event.set()
        for worker in self.workers:
            worker.join(timeout=timeout)

    def queue_depth(self) -> int:
        return sum(item.qsize() for item in self.queues)

    def enqueue_update(self, update: dict) -> bool:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            logger.warning("Skipping Telegram update without integer update_id: %s", update)
            return False

        partition_key = self.bot.update_partition_key(update)
        worker_id = stable_worker_index(partition_key, self.worker_count)
        claimed = self.bot.repo.claim_telegram_update(
            update_id=update_id,
            callback_query_id=callback_query_id(update),
            partition_key=partition_key,
            worker_id=worker_id,
        )
        if not claimed:
            with self.metrics_lock:
                self.duplicate_skipped_count += 1
                duplicate_skipped_count = self.duplicate_skipped_count
            logger.debug(
                "telegram update duplicate skipped update_id=%s callback_query_id=%s "
                "duplicate_skipped_count=%s",
                update_id,
                callback_query_id(update),
                duplicate_skipped_count,
            )
            self.advance_offset()
            return False

        queue_started = time.monotonic()
        item = QueuedTelegramUpdate(
            update=update,
            update_id=update_id,
            partition_key=partition_key,
            worker_id=worker_id,
            enqueued_monotonic=time.monotonic(),
        )
        self.queues[worker_id].put(item)
        self.bot.repo.mark_telegram_update_enqueued(update_id)
        logger.debug(
            "telegram update enqueued update_id=%s worker_id=%s partition=%s "
            "queue_depth=%s enqueue_latency_ms=%.1f",
            update_id,
            worker_id,
            partition_key,
            self.queue_depth(),
            (time.monotonic() - queue_started) * 1000,
        )
        return True

    def worker_loop(self, worker_id: int) -> None:
        worker_queue = self.queues[worker_id]
        while not self.stop_event.is_set():
            try:
                item = worker_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle_queued_update(item)
            finally:
                worker_queue.task_done()

    def handle_queued_update(self, item: QueuedTelegramUpdate) -> None:
        handle_started = time.monotonic()
        time_in_queue_ms = (handle_started - item.enqueued_monotonic) * 1000
        kind = update_kind(item.update)
        age = update_age_seconds(item.update)
        raw_date, age_source = update_telegram_date(item.update)
        local_now = datetime.now(timezone.utc)
        logger.debug(
            "update handle start update_id=%s kind=%s worker_id=%s partition=%s "
            "queue_depth=%s time_in_queue_ms=%.1f age_seconds=%s "
            "age_source=%s telegram_date_epoch=%s telegram_date_utc=%s "
            "local_now_epoch=%.3f local_now_utc=%s",
            item.update_id,
            kind,
            item.worker_id,
            item.partition_key,
            self.queue_depth(),
            time_in_queue_ms,
            f"{age:.3f}" if age is not None else "unknown",
            age_source,
            f"{raw_date:.3f}" if raw_date is not None else "unknown",
            epoch_utc_label(raw_date),
            local_now.timestamp(),
            local_now.isoformat(),
        )
        self.bot.repo.mark_telegram_update_started(item.update_id)
        try:
            self.bot.handle_update(item.update)
        except Exception as exc:
            with self.metrics_lock:
                self.failed_update_count += 1
                failed_update_count = self.failed_update_count
            status = self.bot.repo.mark_telegram_update_failed(
                item.update_id,
                str(exc),
                max_attempts=self.max_update_attempts,
            )
            logger.exception(
                "Handling Telegram update failed update_id=%s worker_id=%s "
                "partition=%s status=%s failed_update_count=%s update=%s",
                item.update_id,
                item.worker_id,
                item.partition_key,
                status,
                failed_update_count,
                item.update,
            )
        else:
            self.bot.repo.mark_telegram_update_processed(item.update_id)
        finally:
            logger.debug(
                "update handle done update_id=%s kind=%s worker_id=%s partition=%s "
                "queue_depth=%s elapsed_ms=%.1f",
                item.update_id,
                kind,
                item.worker_id,
                item.partition_key,
                self.queue_depth(),
                (time.monotonic() - handle_started) * 1000,
            )
            self.advance_offset()

    def advance_offset(self) -> int | None:
        with self.offset_lock:
            self.offset = self.bot.repo.advance_update_offset_from_tracking(self.offset)
            return self.offset


def resolve_cli_survey_kind(args: argparse.Namespace) -> str:
    """Resolve survey kind for create/direct-preview/publish-now.

    Explicit --survey-kind always wins. Otherwise --direct-preview / --publish-now
    default to debug; other create paths default to production.
    """
    explicit = getattr(args, "survey_kind", None)
    if explicit:
        return explicit
    if getattr(args, "direct_preview", False) or getattr(args, "publish_now", False):
        return SURVEY_KIND_DEBUG
    return SURVEY_KIND_PRODUCTION


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


def parse_int_csv(raw: str) -> list[int]:
    text = raw.strip()
    if not text:
        return []
    values: list[int] = []
    seen: set[int] = set()
    for item in text.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(f"Invalid day number: {token!r}") from exc
        if value < 1 or value > 31:
            raise ValueError(f"Invalid day number: {value}; expected 1..31.")
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values


def parse_weekday_csv(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in text.split(","):
        token = item.strip()
        if not token:
            continue
        weekday = normalize_weekday(token)
        if weekday not in VALID_WEEKDAYS:
            allowed = ", ".join(sorted(VALID_WEEKDAYS))
            raise ValueError(f"Invalid weekday: {token!r}. Allowed: {allowed}.")
        if weekday not in seen:
            seen.add(weekday)
            values.append(weekday)
    return values


def resolve_survey_participant(survey: Survey, selector: str, repo: BotRepository) -> Member | None:
    wanted = selector.strip().lower().lstrip("@")
    if not wanted:
        return None
    for member in repo.list_survey_participants(survey.id):
        candidates = {
            normalize_username(member.username),
            member.name.strip().lower(),
            str(member.telegram_id),
        }
        if wanted in candidates:
            return member
    return None


def set_survey_availability_response(
    repo: BotRepository,
    survey_id: str,
    member_selector: str,
    *,
    unavailable_days: list[int] | None = None,
    unavailable_weekdays: list[str] | None = None,
    mode: str = "custom",
) -> tuple[Survey, Member, AvailabilityResponse]:
    survey = repo.get_survey(survey_id)
    if survey is None:
        raise ValueError(f"Survey not found: {survey_id}")
    if survey.status not in ADMIN_MUTABLE_SURVEY_STATUSES:
        raise ValueError(
            f"Can only set availability while survey is collecting, in revision, "
            f"or pending admin review; "
            f"{survey.id} status={survey.status}."
        )
    member = resolve_survey_participant(survey, member_selector, repo)
    if member is None:
        raise ValueError(f"Survey participant not found: {member_selector}")
    response = response_for_survey_member(repo, survey.id, member)
    if mode == "full":
        response.mode = "full"
        response.unavailable_days = []
        response.unavailable_weekdays = []
    elif mode == "custom":
        response.mode = "custom"
        if unavailable_days is not None:
            response.unavailable_days = sorted(unavailable_days)
        if unavailable_weekdays is not None:
            response.unavailable_weekdays = sorted(unavailable_weekdays)
    else:
        raise ValueError(f"Unsupported availability mode: {mode}")
    response.confirmed = True
    repo.save_survey_response(survey.id, response)
    repo.remove_revision_allowlist_members(survey, [member.telegram_id])
    return survey, member, response


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
        "available_count": workday_count - unavailable_count,
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
    availability_fairness_risks = []
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
    expected_main_floor = (
        len(month_workdays(year, month, calendar_type)) // len(active)
        if active
        else 0
    )
    for item in availability:
        if not item["confirmed"] and item["telegram_id"] > 0:
            flagged_member_ids.add(item["telegram_id"])
        unavailable_ratio = item["unavailable_count"] / max(item["workday_count"], 1)
        high_unavailable = (
            item["unavailable_count"] >= HIGH_UNAVAILABLE_MIN_DAYS
            and item["unavailable_count"] >= average_unavailable + HIGH_UNAVAILABLE_EXTRA_DAYS
            and unavailable_ratio >= HIGH_UNAVAILABLE_RATIO
        )
        fairness_blocker = (
            item["available_count"] < expected_main_floor
            or unavailable_ratio >= FAIRNESS_RISK_UNAVAILABLE_RATIO
        )
        if high_unavailable or fairness_blocker:
            availability_fairness_risks.append(
                {
                    "telegram_id": item["telegram_id"],
                    "name": item["name"],
                    "available_count": item["available_count"],
                    "unavailable_count": item["unavailable_count"],
                    "workday_count": item["workday_count"],
                    "expected_main_floor": expected_main_floor,
                    "reason": "fairness_blocker" if fairness_blocker else "high_unavailable",
                }
            )
            if item["telegram_id"] > 0:
                flagged_member_ids.add(item["telegram_id"])

    if availability_fairness_risks:
        details = ", ".join(
            f"{item['name']} ({item['available_count']}/{item['workday_count']} available)"
            for item in availability_fairness_risks
        )
        warnings.append(
            "Availability fairness risk: "
            f"{details}. The bot may not be able to build a fair schedule. "
            "Use Request corrections to resend their forms, or approve if this is expected."
        )

    status = "needs_review" if warnings else "ready"

    return {
        "status": status,
        "coverage_warnings": coverage_warnings,
        "availability_fairness_risks": availability_fairness_risks,
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


def review_text(review: dict, calendar_type: str, language: str | None = None) -> str:
    lang = normalize_language(language, calendar_type)
    lines = [service_text("review_report", lang, calendar_type)]
    if review.get("warnings"):
        lines.append(service_text("needs_review", lang, calendar_type))
        lines.extend(f"- {warning}" for warning in review["warnings"])
    if review.get("flagged_member_names"):
        lines.append(
            service_text(
                "request_corrections_will_be_sent",
                lang,
                calendar_type,
                names=format_names(review["flagged_member_names"], calendar_type, lang),
            )
        )
    lines.append(service_text("availability_heading", lang, calendar_type))
    for item in review["availability"]:
        confirmed = service_text("confirmed" if item["confirmed"] else "not_confirmed", lang, calendar_type)
        lines.append(
            service_text(
                "review_member_line",
                lang,
                calendar_type,
                name=item["name"],
                unavailable=item["unavailable_count"],
                workdays=item["workday_count"],
                percent=item["availability_percent"],
                confirmed=confirmed,
            )
        )
    return "\n".join(lines)


def summary_text(stats: dict, calendar_type: str, language: str | None = None) -> str:
    thursday = service_text("thursday", language, calendar_type)
    lines = [service_text("monthly_summary", language, calendar_type)]
    for name, values in sorted(stats_to_plain_dict(stats).items()):
        lines.append(
            service_text(
                "summary_line",
                language,
                calendar_type,
                name=name,
                main=values["main_count"],
                backup=values["backup_count"],
                total=values["total_count"],
                thursday=thursday,
                thursday_count=values["thursday_count"],
            )
        )
    return "\n".join(lines)


def month_label(year: int, month: int, calendar_type: str) -> str:
    return format_month_title(year, month, calendar_type)


def group_schedule_caption(
    year: int,
    month: int,
    calendar_type: str,
    language: str | None = None,
) -> str:
    return service_text(
        "schedule_caption",
        language,
        calendar_type,
        month=month_label(year, month, calendar_type),
    )


def approval_sent_message(
    year: int,
    month: int,
    calendar_type: str,
    language: str | None = None,
) -> str:
    return service_text(
        "approval_sent",
        language,
        calendar_type,
        month=month_label(year, month, calendar_type),
    )


def approval_posted_pin_failed_message(
    year: int,
    month: int,
    calendar_type: str,
    error: str,
    language: str | None = None,
) -> str:
    return service_text(
        "approval_pin_failed",
        language,
        calendar_type,
        month=month_label(year, month, calendar_type),
        error=error,
    )


def preview_photo_caption(
    year: int,
    month: int,
    calendar_type: str,
    review: dict,
    language: str | None = None,
) -> str:
    status_key = "preview_needs_review" if review.get("warnings") else "preview_ready"
    status = service_text(status_key, language, calendar_type)
    caption = service_text(
        "preview_caption",
        language,
        calendar_type,
        month=month_label(year, month, calendar_type),
        status=status,
    )
    return caption[:TELEGRAM_PHOTO_CAPTION_LIMIT]


def preview_report_message(
    year: int,
    month: int,
    calendar_type: str,
    stats: dict,
    review: dict,
    language: str | None = None,
) -> str:
    title = service_text(
        "preview_report_title",
        language,
        calendar_type,
        month=month_label(year, month, calendar_type),
    )
    return (
        f"{title}\n\n"
        f"{summary_text(stats, calendar_type, language)}\n\n"
        f"{review_text(review, calendar_type, language)}"
    )


def availability_saved_message(calendar_type: str, language: str | None = None) -> str:
    return service_text("saved", language, calendar_type)


def availability_saved_but_refresh_failed_message(
    calendar_type: str,
    language: str | None = None,
) -> str:
    return service_text("saved_refresh_failed", language, calendar_type)


def availability_confirmed_form_text(
    survey: Survey,
    response: AvailabilityResponse,
    survey_label: str,
    language: str | None = None,
) -> str:
    return (
        f"{availability_saved_message(survey.calendar_type, language)}\n\n"
        + availability_summary(
            response,
            survey.calendar_type,
            survey_label=survey_label,
            language=language,
        )
    )


def assignment_summary_message(
    member: Member,
    survey: Survey,
    schedule: list[dict],
    language: str | None = None,
) -> str:
    main_days = []
    backup_days = []
    for item in schedule:
        label = f"{item['date']} ({item['weekday']})"
        if item.get("main") == member.name:
            main_days.append(label)
        if item.get("backup") == member.name:
            backup_days.append(label)

    lines = [
        service_text(
            "schedule_live",
            language,
            survey.calendar_type,
            month=month_label(survey.year, survey.month, survey.calendar_type),
        ),
        service_text("main_days", language, survey.calendar_type, count=len(main_days)),
    ]
    lines.extend(f"- {item}" for item in main_days)
    lines.append(service_text("backup_days", language, survey.calendar_type, count=len(backup_days)))
    lines.extend(f"- {item}" for item in backup_days)
    lines.append(
        service_text(
            "total_days",
            language,
            survey.calendar_type,
            count=len(main_days) + len(backup_days),
        )
    )
    return "\n".join(lines)


def display_label_for_member(repo: BotRepository, member: Member) -> str:
    username = normalize_username(member.username)
    if username:
        user = repo.get_user_by_username(username)
        if user is not None:
            return display_name_or_username(user["display_name"], user["username"])
    return display_name_or_username(member.name, member.username)


def relabel_schedule_outputs(
    repo: BotRepository,
    *,
    schedule: list[dict],
    stats: dict,
    review: dict,
    members: list[Member],
) -> tuple[list[dict], dict, dict]:
    label_by_key = {
        schedule_person_key(member): display_label_for_member(repo, member)
        for member in schedule_members(members)
    }

    def relabel_name(name: str) -> str:
        return label_by_key.get(name, name)

    relabeled_schedule = [
        {
            **item,
            "main": relabel_name(item["main"]),
            "backup": relabel_name(item["backup"]),
        }
        for item in schedule
    ]
    relabeled_stats = {
        relabel_name(name): values for name, values in stats_to_plain_dict(stats).items()
    }
    relabeled_review = deepcopy(review)
    for warning in relabeled_review.get("coverage_warnings", []):
        warning["available"] = [relabel_name(name) for name in warning.get("available", [])]
        warning["unavailable"] = [relabel_name(name) for name in warning.get("unavailable", [])]
    for item in relabeled_review.get("availability_fairness_risks", []):
        item["name"] = relabel_name(item["name"])
    relabeled_review["flagged_member_names"] = [
        relabel_name(name) for name in relabeled_review.get("flagged_member_names", [])
    ]
    relabeled_review["unregistered_member_names"] = [
        relabel_name(name) for name in relabeled_review.get("unregistered_member_names", [])
    ]
    for item in relabeled_review.get("availability", []):
        item["name"] = relabel_name(item["name"])
    return relabeled_schedule, relabeled_stats, relabeled_review


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


def today_bug_day_message(
    main_member: Member | None,
    backup_member: Member | None,
    main_name: str,
    backup_name: str,
    calendar_type: str,
    language: str | None = None,
) -> str:
    main_id = main_member.telegram_id if main_member else "unknown"
    backup_id = backup_member.telegram_id if backup_member else "unknown"
    return (
        service_text("today_schedule", language, calendar_type)
        + "\n"
        + service_text("bug_day", language, calendar_type, name=main_name, id=main_id)
        + "\n"
        + service_text("helper", language, calendar_type, name=backup_name, id=backup_id)
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


def format_names(names: list[str], calendar_type: str, language: str | None = None) -> str:
    lang = normalize_language(language, calendar_type)
    if not names:
        return service_text("nobody", lang, calendar_type)
    return "، ".join(names) if lang in {"fa", "ar"} else ", ".join(names)


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
        self.update_dispatcher = PartitionedTelegramUpdateDispatcher(
            self,
            worker_count=self.runtime.telegram_worker_count,
            queue_maxsize=self.runtime.telegram_queue_maxsize,
        )
        self.scheduled_stop_event = threading.Event()
        self.scheduled_thread: threading.Thread | None = None

    @property
    def runtime(self) -> RuntimeSettings:
        if self._runtime_cache is None:
            self._runtime_cache = self.repo.get_runtime_settings()
        return self._runtime_cache

    def refresh_runtime(self) -> RuntimeSettings:
        self._runtime_cache = self.repo.get_runtime_settings()
        return self._runtime_cache

    @property
    def messages(self) -> BotMessages:
        return self.runtime.bot_messages

    @property
    def language(self) -> str:
        return self.runtime.language

    @property
    def calendar_type(self) -> str:
        return normalize_calendar_type(self.runtime.calendar)

    @property
    def timezone_name(self) -> str:
        return self.runtime.timezone

    def find_active_member(self, telegram_id: int) -> Member | None:
        """Allowlisted active bot_users row bound to this Telegram id."""
        user = self.repo.get_user_by_telegram_id(int(telegram_id))
        if user is None or not user["active"]:
            return None
        return self.repo.user_to_member(user)

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
        return self.find_active_member(telegram_id) is not None

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
            language=self.language,
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

    def confirmed_locked_survey_for_member(self, member: Member) -> Survey | None:
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
            if self.collection_deadline_reached(survey=survey):
                continue
            response = self.repo.get_survey_response(survey.id, member.telegram_id)
            if not response or not response.confirmed:
                continue
            state = self.survey_state_for(survey)
            if state.get("phase", STATUS_COLLECTING) != STATUS_COLLECTING:
                continue
            allowed = {int(item) for item in state.get("allowed_member_ids", [])}
            if member.telegram_id not in allowed:
                return survey
        return None

    def survey_state_for(self, survey: Survey) -> dict:
        """Runtime view of survey state.

        surveys.status and surveys.closes_at are authoritative. Revision
        targeting allowlists come from the active_survey:{kind} blob only.
        """
        return {
            "id": survey.id,
            "kind": survey.kind,
            "calendar": survey.calendar_type,
            "year": survey.year,
            "month": survey.month,
            "phase": survey.status,
            "collect_until": survey.closes_at,
            "allowed_member_ids": self.repo.get_revision_allowlist(survey),
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
        # survey_responses is the only writable response store.
        self.repo.save_survey_response(survey.id, response)

    def responses_for_survey(self, survey: Survey) -> dict[int, AvailabilityResponse]:
        return self.repo.list_survey_responses(survey.id)

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
        replace_active: bool = False,
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
            replace_active=replace_active,
        )
        self.repo.clear_revision_allowlist(kind)
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
                created_by="start_collecting",
            )
        else:
            self.repo.update_survey(
                survey.id,
                status=STATUS_COLLECTING,
                starts_at=survey.starts_at or now.isoformat(),
                closes_at=survey.closes_at or self.configured_closes_at(now),
            )
            survey = self.repo.get_survey(survey.id) or survey
        self.repo.clear_revision_allowlist(survey.kind)

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
        allowed = sorted(set(member_ids))
        # Prior confirms must not satisfy revision; members get a real re-confirm window.
        self.repo.unconfirm_survey_responses(survey.id, allowed)
        self.repo.set_revision_allowlist(survey, allowed)
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
                created_by="send_survey",
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
                self.send_or_edit_member_invite(survey, member)
            except TelegramApiError as exc:
                failed_members.append(f"{member.name}: {exc}")

        self.repo.clear_revision_allowlist(survey.kind)
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

    def add_survey_participants_and_send(
        self,
        survey_id: str,
        participants: list[Member],
    ) -> dict:
        survey = self.repo.get_survey(survey_id)
        if survey is None:
            raise ValueError(f"Survey not found: {survey_id}")
        if survey.status == STATUS_CANCELED:
            now = datetime.now(ZoneInfo(self.timezone_name))
            self.repo.update_survey(
                survey.id,
                status=STATUS_COLLECTING,
                closes_at=self.configured_closes_at(now),
            )
            survey = self.repo.get_survey(survey.id) or survey
        if survey.status != STATUS_COLLECTING:
            raise ValueError(
                f"Can only add participants while survey is collecting; "
                f"{survey.id} status={survey.status}."
            )
        if not participants:
            raise ValueError("--participants is required and cannot be empty.")

        added, already_present = self.repo.add_survey_participants(survey.id, participants)
        result = {
            "added": added,
            "already_present": already_present,
            "sent": [],
            "not_messageable": [],
            "failed": [],
        }
        for member in added:
            response = response_for_survey_member(self.repo, survey.id, member)
            self.persist_member_response(survey, response)
            if not can_receive_telegram_messages(member):
                result["not_messageable"].append(member)
                continue
            if self.repo.get_survey_message(survey.id, member.telegram_id):
                continue
            try:
                self.send_or_edit_member_invite(survey, member)
            except TelegramApiError as exc:
                result["failed"].append((member, exc))
            else:
                result["sent"].append(member)
        return result

    def resend_survey_form_to_participants(
        self,
        survey_id: str,
        participants: list[Member],
    ) -> dict:
        survey = self.repo.get_survey(survey_id)
        if survey is None:
            raise ValueError(f"Survey not found: {survey_id}")
        if survey.status not in EDITABLE_SURVEY_STATUSES:
            raise ValueError(
                f"Can only resend forms while survey is editable; "
                f"{survey.id} status={survey.status}."
            )
        if not participants:
            raise ValueError("--participants is required and cannot be empty.")

        existing = {
            member.telegram_id: member
            for member in self.repo.list_survey_participants(survey.id)
        }
        requested: list[Member] = []
        not_present: list[Member] = []
        seen_ids: set[int] = set()
        for member in participants:
            participant_id = stable_participant_id(member)
            if participant_id in seen_ids:
                raise ValueError(
                    f"Duplicate survey participant identity for {member.name!r} "
                    f"(id={participant_id})."
                )
            seen_ids.add(participant_id)
            existing_member = existing.get(participant_id)
            if existing_member is None:
                not_present.append(member)
                continue
            requested.append(existing_member)

        requested_ids = [member.telegram_id for member in requested]
        current_allowed = set(self.repo.get_revision_allowlist(survey))
        self.repo.unconfirm_survey_responses(survey.id, requested_ids)
        self.repo.set_revision_allowlist(survey, sorted(current_allowed | set(requested_ids)))

        result = {
            "resent": [],
            "not_present": not_present,
            "not_messageable": [],
            "failed": [],
        }
        for member in requested:
            response = response_for_survey_member(self.repo, survey.id, member)
            response.confirmed = False
            self.persist_member_response(survey, response)
            if not can_receive_telegram_messages(member):
                result["not_messageable"].append(member)
                continue
            try:
                self.send_or_edit_member_form(survey, member, response, force_new_message=True)
            except TelegramApiError as exc:
                result["failed"].append((member, exc))
            else:
                result["resent"].append(member)
        return result

    def set_survey_availability_for_member(
        self,
        survey_id: str,
        member_selector: str,
        *,
        unavailable_days: list[int] | None = None,
        unavailable_weekdays: list[str] | None = None,
        mode: str = "custom",
    ) -> tuple[Member, AvailabilityResponse]:
        _survey, member, response = set_survey_availability_response(
            self.repo,
            survey_id,
            member_selector,
            unavailable_days=unavailable_days,
            unavailable_weekdays=unavailable_weekdays,
            mode=mode,
        )
        return member, response

    def close_active_survey_messages(
        self,
        survey_id: str,
        telegram_id: int,
        *,
        except_chat_id: int | None = None,
        except_message_id: int | None = None,
        kind: str | None = None,
    ) -> None:
        messages = self.repo.list_active_survey_messages(
            survey_id,
            telegram_id,
            kind=kind,
        )
        for message in messages:
            chat_id = int(message["chat_id"])
            message_id = int(message["message_id"])
            if (
                except_chat_id is not None
                and except_message_id is not None
                and chat_id == int(except_chat_id)
                and message_id == int(except_message_id)
            ):
                continue
            try:
                self.telegram.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=None,
                )
            except TelegramApiError as exc:
                if not self.is_ignorable_telegram_error(exc):
                    logger.warning(
                        "Could not close old survey message survey=%s user=%s message=%s/%s: %s",
                        survey_id,
                        telegram_id,
                        chat_id,
                        message_id,
                        exc,
                    )
            self.repo.mark_survey_messages_inactive(
                survey_id,
                telegram_id,
                chat_id=chat_id,
                message_id=message_id,
            )

    def send_or_edit_member_invite(self, survey: Survey, member: Member) -> None:
        canonical = self.repo.get_survey_message(survey.id, member.telegram_id)
        text = self.messages.survey_invite
        reply_markup = start_survey_keyboard(
            survey.id,
            language=self.language,
            calendar_type=survey.calendar_type,
        )
        if canonical:
            try:
                self.telegram.edit_message_text(
                    chat_id=canonical["chat_id"],
                    message_id=canonical["message_id"],
                    text=text,
                    reply_markup=reply_markup,
                )
                self.repo.record_survey_message(
                    survey.id,
                    member.telegram_id,
                    int(canonical["chat_id"]),
                    int(canonical["message_id"]),
                    kind="invite",
                )
                return
            except TelegramApiError as exc:
                if self.is_ignorable_telegram_error(exc):
                    return
                logger.warning(
                    "Could not edit survey invite for survey %s member %s: %s",
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
                kind="invite",
            )

    def send_or_edit_member_form(
        self,
        survey: Survey,
        member: Member,
        response: AvailabilityResponse,
        *,
        force_new_message: bool = False,
    ) -> None:
        if force_new_message:
            self.close_active_survey_messages(survey.id, member.telegram_id)
        canonical = None if force_new_message else self.repo.get_survey_message(survey.id, member.telegram_id)
        text = self.survey_form_text(survey, response)
        reply_markup = self.response_keyboard(response, survey.id)
        if canonical and not force_new_message:
            try:
                self.telegram.edit_message_text(
                    chat_id=canonical["chat_id"],
                    message_id=canonical["message_id"],
                    text=text,
                    reply_markup=reply_markup,
                )
                self.repo.record_survey_message(
                    survey.id,
                    member.telegram_id,
                    int(canonical["chat_id"]),
                    int(canonical["message_id"]),
                    kind="form",
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
                kind="form",
            )

    def maybe_activate_due_live_schedule(self, today: date | None = None) -> None:
        """Switch live reminders to the current calendar month when its schedule exists.

        Approving a future month early writes schedule_entries but must not move
        active_schedule_source until that month becomes current.
        """
        today = today or datetime.now(ZoneInfo(self.timezone_name)).date()
        year, month = current_calendar_month(today, self.calendar_type)
        source = self.repo.get_active_schedule_source()
        if source == (year, month):
            return
        if self.repo.has_schedule_month(year, month):
            self.repo.set_active_schedule_source(year, month)
            logger.info(
                "Live reminder schedule activated for current month %s/%s.",
                year,
                month,
            )

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

    def stale_survey_callback(self, callback: dict, message: str | None = None) -> None:
        self.safe_answer_callback_query(
            callback["id"],
            message if message is not None else self.messages.survey_ended,
            show_alert=True,
        )
        self.remove_callback_buttons(callback)
        data = str(callback.get("data", ""))
        survey_id = ""
        if data.startswith("av:"):
            parts = data.split(":")
            if len(parts) >= 3:
                survey_id = parts[1]
        callback_message = callback.get("message") or {}
        chat = callback_message.get("chat") or {}
        user = callback.get("from") or {}
        if survey_id and chat.get("id") is not None and callback_message.get("message_id") is not None:
            self.repo.mark_survey_messages_inactive(
                survey_id,
                int(user.get("id") or 0) or None,
                chat_id=int(chat["id"]),
                message_id=int(callback_message["message_id"]),
            )

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
        allowed = {int(item) for item in state.get("allowed_member_ids", [])}
        response = self.repo.get_survey_response(survey.id, member.telegram_id)
        if response and response.confirmed and member.telegram_id not in allowed:
            return False
        if phase == STATUS_COLLECTING:
            return True
        if phase == STATUS_REVISION_REQUESTED:
            return member.telegram_id in allowed
        return False

    def response_keyboard(self, response: AvailabilityResponse, survey_id: str = "") -> dict:
        return main_availability_keyboard(
            response,
            survey_id=survey_id,
            language=self.language,
            calendar_type=self.calendar_type,
        )

    def authorize_from_start(self, chat_id: int, user: dict | None) -> Member | None:
        """Allowlist-only: bind telegram_id on first /start for a pre-registered username."""
        return resolve_allowlisted_member(
            self.repo,
            telegram_id=chat_id,
            username=str((user or {}).get("username", "")),
            display_name=telegram_display_name(user),
        )

    def deny_access(self, chat_id: int, user: dict | None = None) -> str:
        return access_denied_message(
            self.repo,
            self.messages,
            telegram_id=chat_id,
            username=str((user or {}).get("username", "")),
        )

    def start_member_survey(self, chat_id: int, user: dict | None = None) -> None:
        member = self.authorize_from_start(chat_id, user)
        if member is None:
            self.telegram.send_message(chat_id, self.deny_access(chat_id, user))
            return

        self.refresh_members()
        survey = self.editable_survey_for_member(member)
        if survey is not None:
            response = response_for_survey_member(self.repo, survey.id, member)
            self.persist_member_response(survey, response)
            self.send_or_edit_member_form(survey, member, response)
            return

        if self.confirmed_locked_survey_for_member(member) is not None:
            self.telegram.send_message(
                chat_id,
                self.messages.already_submitted_contact_admin,
            )
            return

        if self.participant_in_active_survey(member):
            self.telegram.send_message(chat_id, self.messages.survey_closed)
            return

        self.telegram.send_message(chat_id, self.messages.no_active_survey)

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

        member = resolve_allowlisted_member(
            self.repo,
            telegram_id=chat_id,
            username=str((user or {}).get("username", "")),
            display_name=telegram_display_name(user),
        )
        if member is None:
            self.telegram.send_message(chat_id, self.deny_access(chat_id, user))
            return

        self.refresh_members()
        if self.is_today_command(text):
            self.send_today_bug_day(chat_id)
            return

        survey = self.editable_survey_for_member(member)
        if survey is not None:
            response = response_for_survey_member(self.repo, survey.id, member)
            self.persist_member_response(survey, response)
            self.send_or_edit_member_form(survey, member, response)
            return

        if self.confirmed_locked_survey_for_member(member) is not None:
            self.telegram.send_message(
                chat_id,
                self.messages.already_submitted_contact_admin,
            )
            return

        if self.participant_in_active_survey(member):
            self.telegram.send_message(chat_id, self.messages.survey_closed)
            return

        self.telegram.send_message(chat_id, self.messages.no_active_survey)

    def is_today_command(self, text: str) -> bool:
        normalized = text.strip().lower()
        return normalized in {"/today", "today", "امروز", "روز باگ امروز"}

    def send_today_bug_day(self, chat_id: int, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        self.refresh_members()
        entry = self.repo.get_schedule_entry(now.date().isoformat())
        if entry is None:
            self.telegram.send_message(chat_id, self.messages.no_schedule_today)
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
                language=self.language,
            ),
        )

    def handle_availability_callback(self, callback: dict) -> None:
        callback_started = time.monotonic()
        last_step = callback_started
        debug_timing = logger.isEnabledFor(logging.DEBUG)

        def log_step(step: str, **fields) -> None:
            nonlocal last_step
            if not debug_timing:
                return
            now = time.monotonic()
            extras = " ".join(f"{key}={value}" for key, value in fields.items())
            logger.debug(
                "availability callback step=%s delta_ms=%.1f total_ms=%.1f%s%s",
                step,
                (now - last_step) * 1000,
                (now - callback_started) * 1000,
                " " if extras else "",
                extras,
            )
            last_step = now

        log_step(
            "start",
            callback_id=callback.get("id"),
            user_id=(callback.get("from") or {}).get("id"),
            data=callback.get("data", ""),
        )
        self.refresh_members()
        log_step("refresh_members")
        survey, action, args = self.survey_by_callback_parts(callback.get("data", ""))
        log_step(
            "parse_callback",
            survey_id=survey.id if survey else "missing",
            action=action,
        )
        if survey is None:
            self.stale_survey_callback(callback, self.messages.survey_ended)
            log_step("stale_missing_survey")
            return

        user = callback["from"]
        telegram_id = int(user["id"])
        member = resolve_allowlisted_member(
            self.repo,
            telegram_id=telegram_id,
            username=str(user.get("username", "")),
            display_name=telegram_display_name(user),
        )
        log_step(
            "resolve_member",
            member=member.name if member else "denied",
            telegram_id=telegram_id,
        )
        if member is None:
            self.safe_answer_callback_query(
                callback["id"],
                self.deny_access(telegram_id, user),
            )
            log_step("deny_member")
            return
        if not self.survey_is_current_and_editable(survey):
            active = self.repo.get_active_survey(survey.kind)
            if (
                survey.status in TERMINAL_STATUSES
                or active is None
                or active.id != survey.id
            ):
                self.stale_survey_callback(callback, self.messages.survey_ended)
            else:
                self.stale_survey_callback(callback, self.messages.survey_closed)
            log_step("stale_or_closed", survey_status=survey.status)
            return

        state = self.survey_state_for(survey)
        log_step("load_survey_state", phase=state.get("phase"))
        if not self.member_can_edit_survey(member, survey, state):
            response = self.repo.get_survey_response(survey.id, member.telegram_id)
            allowed = {int(item) for item in state.get("allowed_member_ids", [])}
            if (
                state.get("phase", STATUS_COLLECTING) == STATUS_COLLECTING
                and response
                and response.confirmed
                and member.telegram_id not in allowed
            ):
                self.stale_survey_callback(
                    callback,
                    self.messages.already_submitted_contact_admin,
                )
            else:
                self.stale_survey_callback(callback, self.messages.survey_closed)
            log_step("member_cannot_edit")
            return

        response = response_for_survey_member(self.repo, survey.id, member)
        log_step("load_response", confirmed=response.confirmed, mode=response.mode)

        if action == "start":
            self.persist_member_response(survey, response)
            self.safe_answer_callback_query(callback["id"])
            self.remove_callback_buttons(callback)
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is not None and message.get("message_id") is not None:
                self.repo.mark_survey_messages_inactive(
                    survey.id,
                    telegram_id,
                    chat_id=int(chat["id"]),
                    message_id=int(message["message_id"]),
                    kind="invite",
                )
            self.send_or_edit_member_form(survey, member, response)
            log_step("start_form_sent")
            return
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
                self.safe_answer_callback_query(
                    callback["id"],
                    service_text("use_change_first", self.language, survey.calendar_type),
                )
                return
            reply_markup = weekdays_keyboard(
                response,
                survey.calendar_type,
                survey.id,
                language=self.language,
            )
        elif action == "dates":
            if response.mode == "full":
                self.safe_answer_callback_query(
                    callback["id"],
                    service_text("use_change_first", self.language, survey.calendar_type),
                )
                return
            reply_markup = dates_keyboard(
                response,
                survey.year,
                survey.month,
                survey.calendar_type,
                survey.id,
                language=self.language,
            )
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
            reply_markup = weekdays_keyboard(
                response,
                survey.calendar_type,
                survey.id,
                language=self.language,
            )
        elif action == "d" and len(args) == 2:
            response.mode = "custom"
            verb, raw_day = args
            day = int(raw_day)
            if verb == "remove" and day in response.unavailable_days:
                response.unavailable_days.remove(day)
            elif verb == "add" and day not in response.unavailable_days:
                response.unavailable_days.append(day)
            response.confirmed = False
            reply_markup = dates_keyboard(
                response,
                survey.year,
                survey.month,
                survey.calendar_type,
                survey.id,
                language=self.language,
            )
        else:
            self.safe_answer_callback_query(callback["id"])
            log_step("unsupported_or_noop_action", action=action)
            return
        log_step("apply_action_build_markup", action=action)

        save_like_actions = {"confirm", "full"}
        success_notice = (
            availability_saved_message(survey.calendar_type, self.language)
            if action in save_like_actions
            else ""
        )
        is_final_confirm = action in save_like_actions
        try:
            self.persist_member_response(survey, response)
            log_step("persist_response", confirmed=response.confirmed, mode=response.mode)
            if is_final_confirm:
                self.repo.remove_revision_allowlist_members(survey, [telegram_id])
                log_step("clear_member_edit_override")
        except Exception as exc:
            logger.warning("Availability save failed for %s: %s", member.name, exc)
            self.safe_answer_callback_query(callback["id"], TEMPORARY_TELEGRAM_ERROR)
            log_step("persist_response_failed")
            return

        self.safe_answer_callback_query(callback["id"], "" if is_final_confirm else success_notice)
        log_step("telegram_answer_callback_early", final_confirm=is_final_confirm)
        try:
            self.telegram.edit_message_text(
                chat_id=callback["message"]["chat"]["id"],
                message_id=callback["message"]["message_id"],
                text=(
                    availability_confirmed_form_text(
                        survey,
                        response,
                        self.survey_label(survey),
                        language=self.language,
                    )
                    if is_final_confirm
                    else self.survey_form_text(survey, response)
                ),
                reply_markup=None if is_final_confirm else reply_markup,
            )
            log_step("telegram_edit_message_text", final_confirm=is_final_confirm)
            self.repo.record_survey_message(
                survey.id,
                telegram_id,
                int(callback["message"]["chat"]["id"]),
                int(callback["message"]["message_id"]),
                kind="form",
            )
            log_step("record_survey_message")
            if is_final_confirm:
                self.close_active_survey_messages(
                    survey.id,
                    telegram_id,
                    except_chat_id=int(callback["message"]["chat"]["id"]),
                    except_message_id=int(callback["message"]["message_id"]),
                )
                self.repo.mark_survey_messages_inactive(
                    survey.id,
                    telegram_id,
                    chat_id=int(callback["message"]["chat"]["id"]),
                    message_id=int(callback["message"]["message_id"]),
                )
                self.safe_send_message(
                    chat_id=telegram_id,
                    text=availability_saved_message(survey.calendar_type, self.language),
                )
                log_step("telegram_send_confirmation")
        except TelegramApiError as exc:
            if self.is_ignorable_telegram_error(exc):
                log_step("telegram_edit_ignorable", final_confirm=is_final_confirm)
                if is_final_confirm:
                    callback_message = callback.get("message") or {}
                    chat = callback_message.get("chat") or {}
                    self.close_active_survey_messages(
                        survey.id,
                        telegram_id,
                        except_chat_id=int(chat["id"]) if chat.get("id") is not None else None,
                        except_message_id=int(callback_message["message_id"])
                        if callback_message.get("message_id") is not None
                        else None,
                    )
                    self.safe_send_message(
                        chat_id=telegram_id,
                        text=availability_saved_message(survey.calendar_type, self.language),
                    )
                    log_step("telegram_send_confirmation_after_ignorable")
                return

            if not self.is_ignorable_telegram_error(exc):
                logger.warning("Availability callback failed for %s: %s", member.name, exc)
                self.safe_send_message(
                    chat_id=telegram_id,
                    text=(
                        availability_saved_but_refresh_failed_message(
                            survey.calendar_type,
                            self.language,
                        )
                        if action in save_like_actions
                        else TEMPORARY_TELEGRAM_ERROR
                    ),
                )
                log_step("telegram_callback_failed", final_confirm=is_final_confirm)
                return
            log_step("telegram_unexpected_ignorable_path")

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
            if not is_local_only_member(member)
        ]
        if phase != STATUS_REVISION_REQUESTED:
            return members
        allowed = {int(item) for item in state.get("allowed_member_ids", [])}
        return [member for member in members if member.telegram_id in allowed]

    def all_members_responded(self, year: int, month: int, survey: Survey | None = None) -> bool:
        survey = survey or self.active_or_latest_survey_for_month(year, month)
        if survey is None:
            return False
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
        # surveys.closes_at is the only deadline source of truth.
        collect_until_raw = survey.closes_at
        if not collect_until_raw:
            return False

        now = now or datetime.now(ZoneInfo(self.timezone_name))
        collect_until = parse_stored_datetime(collect_until_raw, now)
        if collect_until is None:
            return False
        return now >= collect_until

    def maybe_create_preview(self, today: date | datetime | None = None) -> None:
        if isinstance(today, datetime):
            now = today.astimezone(ZoneInfo(self.timezone_name))
            today = now.date()
        else:
            now = datetime.now(ZoneInfo(self.timezone_name))
            today = today or now.date()

        for kind in (SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG):
            survey = self.active_survey_record(kind)
            if not survey:
                continue
            if survey.status in {
                STATUS_SCHEDULED,
                STATUS_PENDING_ADMIN_REVIEW,
                STATUS_BLOCKED,
                STATUS_PUBLISHING,
                STATUS_APPROVED,
                STATUS_CANCELED,
            }:
                continue

            responded = self.all_members_responded(survey.year, survey.month, survey)
            collect_deadline = self.collection_deadline_reached(now, survey=survey)
            if collect_deadline:
                self.auto_confirm_missing_responses(survey)
                responded = True
            if survey.status == STATUS_REVISION_REQUESTED:
                # Revision waits for re-confirms or the revision collect window.
                # Month-start alone must not close a revision immediately.
                if not responded and not collect_deadline:
                    continue
            elif not responded and not collect_deadline:
                continue

            self.create_preview(survey.year, survey.month, survey_id=survey.id)

    def unconfirmed_messageable_participants(self, survey: Survey) -> list[Member]:
        responses = self.responses_for_survey(survey)
        members = []
        for member in self.messageable_survey_participants(survey):
            response = responses.get(member.telegram_id)
            if response is None or not response.confirmed:
                members.append(member)
        return members

    def auto_confirm_missing_responses(self, survey: Survey) -> None:
        for member in self.required_response_members(survey):
            response = self.repo.get_survey_response(survey.id, member.telegram_id)
            if response is not None and response.confirmed:
                continue
            auto_response = default_response(member)
            auto_response.unavailable_days = []
            auto_response.unavailable_weekdays = []
            auto_response.confirmed = True
            auto_response.mode = "full"
            self.persist_member_response(survey, auto_response)

    def maybe_send_survey_collection_reminders(self, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        today = now.date()
        for kind in (SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG):
            survey = self.active_survey_record(kind)
            if survey is None or survey.status != STATUS_COLLECTING:
                continue
            if self.collection_deadline_reached(now, survey=survey):
                continue
            unconfirmed = self.unconfirmed_messageable_participants(survey)
            if not unconfirmed:
                continue

            starts_at = parse_stored_datetime(survey.starts_at, now)
            closes_at = parse_stored_datetime(survey.closes_at, now)
            start_date = starts_at.astimezone(now.tzinfo).date() if starts_at else today
            close_date = closes_at.astimezone(now.tzinfo).date() if closes_at else None

            if close_date is not None and today == close_date:
                self.send_last_day_survey_group_reminder(survey, unconfirmed, today)
                continue

            if today <= start_date:
                continue
            self.send_daily_survey_dm_reminders(survey, unconfirmed, today)

    def send_daily_survey_dm_reminders(
        self,
        survey: Survey,
        members: list[Member],
        reminder_date: date,
    ) -> None:
        key = f"survey_reminder_dm:{survey.id}:{reminder_date.isoformat()}"
        if self.repo.get_state(key):
            return
        sent = []
        for member in members:
            try:
                self.telegram.send_message(
                    chat_id=member.telegram_id,
                    text=self.messages.survey_reminder,
                )
                sent.append(member.telegram_id)
            except TelegramApiError as exc:
                logger.warning("Survey reminder failed for %s: %s", member.name, exc)
        if sent:
            self.repo.set_state(key, {"sent_at": utc_now(), "telegram_ids": sent})

    def send_last_day_survey_group_reminder(
        self,
        survey: Survey,
        members: list[Member],
        reminder_date: date,
    ) -> None:
        key = f"survey_reminder_group:{survey.id}:{reminder_date.isoformat()}"
        if self.repo.get_state(key):
            return
        try:
            group_chat_id, topic_id = self.telegram_destination()
        except RuntimeError as exc:
            logger.warning("Survey group reminder skipped: %s", exc)
            return
        mentions = " ".join(mention(member) for member in members)
        self.telegram.send_message(
            chat_id=group_chat_id,
            message_thread_id=topic_id,
            text=self.messages.survey_last_day_group_reminder.format(mentions=mentions),
        )
        self.repo.set_state(
            key,
            {
                "sent_at": utc_now(),
                "telegram_ids": [member.telegram_id for member in members],
            },
        )

    def create_preview(self, year: int, month: int, survey_id: str | None = None) -> None:
        self.refresh_members()
        survey = self.repo.get_survey(survey_id) if survey_id else self.active_or_latest_survey_for_month(year, month)
        if survey is None:
            raise RuntimeError(
                f"No survey found for preview ({self.calendar_type} {year}-{month:02d}). "
                "Create and collect a survey first."
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
        schedule, stats, review = relabel_schedule_outputs(
            self.repo,
            schedule=schedule,
            stats=stats,
            review=review,
            members=participants,
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

        caption = preview_photo_caption(
            survey.year,
            survey.month,
            survey.calendar_type,
            review,
            language=self.language,
        )
        report_message = preview_report_message(
            survey.year,
            survey.month,
            survey.calendar_type,
            stats,
            review,
            language=self.language,
        )
        approval_markup = admin_approval_keyboard(
            survey.year,
            survey.month,
            survey.calendar_type,
            status,
            has_flagged_members=bool(review["flagged_member_ids"]),
            survey_id=survey.id,
            language=self.language,
        )

        # Persist review state before Telegram fan-out so one bad admin cannot wedge retries.
        if not self.repo.transition_survey(
            survey.id,
            from_statuses=PREVIEW_SOURCE_STATUSES,
            to_status=status,
            preview_sent_at=utc_now(),
            image_path=str(image_path),
            schedule_json=json.dumps(schedule),
            stats_json=json.dumps(stats),
            review_json=json.dumps(review),
        ):
            current = self.repo.get_survey(survey.id)
            raise RuntimeError(
                f"Cannot create preview from status "
                f"{current.status if current else 'missing'} for survey {survey.id}."
            )
        survey = self.repo.get_survey(survey.id) or survey
        # Preview ends any revision window; allowlist is no longer needed.
        self.repo.clear_revision_allowlist(survey.kind)

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
            raise RuntimeError(
                f"Configured output_dir is not writable: {configured}. "
                "Fix runtime_settings.output_dir to a writable path."
            ) from exc

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
            self.safe_answer_callback_query(
                callback["id"],
                service_text("admins_only", self.language, self.calendar_type),
            )
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
            self.safe_send_message(
                user_id,
                service_text("no_survey_action", self.language, self.calendar_type),
            )
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
            self.safe_send_message(
                user_id,
                service_text("already_approved", self.language, calendar_type),
            )
            return
        if survey.status == STATUS_CANCELED and action not in {"cancel", "restart"}:
            self.safe_send_message(
                user_id,
                service_text("was_canceled", self.language, calendar_type),
            )
            return

        if action == "restart":
            now = datetime.now(ZoneInfo(self.timezone_name))
            if survey.status == STATUS_APPROVED:
                self.safe_send_message(
                    user_id,
                    service_text("already_approved", self.language, calendar_type),
                )
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
                self.safe_send_message(
                    user_id,
                    service_text("already_approved", self.language, calendar_type),
                )
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
                reply_markup=admin_canceled_keyboard(
                    year,
                    month,
                    calendar_type,
                    survey.id,
                    language=self.language,
                ),
            )
            return

        if action in {"correct", "reopen"}:
            if not survey.review_json:
                self.safe_send_message(
                    user_id,
                    service_text("no_review", self.language, calendar_type),
                )
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
                reply_markup=admin_revision_keyboard(
                    year,
                    month,
                    calendar_type,
                    survey.id,
                    language=self.language,
                ),
            )
            return

        survey = self.repo.get_survey(survey.id) or survey
        if not survey.schedule_json:
            self.safe_send_message(
                user_id,
                service_text("no_preview", self.language, calendar_type),
            )
            return
        if survey.status == STATUS_CANCELED:
            self.safe_send_message(
                user_id,
                service_text("was_canceled", self.language, calendar_type),
            )
            return
        if survey.status == STATUS_APPROVED:
            self.safe_send_message(
                user_id,
                service_text("already_approved", self.language, calendar_type),
            )
            return
        if survey.status == STATUS_REVISION_REQUESTED:
            self.safe_send_message(
                user_id,
                service_text("cannot_approve_revision", self.language, calendar_type),
            )
            return
        try:
            pin_error = self.publish_survey(survey)
            self.remove_callback_buttons(callback)
            if pin_error is None:
                admin_message = approval_sent_message(
                    year,
                    month,
                    calendar_type,
                    self.language,
                )
            else:
                admin_message = approval_posted_pin_failed_message(
                    year,
                    month,
                    calendar_type,
                    str(pin_error),
                    language=self.language,
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
        today = datetime.now(ZoneInfo(self.timezone_name)).date()
        # Only cut over live reminders when the published month is already current.
        activate_live = (
            survey.kind == SURVEY_KIND_PRODUCTION
            and current_calendar_month(today, survey.calendar_type) == (survey.year, survey.month)
        )
        if not self.repo.complete_publish(survey.id, activate_live=activate_live):
            current = self.repo.get_survey(survey.id)
            if current is None or current.status != STATUS_APPROVED:
                raise RuntimeError(
                    f"Failed to finalize publish for survey {survey.id} "
                    f"(status={current.status if current else 'missing'})."
                )

    def notify_members_schedule_published(self, survey: Survey) -> None:
        if not survey.schedule_json:
            return
        schedule = json.loads(survey.schedule_json)
        for member in self.repo.list_survey_participants(survey.id):
            if not can_receive_telegram_messages(member):
                continue
            try:
                self.telegram.send_message(
                    chat_id=member.telegram_id,
                    text=assignment_summary_message(
                        member,
                        survey,
                        schedule,
                        language=self.language,
                    ),
                )
            except TelegramApiError as exc:
                logger.warning(
                    "Published schedule notification failed for %s: %s",
                    member.name,
                    exc,
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

        # Validate destination and image before claiming publishing — avoid wedging
        # on failures that are known before any Telegram side effect.
        group_chat_id, topic_id = self.telegram_destination()
        image_path = Path(survey.image_path or "")
        if not image_path.is_file():
            raise RuntimeError(
                f"Cannot publish survey {survey.id}: preview image missing at {image_path}."
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

        caption = group_schedule_caption(
            survey.year,
            survey.month,
            survey.calendar_type,
            self.language,
        )
        try:
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
        survey = self.repo.get_survey(survey.id) or survey
        self.notify_members_schedule_published(survey)
        return pin_error

    def canceled_message(self, year: int, month: int, calendar_type: str) -> str:
        return service_text(
            "canceled",
            self.language,
            calendar_type,
            month=month_label(year, month, calendar_type),
        )

    def survey_restarted_message(self, year: int, month: int, calendar_type: str) -> str:
        return service_text(
            "restarted",
            self.language,
            calendar_type,
            month=month_label(year, month, calendar_type),
        )

    def no_reachable_flagged_members_message(self, flagged_names: list[str]) -> str:
        names = format_names(flagged_names, self.calendar_type, self.language)
        return service_text(
            "correction_members",
            self.language,
            self.calendar_type,
            names=names,
        )

    def revision_started_message(
        self,
        member_ids: list[int],
        survey: Survey | None = None,
    ) -> str:
        names = format_names(
            self.member_names_for_ids(member_ids, survey),
            self.calendar_type,
            self.language,
        )
        return service_text(
            "revision_started",
            self.language,
            self.calendar_type,
            names=names,
        )

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
        return service_text(
            "revision_request",
            self.language,
            self.calendar_type,
            month=month_label(year, month, self.calendar_type),
        )

    def send_daily_reminder(self, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self.timezone_name))
        self.refresh_members()
        target_time = day_time.fromisoformat(self.runtime.daily_reminder_time)
        if now.time().replace(second=0, microsecond=0) < target_time:
            return

        work_date = now.date().isoformat()
        # Prefer a possible rare duplicate over a silent lost reminder:
        # mark sent only after Telegram accepts the send. A crash between a
        # successful send and this mark can duplicate once on retry; claiming
        # before send would drop the day entirely after a crash.
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
                    self.send_or_edit_member_invite(survey, member)
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

    def update_partition_key(self, update: dict) -> str:
        if "callback_query" in update:
            callback = update["callback_query"]
            user = callback.get("from") or {}
            telegram_id = user.get("id")
            data = str(callback.get("data", ""))
            parts = data.split(":")
            if data.startswith("av:") and len(parts) >= 3:
                return f"availability:{parts[1]}:{telegram_id}"
            if data.startswith("admin:"):
                if len(parts) == 3:
                    return f"admin:{parts[2]}"
                if len(parts) >= 5:
                    return f"admin:{parts[2]}:{parts[3]}:{parts[4]}"
                return f"admin:{telegram_id}"
            return f"callback:{telegram_id or callback.get('id') or 'unknown'}"

        message = update.get("message") or {}
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        identity = user.get("id") or chat.get("id") or update.get("update_id") or "unknown"
        return f"message:{identity}"

    def run_once(self) -> None:
        self.refresh_runtime()
        now = datetime.now(ZoneInfo(self.timezone_name))
        today = now.date()
        for task in (
            lambda now=now: self.ensure_survey_started(now),
            self.resume_collecting_form_delivery,
            lambda now=now: self.maybe_send_survey_collection_reminders(now),
            lambda today=today: self.maybe_activate_due_live_schedule(today),
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
        if offset != self.update_dispatcher.offset:
            self.update_dispatcher.offset = offset
        offset = self.update_dispatcher.advance_offset()
        poll_started = time.monotonic()
        try:
            updates = self.telegram.get_updates(
                offset=offset,
                timeout=self.runtime.poll_interval_seconds,
            )
        except TelegramApiError:
            logger.exception("Fetching Telegram updates failed")
            return offset
        first_update = updates[0] if updates else {}
        last_update = updates[-1] if updates else {}
        first_date, first_source = update_telegram_date(first_update)
        last_date, last_source = update_telegram_date(last_update)
        first_age = update_age_seconds(first_update) if updates else None
        last_age = update_age_seconds(last_update) if updates else None
        logger.debug(
            "poll cycle offset=%s updates=%s elapsed_ms=%.1f "
            "first_update_id=%s last_update_id=%s "
            "first_age_seconds=%s first_age_source=%s first_telegram_date_utc=%s "
            "last_age_seconds=%s last_age_source=%s last_telegram_date_utc=%s",
            offset,
            len(updates),
            (time.monotonic() - poll_started) * 1000,
            first_update.get("update_id"),
            last_update.get("update_id"),
            f"{first_age:.3f}" if first_age is not None else "unknown",
            first_source,
            epoch_utc_label(first_date),
            f"{last_age:.3f}" if last_age is not None else "unknown",
            last_source,
            epoch_utc_label(last_date),
        )

        for update in updates:
            self.update_dispatcher.enqueue_update(update)

        return self.update_dispatcher.advance_offset()

    def run_forever(self) -> None:
        logger.warning(
            "Starting long-polling bot. Do not run another process with the same "
            "Telegram bot token (including debug or extra bot processes); getUpdates conflicts."
        )
        self.start_background_workers()
        offset = self.repo.get_update_offset()
        while True:
            offset = self.run_loop_once(offset)
            time.sleep(1)

    def start_background_workers(self) -> None:
        self.update_dispatcher.start()
        if self.scheduled_thread is not None and self.scheduled_thread.is_alive():
            return
        self.scheduled_stop_event.clear()
        self.scheduled_thread = threading.Thread(
            target=self.scheduled_work_loop,
            name="telegram-scheduled-runner",
            daemon=True,
        )
        self.scheduled_thread.start()

    def scheduled_work_loop(self) -> None:
        while not self.scheduled_stop_event.is_set():
            if self.should_run_scheduled_work():
                self.run_once()
            self.scheduled_stop_event.wait(1)

    def stop_background_workers(self, timeout: float | None = None) -> None:
        self.scheduled_stop_event.set()
        if self.scheduled_thread is not None:
            self.scheduled_thread.join(timeout=timeout)
        self.update_dispatcher.stop(timeout=timeout)

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
    if survey.status == STATUS_CANCELED:
        raise SystemExit(f"Cannot activate canceled survey {survey.id} as the live schedule.")
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
    kind = resolve_cli_survey_kind(args)
    created_by = "local-cli-debug" if kind == SURVEY_KIND_DEBUG else "local-cli"
    skip_collect = bool(getattr(args, "direct_preview", False) or getattr(args, "publish_now", False))
    status = STATUS_COLLECTING
    if starts_at > now and not args.send_now and not skip_collect:
        status = STATUS_SCHEDULED
    replace_active = bool(getattr(args, "replace_active_survey", False))
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
        replace_active=replace_active,
    )


def handle_user_admin_command(repo: BotRepository, args: argparse.Namespace) -> bool:
    requested_user_actions = [
        name
        for name, value in (
            ("--upsert-user", args.upsert_user),
            ("--deactivate-user", args.deactivate_user),
            ("--delete-user", args.delete_user),
        )
        if value
    ]
    if len(requested_user_actions) > 1:
        raise SystemExit(
            "Use only one user command at a time: " + ", ".join(requested_user_actions)
        )

    if args.deactivate_user:
        username = normalize_username(args.deactivate_user)
        if not repo.deactivate_user(username):
            raise SystemExit(f"User not found: @{username}")
        print(f"User deactivated: @{username}")
        return True

    if args.delete_user:
        username = normalize_username(args.delete_user)
        if not repo.delete_user(username):
            raise SystemExit(f"User not found: @{username}")
        print(f"User deleted: @{username}")
        return True

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


def format_unavailability_detail(response: dict | None) -> str:
    if response is None:
        return "-"
    if response.get("mode") == "full":
        return "fully available"
    parts = []
    days = response.get("unavailable_days") or []
    weekdays = response.get("unavailable_weekdays") or []
    if days:
        parts.append("days=" + ",".join(str(day) for day in sorted(days)))
    if weekdays:
        parts.append("weekdays=" + ",".join(str(weekday) for weekday in sorted(weekdays)))
    return "; ".join(parts) if parts else "none selected"


def participant_matches_filter(member: Member, raw_filter: str) -> bool:
    needle = raw_filter.strip().lower().lstrip("@")
    if not needle:
        return True
    values = {
        normalize_username(member.username),
        member.name.strip().lower(),
        str(member.telegram_id),
    }
    return needle in values


def format_survey_report(
    repo: BotRepository,
    survey: Survey,
    *,
    member_filter: str = "",
) -> str:
    participants = repo.list_survey_participants(survey.id)
    if member_filter:
        participants = [
            member
            for member in participants
            if participant_matches_filter(member, member_filter)
        ]
    messages = repo.list_survey_messages(survey.id)
    responses = repo.list_survey_response_details(survey.id)
    messageable = [member for member in participants if can_receive_telegram_messages(member)]
    sent_ids = {
        member.telegram_id
        for member in messageable
        if member.telegram_id in messages
    }
    confirmed_ids = {
        member.telegram_id
        for member in messageable
        if responses.get(member.telegram_id, {}).get("confirmed")
    }
    not_confirmed_ids = {member.telegram_id for member in messageable} - confirmed_ids
    partial_ids = {
        member.telegram_id
        for member in messageable
        if member.telegram_id in responses
        and member.telegram_id not in confirmed_ids
        and (
            responses[member.telegram_id].get("unavailable_days")
            or responses[member.telegram_id].get("unavailable_weekdays")
            or responses[member.telegram_id].get("mode") not in (None, "", "custom")
        )
    }
    not_sent_ids = {member.telegram_id for member in messageable} - sent_ids

    lines = [
        (
            f"Survey: {survey.id} | {survey.kind} | {survey.calendar_type} "
            f"{survey.year}-{survey.month:02d} | status={survey.status}"
        ),
        f"starts_at={survey.starts_at or '-'} | closes_at={survey.closes_at or '-'}",
        (
            f"participants={len(participants)} | messageable={len(messageable)} | "
            f"sent={len(sent_ids)} | not_sent={len(not_sent_ids)} | "
            f"confirmed={len(confirmed_ids)} | not_confirmed={len(not_confirmed_ids)} | "
            f"partial_not_confirmed={len(partial_ids)}"
        ),
    ]

    if member_filter and not participants:
        lines.append(f"No participant matched: {member_filter}")
        return "\n".join(lines)

    lines.append("Details:")
    for member in participants:
        response = responses.get(member.telegram_id)
        message = messages.get(member.telegram_id)
        if not can_receive_telegram_messages(member):
            status = "local-only/not-messageable"
        elif response and response.get("confirmed"):
            status = "confirmed"
        elif response and member.telegram_id in partial_ids:
            status = "partial-not-confirmed"
        else:
            status = "not-confirmed"
        sent = "yes" if message else "no"
        updated_at = response.get("updated_at") if response else "-"
        mode = response.get("mode") if response else "-"
        username = f"@{normalize_username(member.username)}" if member.username else "-"
        lines.append(
            f"- {member.name} ({username}, id={member.telegram_id}): "
            f"{status} | sent={sent} | mode={mode} | "
            f"availability={format_unavailability_detail(response)} | updated_at={updated_at}"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Adhoc Assistant Telegram bot.")
    parser.add_argument(
        "--database",
        help="SQLite database path (infra only). Overrides DATABASE_URL.",
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
        "--deactivate-user",
        help="Mark an allowed Telegram username inactive in bot_users, then exit.",
    )
    parser.add_argument(
        "--delete-user",
        help=(
            "Delete an allowed Telegram username from bot_users, then exit. "
            "Survey snapshots and schedule history are not modified."
        ),
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
        "--replace-active-survey",
        action="store_true",
        help=(
            "With --create-survey / --send-now / --direct-preview / --publish-now: "
            "cancel any non-terminal survey of the same kind, then create the new one. "
            "Without this flag, an active survey is a hard conflict."
        ),
    )
    parser.add_argument(
        "--survey-kind",
        choices=[SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG],
        default=None,
        help=(
            "Survey kind. Defaults to debug for --direct-preview/--publish-now, "
            "and to production for --create-survey and other create paths."
        ),
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
            "Create a survey (defaults to --survey-kind debug unless explicitly set), "
            "build preview immediately with default availability (no response wait), then exit."
        ),
    )
    parser.add_argument(
        "--publish-now",
        action="store_true",
        help=(
            "Create/build a survey preview and publish it (defaults to --survey-kind debug "
            "unless explicitly set). Debug publish to the production destination still "
            "requires --allow-production-destination."
        ),
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
        "--add-survey-participants",
        help=(
            "Add --participants to an existing collecting survey and send forms "
            "only to newly added messageable participants."
        ),
    )
    parser.add_argument(
        "--remove-survey-participants",
        help=(
            "Remove --participants from an existing editable survey and delete "
            "their survey-specific response/message state."
        ),
    )
    parser.add_argument(
        "--resend-survey-form",
        help=(
            "Allow --participants to edit an existing editable survey again, "
            "then send each of them a fresh form."
        ),
    )
    parser.add_argument(
        "--set-survey-availability",
        help="Set one participant's availability response for an existing editable survey.",
    )
    parser.add_argument(
        "--availability-member",
        help="Username, display name, or Telegram id for --set-survey-availability.",
    )
    parser.add_argument(
        "--unavailable-days",
        help=(
            "Comma-separated day numbers for --set-survey-availability. "
            "Use an empty string to clear specific unavailable dates."
        ),
    )
    parser.add_argument(
        "--unavailable-weekdays",
        help=(
            "Comma-separated weekday names for --set-survey-availability, "
            "e.g. sunday,monday. Persian weekday names are accepted too. "
            "Use an empty string to clear recurring weekdays."
        ),
    )
    parser.add_argument(
        "--availability-mode",
        choices=["custom", "full"],
        default="custom",
        help="Availability mode for --set-survey-availability. Default: custom.",
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
        choices=CLI_SETTABLE_SURVEY_STATUSES,
        help=(
            "Guarded status recovery for --survey-id. Allowed targets: "
            + ", ".join(CLI_SETTABLE_SURVEY_STATUSES)
            + ". Only real state-machine transitions are accepted "
            "(scheduled→collecting, or cancel from a cancelable status). "
            "Preview/revision/approve/publish require their dedicated flows."
        ),
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
        "--finalize-publishing-survey-id",
        help=(
            "Manually finalize a survey stuck in publishing after you have verified "
            "the Telegram group post was actually delivered."
        ),
    )
    parser.add_argument(
        "--list-surveys",
        action="store_true",
        help="List surveys, then exit.",
    )
    parser.add_argument(
        "--survey-report",
        action="store_true",
        help="Print a read-only response/sending progress report for a survey, then exit.",
    )
    parser.add_argument(
        "--survey-member",
        help=(
            "Filter --survey-report to one participant by username, display name, "
            "or Telegram id."
        ),
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
        "--set-telegram-worker-count",
        dest="set_telegram_worker_count",
        type=int,
        help="Store Telegram update worker count in DB runtime settings.",
    )
    parser.add_argument(
        "--set-telegram-queue-maxsize",
        dest="set_telegram_queue_maxsize",
        type=int,
        help="Store per-worker Telegram update queue maxsize in DB runtime settings.",
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
        "--set-language",
        "--set-runtime-language",
        dest="set_runtime_language",
        help="Store bot UI language in DB runtime settings. Supported: en, fa, ar, ru.",
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
        "--set-bot-messages",
        help=(
            "Merge JSON object into runtime_settings.bot_messages. "
            f"Keys: {', '.join(BOT_MESSAGE_KEYS)}."
        ),
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
    parser.add_argument(
        "--cleanup-telegram-updates",
        action="store_true",
        help="Delete old terminal telegram_update_tracking rows, then exit.",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=90,
        help="Retention window for --cleanup-telegram-updates. Default: 90.",
    )
    return parser.parse_args(argv)


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
        "language",
        "survey_days_before_month",
        "survey_start_at",
        "survey_collect_for",
        "revision_collect_for",
        "daily_reminder_time",
        "poll_interval_seconds",
        "telegram_worker_count",
        "telegram_queue_maxsize",
        "holidays",
        "allow_production_destination",
        "bot_messages",
    ):
        if key not in raw:
            if key == "calendar":
                print(f"{key}: gregorian")
                continue
            if key == "language":
                print(f"{key}: en")
                continue
            if key == "allow_production_destination":
                print(f"{key}: false")
                continue
            if key == "bot_messages":
                print(f"{key}: <defaults>")
                continue
            print(f"{key}: <missing>")
            continue
        value = raw[key]
        if key == "telegram_token":
            print(f"{key}: {'***' if value else '<empty>'}")
        elif key in {"holidays", "bot_messages"}:
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

    if getattr(args, "cleanup_telegram_updates", False):
        days = max(1, int(getattr(args, "older_than_days", 90)))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = repo.cleanup_telegram_update_tracking(cutoff)
        print(
            "Telegram update tracking cleaned: "
            f"deleted={deleted} older_than_days={days}"
        )
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

    if args.survey_report:
        if args.survey_id:
            survey = repo.get_survey(args.survey_id)
        else:
            kind = args.survey_kind or SURVEY_KIND_PRODUCTION
            survey = repo.get_active_survey(kind)
        if survey is None:
            selector = args.survey_id or f"active {args.survey_kind or SURVEY_KIND_PRODUCTION}"
            raise SystemExit(f"Survey not found for report: {selector}")
        print(format_survey_report(repo, survey, member_filter=args.survey_member or ""))
        return True

    if args.set_survey_availability:
        if not args.availability_member:
            raise SystemExit("--availability-member is required with --set-survey-availability.")
        if (
            args.availability_mode == "custom"
            and args.unavailable_days is None
            and args.unavailable_weekdays is None
        ):
            raise SystemExit(
                "Use --unavailable-days and/or --unavailable-weekdays with "
                "--set-survey-availability, or pass --availability-mode full."
            )
        try:
            days = (
                parse_int_csv(args.unavailable_days)
                if args.unavailable_days is not None
                else None
            )
            weekdays = (
                parse_weekday_csv(args.unavailable_weekdays)
                if args.unavailable_weekdays is not None
                else None
            )
            survey, member, response = set_survey_availability_response(
                repo,
                args.set_survey_availability,
                args.availability_member,
                unavailable_days=days,
                unavailable_weekdays=weekdays,
                mode=args.availability_mode,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Survey availability updated: {survey.id}")
        print(f"  member={member.name} (@{normalize_username(member.username)})")
        print(f"  mode={response.mode}")
        print(f"  confirmed={response.confirmed}")
        print(
            "  unavailable_days="
            + (",".join(str(day) for day in response.unavailable_days) or "-")
        )
        print(
            "  unavailable_weekdays="
            + (",".join(response.unavailable_weekdays) or "-")
        )
        return True

    if args.remove_survey_participants:
        if not args.participants:
            raise SystemExit("--participants is required with --remove-survey-participants.")
        survey = repo.get_survey(args.remove_survey_participants)
        if survey is None:
            raise SystemExit(f"Survey not found: {args.remove_survey_participants}")
        if survey.status not in EDITABLE_SURVEY_STATUSES:
            raise SystemExit(
                "Can only remove participants while survey is editable; "
                f"survey {survey.id} status={survey.status}."
            )
        participants = parse_survey_participants(args.participants, repo)
        try:
            removed, not_present = repo.remove_survey_participants(survey.id, participants)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Survey participants removed: {survey.id}")
        print(f"  removed={len(removed)}")
        print(f"  not_present={len(not_present)}")
        for member in removed:
            print(f"  removed: {member.name} (@{normalize_username(member.username)})")
        for member in not_present:
            print(f"  not_present: {member.name} (@{normalize_username(member.username)})")
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

    if args.finalize_publishing_survey_id:
        survey = repo.get_survey(args.finalize_publishing_survey_id)
        if survey is None:
            raise SystemExit(f"Survey not found: {args.finalize_publishing_survey_id}")
        if survey.status != STATUS_PUBLISHING:
            raise SystemExit(
                f"Survey {survey.id} is not in publishing state (status={survey.status})."
            )
        if not survey.group_sent_at:
            repo.update_survey(survey.id, group_sent_at=utc_now())
            survey = repo.get_survey(survey.id) or survey
        runtime = repo.get_runtime_settings()
        today = datetime.now(ZoneInfo(runtime.timezone)).date()
        activate_live = (
            survey.kind == SURVEY_KIND_PRODUCTION
            and current_calendar_month(today, survey.calendar_type) == (survey.year, survey.month)
        )
        if not repo.complete_publish(survey.id, activate_live=activate_live):
            current = repo.get_survey(survey.id)
            raise SystemExit(
                f"Failed to finalize publishing survey {survey.id}; "
                f"status={current.status if current else 'missing'}."
            )
        print(f"Publishing survey finalized: {survey.id}")
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
        survey = repo.get_survey(args.survey_id)
        if survey is None:
            raise SystemExit(f"Survey not found: {args.survey_id}")
        runtime = repo.get_runtime_settings()
        if args.set_survey_status:
            new_status = args.set_survey_status
            allowed_targets = CLI_SURVEY_STATUS_TRANSITIONS.get(survey.status, set())
            if new_status not in allowed_targets:
                raise SystemExit(
                    f"Cannot set survey {survey.id} from {survey.status!r} to {new_status!r}. "
                    f"Allowed from {survey.status!r}: "
                    f"{', '.join(sorted(allowed_targets)) or 'none'}. "
                    "Use --cancel-survey-id / --force-preview / admin approve for real flows."
                )
            if new_status == survey.status:
                print(f"Survey already {new_status}: {survey.id}")
            elif not repo.transition_survey(
                survey.id,
                from_statuses={survey.status},
                to_status=new_status,
            ):
                current = repo.get_survey(survey.id)
                raise SystemExit(
                    f"Could not update survey {survey.id}; "
                    f"status={current.status if current else 'missing'}"
                )
            else:
                if new_status != STATUS_REVISION_REQUESTED:
                    repo.clear_revision_allowlist(survey.kind)
                print(f"Survey status set: {survey.id} {survey.status} -> {new_status}")
        if args.set_survey_starts_at:
            repo.update_survey(
                args.survey_id,
                starts_at=local_datetime_value(args.set_survey_starts_at, runtime.timezone),
            )
        if args.set_survey_closes_at:
            closes_at = local_datetime_value(args.set_survey_closes_at, runtime.timezone)
            repo.update_survey(args.survey_id, closes_at=closes_at)
        if args.set_survey_starts_at or args.set_survey_closes_at:
            print(f"Survey updated: {args.survey_id}")
        return True

    if args.set_schedule_entry_date:
        if not args.entry_main and not args.entry_backup:
            raise SystemExit(
                "--entry-main and/or --entry-backup are required with "
                "--set-schedule-entry-date."
            )
        updated = repo.set_schedule_entry_people(
            args.set_schedule_entry_date,
            args.entry_main,
            args.entry_backup,
        )
        if not updated:
            raise SystemExit(f"No schedule entry found for {args.set_schedule_entry_date}.")
        print(
            f"Schedule entry updated: {args.set_schedule_entry_date} "
            f"main={args.entry_main if args.entry_main else 'unchanged'} "
            f"backup={args.entry_backup if args.entry_backup else 'unchanged'}"
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
    if getattr(args, "set_telegram_worker_count", None) is not None:
        runtime_updates["telegram_worker_count"] = args.set_telegram_worker_count
    if getattr(args, "set_telegram_queue_maxsize", None) is not None:
        runtime_updates["telegram_queue_maxsize"] = args.set_telegram_queue_maxsize
    if args.set_runtime_timezone is not None:
        runtime_updates["timezone"] = args.set_runtime_timezone
    if args.set_runtime_calendar is not None:
        runtime_updates["calendar"] = args.set_runtime_calendar
    if getattr(args, "set_runtime_language", None) is not None:
        current_raw = repo.get_runtime_settings_raw() or {}
        calendar_for_language = runtime_updates.get(
            "calendar",
            str(current_raw.get("calendar") or "gregorian"),
        )
        runtime_updates["language"] = normalize_language(
            args.set_runtime_language,
            str(calendar_for_language),
        )
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
    if getattr(args, "set_bot_messages", None) is not None:
        try:
            payload = json.loads(args.set_bot_messages)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid --set-bot-messages JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit("--set-bot-messages must be a JSON object.")
        current_messages = repo.get_runtime_settings_raw().get("bot_messages") or {}
        if not isinstance(current_messages, dict):
            current_messages = {}
        unknown = sorted(set(payload) - set(BOT_MESSAGE_KEYS))
        if unknown:
            raise SystemExit(
                "Unknown bot_messages keys: "
                + ", ".join(unknown)
                + f". Allowed: {', '.join(BOT_MESSAGE_KEYS)}"
            )
        for key in BOT_MESSAGE_KEYS:
            if key in payload:
                current_messages[key] = str(payload[key])
        runtime_updates["bot_messages"] = current_messages
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


def build_bot(
    settings: InfraSettings,
    repo: BotRepository,
    *,
    require_export_support: bool = True,
) -> AdhocTelegramBot:
    runtime = repo.get_runtime_settings()
    if not runtime.telegram_token:
        raise SystemExit("telegram_token is empty in runtime_settings. Use --set-telegram-token.")
    if require_export_support:
        try:
            ensure_jpg_export_support()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    members = repo.list_members(active_only=True)
    telegram = TelegramClient(runtime.telegram_token)
    return AdhocTelegramBot(settings, members, repo, telegram)


def main() -> None:
    configure_logging_from_env()
    args = parse_args()
    settings = load_infra_settings(cli_database=args.database)
    repo = BotRepository(settings.database_path)
    if handle_user_admin_command(repo, args):
        return
    if handle_local_db_command(repo, settings, args):
        return

    if args.direct_preview or args.publish_now:
        # resolve_cli_survey_kind defaults these flows to debug unless --survey-kind
        # was set explicitly (including production).
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

    bot = build_bot(
        settings,
        repo,
        require_export_support=not bool(args.add_survey_participants or args.resend_survey_form),
    )
    if args.create_survey and args.send_now:
        survey = create_local_survey(repo, args)
        bot.send_survey_by_id(survey.id)
        print(f"Survey created and sent: {survey.id}")
        return
    if args.add_survey_participants:
        if not args.participants:
            raise SystemExit("--participants is required with --add-survey-participants.")
        participants = parse_survey_participants(args.participants, repo)
        try:
            result = bot.add_survey_participants_and_send(
                args.add_survey_participants,
                participants,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Survey participants updated: {args.add_survey_participants}")
        print(f"  added={len(result['added'])}")
        print(f"  already_present={len(result['already_present'])}")
        print(f"  sent={len(result['sent'])}")
        print(f"  not_messageable={len(result['not_messageable'])}")
        print(f"  failed={len(result['failed'])}")
        for member in result["sent"]:
            print(f"  sent: {member.name} (@{normalize_username(member.username)})")
        for member in result["already_present"]:
            print(f"  already_present: {member.name} (@{normalize_username(member.username)})")
        for member in result["not_messageable"]:
            print(f"  not_messageable: {member.name} (@{normalize_username(member.username)})")
        for member, exc in result["failed"]:
            print(f"  failed: {member.name} (@{normalize_username(member.username)}): {exc}")
        return
    if args.resend_survey_form:
        if not args.participants:
            raise SystemExit("--participants is required with --resend-survey-form.")
        participants = parse_survey_participants(args.participants, repo)
        try:
            result = bot.resend_survey_form_to_participants(
                args.resend_survey_form,
                participants,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Survey forms resent: {args.resend_survey_form}")
        print(f"  resent={len(result['resent'])}")
        print(f"  not_present={len(result['not_present'])}")
        print(f"  not_messageable={len(result['not_messageable'])}")
        print(f"  failed={len(result['failed'])}")
        for member in result["resent"]:
            print(f"  resent: {member.name} (@{normalize_username(member.username)})")
        for member in result["not_present"]:
            print(f"  not_present: {member.name} (@{normalize_username(member.username)})")
        for member in result["not_messageable"]:
            print(f"  not_messageable: {member.name} (@{normalize_username(member.username)})")
        for member, exc in result["failed"]:
            print(f"  failed: {member.name} (@{normalize_username(member.username)}): {exc}")
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
