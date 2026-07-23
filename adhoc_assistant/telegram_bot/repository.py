import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from adhoc_assistant.scheduler import stats_to_plain_dict
from adhoc_assistant.storage import init_db as init_schedule_db
from adhoc_assistant.telegram_bot.settings import Member, RuntimeSettings


RUNTIME_SETTINGS_KEY = "runtime_settings"
ACTIVE_SCHEDULE_SOURCE_KEY = "active_schedule_source"
SURVEY_KIND_PRODUCTION = "production"
SURVEY_KIND_DEBUG = "debug"
SURVEY_KINDS = {SURVEY_KIND_PRODUCTION, SURVEY_KIND_DEBUG}
TERMINAL_SURVEY_STATUSES = {"approved", "canceled"}

SURVEY_SELECT_COLUMNS = """
    id,
    calendar_type,
    year,
    month,
    status,
    starts_at,
    closes_at,
    created_by,
    requested_at,
    preview_sent_at,
    approved_at,
    group_sent_at,
    image_path,
    schedule_json,
    stats_json,
    review_json,
    created_at,
    updated_at,
    kind
"""


def active_survey_id_state_key(kind: str) -> str:
    return f"active_survey_id:{kind}"


def active_survey_phase_state_key(kind: str) -> str:
    return f"active_survey:{kind}"


@dataclass
class AvailabilityResponse:
    telegram_id: int
    name: str
    unavailable_days: list[int]
    unavailable_weekdays: list[str]
    confirmed: bool = False
    mode: str = "custom"


@dataclass(frozen=True)
class Survey:
    id: str
    calendar_type: str
    year: int
    month: int
    status: str
    starts_at: str
    closes_at: str
    created_by: str
    requested_at: str
    preview_sent_at: str | None
    approved_at: str | None
    group_sent_at: str | None
    image_path: str | None
    schedule_json: str | None
    stats_json: str | None
    review_json: str | None
    created_at: str
    updated_at: str
    kind: str = SURVEY_KIND_PRODUCTION


def normalize_username(username: str) -> str:
    return username.strip().lower().lstrip("@")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(raw: str | None) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def survey_counts_as_active(survey: Survey, *, now: datetime | None = None) -> bool:
    if survey.status == "canceled":
        return False
    if survey.status != "approved":
        return True
    if not survey.group_sent_at:
        return False
    closes_at = parse_iso_datetime(survey.closes_at)
    if closes_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current < closes_at


def stable_participant_id(member: Member) -> int:
    """Stable survey/response identity. Real Telegram IDs stay positive."""
    telegram_id = int(member.telegram_id or 0)
    if telegram_id > 0:
        return telegram_id
    if telegram_id < 0:
        return telegram_id
    username = normalize_username(member.username)
    if not username:
        raise ValueError(
            f"Participant {member.name!r} needs a telegram_id or username for stable identity."
        )
    digest = int(hashlib.md5(username.encode("utf-8")).hexdigest()[:8], 16)
    return -(1_000_000 + digest % 1_000_000_000)


def schedule_person_key(member: Member) -> str:
    """Stable schedule/history identity. Display name is cosmetic only."""
    username = normalize_username(member.username)
    if username:
        return username
    return member.name.strip()


def display_name_or_username(display_name: str, username: str) -> str:
    display_name = str(display_name or "").strip()
    if display_name:
        return display_name
    username = normalize_username(username)
    if username:
        return username
    return ""


class BotRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        init_schedule_db(self.db_path)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS surveys (
                    id TEXT PRIMARY KEY,
                    calendar_type TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    closes_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    preview_sent_at TEXT,
                    approved_at TEXT,
                    group_sent_at TEXT,
                    image_path TEXT,
                    schedule_json TEXT,
                    stats_json TEXT,
                    review_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS survey_participants (
                    survey_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    access_level TEXT NOT NULL,
                    participates_in_schedule INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL,
                    unavailable_days TEXT NOT NULL,
                    unavailable_weekdays TEXT NOT NULL,
                    PRIMARY KEY (survey_id, telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS survey_responses (
                    survey_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    unavailable_days TEXT NOT NULL,
                    unavailable_weekdays TEXT NOT NULL,
                    confirmed INTEGER NOT NULL,
                    availability_mode TEXT NOT NULL DEFAULT 'custom',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (survey_id, telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS survey_messages (
                    survey_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (survey_id, telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS survey_message_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    survey_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'form',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    UNIQUE(survey_id, telegram_id, chat_id, message_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS availability_responses (
                    calendar_type TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    unavailable_days TEXT NOT NULL,
                    unavailable_weekdays TEXT NOT NULL,
                    confirmed INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (calendar_type, year, month, telegram_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_monthly_runs (
                    calendar_type TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT,
                    preview_sent_at TEXT,
                    approved_at TEXT,
                    group_sent_at TEXT,
                    image_path TEXT,
                    schedule_json TEXT,
                    stats_json TEXT,
                    review_json TEXT,
                    PRIMARY KEY (calendar_type, year, month)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_update_tracking (
                    update_id INTEGER PRIMARY KEY,
                    callback_query_id TEXT UNIQUE,
                    status TEXT NOT NULL,
                    partition_key TEXT NOT NULL,
                    worker_id INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    enqueued_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_reminders (
                    work_date TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_users (
                    username TEXT PRIMARY KEY,
                    telegram_id INTEGER UNIQUE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    access_level TEXT NOT NULL,
                    participates_in_schedule INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    unavailable_days TEXT NOT NULL,
                    unavailable_weekdays TEXT NOT NULL,
                    registered_at TEXT,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.ensure_column(
                conn,
                "availability_responses",
                "availability_mode",
                "TEXT NOT NULL DEFAULT 'custom'",
            )
            self.ensure_column(
                conn,
                "bot_monthly_runs",
                "review_json",
                "TEXT",
            )
            self.ensure_column(
                conn,
                "bot_users",
                "unavailable_days",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self.ensure_column(
                conn,
                "bot_users",
                "unavailable_weekdays",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self.ensure_column(
                conn,
                "bot_users",
                "participates_in_schedule",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self.ensure_column(
                conn,
                "bot_monthly_runs",
                "survey_id",
                "TEXT",
            )
            self.ensure_column(
                conn,
                "surveys",
                "kind",
                "TEXT NOT NULL DEFAULT 'production'",
            )
            self.ensure_column(
                conn,
                "telegram_update_tracking",
                "attempt_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO survey_message_history (
                    survey_id,
                    telegram_id,
                    chat_id,
                    message_id,
                    kind,
                    active,
                    created_at,
                    updated_at
                )
                SELECT
                    survey_id,
                    telegram_id,
                    chat_id,
                    message_id,
                    'form',
                    1,
                    updated_at,
                    updated_at
                FROM survey_messages
                """
            )

    def ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def get_response(
        self,
        calendar_type: str,
        year: int,
        month: int,
        telegram_id: int,
    ) -> AvailabilityResponse | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    telegram_id,
                    name,
                    unavailable_days,
                    unavailable_weekdays,
                    confirmed,
                    availability_mode
                FROM availability_responses
                WHERE calendar_type = ? AND year = ? AND month = ? AND telegram_id = ?
                """,
                (calendar_type, year, month, telegram_id),
            ).fetchone()

        if row is None:
            return None
        return AvailabilityResponse(
            telegram_id=row[0],
            name=row[1],
            unavailable_days=json.loads(row[2]),
            unavailable_weekdays=json.loads(row[3]),
            confirmed=bool(row[4]),
            mode=row[5],
        )

    def get_survey_response(
        self,
        survey_id: str,
        telegram_id: int,
    ) -> AvailabilityResponse | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    telegram_id,
                    name,
                    unavailable_days,
                    unavailable_weekdays,
                    confirmed,
                    availability_mode
                FROM survey_responses
                WHERE survey_id = ? AND telegram_id = ?
                """,
                (survey_id, telegram_id),
            ).fetchone()

        if row is None:
            return None
        return AvailabilityResponse(
            telegram_id=row[0],
            name=row[1],
            unavailable_days=json.loads(row[2]),
            unavailable_weekdays=json.loads(row[3]),
            confirmed=bool(row[4]),
            mode=row[5],
        )

    def save_response(
        self,
        calendar_type: str,
        year: int,
        month: int,
        response: AvailabilityResponse,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO availability_responses (
                    calendar_type,
                    year,
                    month,
                    telegram_id,
                    name,
                    unavailable_days,
                    unavailable_weekdays,
                    confirmed,
                    availability_mode,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_type, year, month, telegram_id)
                DO UPDATE SET
                    name = excluded.name,
                    unavailable_days = excluded.unavailable_days,
                    unavailable_weekdays = excluded.unavailable_weekdays,
                    confirmed = excluded.confirmed,
                    availability_mode = excluded.availability_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    calendar_type,
                    year,
                    month,
                    response.telegram_id,
                    response.name,
                    json.dumps(sorted(response.unavailable_days)),
                    json.dumps(sorted(response.unavailable_weekdays)),
                    1 if response.confirmed else 0,
                    response.mode,
                    utc_now(),
                ),
            )

    def save_survey_response(
        self,
        survey_id: str,
        response: AvailabilityResponse,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO survey_responses (
                    survey_id,
                    telegram_id,
                    name,
                    unavailable_days,
                    unavailable_weekdays,
                    confirmed,
                    availability_mode,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(survey_id, telegram_id)
                DO UPDATE SET
                    name = excluded.name,
                    unavailable_days = excluded.unavailable_days,
                    unavailable_weekdays = excluded.unavailable_weekdays,
                    confirmed = excluded.confirmed,
                    availability_mode = excluded.availability_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    survey_id,
                    response.telegram_id,
                    response.name,
                    json.dumps(sorted(response.unavailable_days)),
                    json.dumps(sorted(response.unavailable_weekdays)),
                    1 if response.confirmed else 0,
                    response.mode,
                    utc_now(),
                ),
            )

    def delete_response(
        self,
        calendar_type: str,
        year: int,
        month: int,
        telegram_id: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM availability_responses
                WHERE calendar_type = ? AND year = ? AND month = ? AND telegram_id = ?
                """,
                (calendar_type, year, month, telegram_id),
            )

    def delete_month_responses(self, calendar_type: str, year: int, month: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM availability_responses
                WHERE calendar_type = ? AND year = ? AND month = ?
                """,
                (calendar_type, year, month),
            )

    def delete_survey_responses(self, survey_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM survey_responses WHERE survey_id = ?", (survey_id,))

    def list_responses(
        self,
        calendar_type: str,
        year: int,
        month: int,
    ) -> dict[int, AvailabilityResponse]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    telegram_id,
                    name,
                    unavailable_days,
                    unavailable_weekdays,
                    confirmed,
                    availability_mode
                FROM availability_responses
                WHERE calendar_type = ? AND year = ? AND month = ?
                """,
                (calendar_type, year, month),
            ).fetchall()

        return {
            row[0]: AvailabilityResponse(
                telegram_id=row[0],
                name=row[1],
                unavailable_days=json.loads(row[2]),
                unavailable_weekdays=json.loads(row[3]),
                confirmed=bool(row[4]),
                mode=row[5],
            )
            for row in rows
        }

    def list_survey_responses(self, survey_id: str) -> dict[int, AvailabilityResponse]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    telegram_id,
                    name,
                    unavailable_days,
                    unavailable_weekdays,
                    confirmed,
                    availability_mode
                FROM survey_responses
                WHERE survey_id = ?
                """,
                (survey_id,),
            ).fetchall()

        return {
            row[0]: AvailabilityResponse(
                telegram_id=row[0],
                name=row[1],
                unavailable_days=json.loads(row[2]),
                unavailable_weekdays=json.loads(row[3]),
                confirmed=bool(row[4]),
                mode=row[5],
            )
            for row in rows
        }

    def set_state(self, key: str, value: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, json.dumps(value)),
            )

    def get_state(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (key,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete_state(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))

    def get_runtime_settings(self) -> RuntimeSettings:
        """Load complete DB-backed policy. Raises if missing or incomplete."""
        self.migrate_legacy_runtime_settings()
        raw = self.get_state(RUNTIME_SETTINGS_KEY)
        if not raw:
            raise RuntimeError(
                "runtime_settings is missing from the database. "
                "Initialize with local CLI --set-* flags "
                "(for example --set-telegram-token, --set-timezone, "
                "--set-calendar, --set-output-dir, --set-holidays)."
            )
        return RuntimeSettings.from_dict(raw)

    def get_runtime_settings_raw(self) -> dict:
        self.migrate_legacy_runtime_settings()
        return dict(self.get_state(RUNTIME_SETTINGS_KEY) or {})

    def set_runtime_settings(self, settings: RuntimeSettings) -> None:
        self.set_state(RUNTIME_SETTINGS_KEY, settings.as_dict())

    def update_runtime_settings(self, **updates) -> dict:
        """Merge fields into runtime_settings. May leave the blob incomplete until all required keys are set."""
        current = self.get_runtime_settings_raw()
        current.update(updates)
        self.set_state(RUNTIME_SETTINGS_KEY, current)
        return current

    def migrate_legacy_runtime_settings(self) -> None:
        """Merge older single-purpose bot_state keys into runtime_settings once."""
        current = self.get_state(RUNTIME_SETTINGS_KEY) or {}
        changed = False

        reminder = self.get_state("daily_reminder_time")
        if reminder and reminder.get("time") and "daily_reminder_time" not in current:
            current["daily_reminder_time"] = reminder["time"]
            changed = True

        if changed:
            self.set_state(RUNTIME_SETTINGS_KEY, current)
    def set_telegram_destination(
        self,
        *,
        group_chat_id: int,
        topic_id: int | None = None,
    ) -> None:
        self.set_state(
            "telegram_destination",
            {
                "group_chat_id": int(group_chat_id),
                "topic_id": int(topic_id) if topic_id is not None else None,
            },
        )

    def get_telegram_destination(self) -> dict | None:
        state = self.get_state("telegram_destination")
        if not state or not state.get("group_chat_id"):
            return None
        topic_id = state.get("topic_id")
        return {
            "group_chat_id": int(state["group_chat_id"]),
            "topic_id": int(topic_id) if topic_id not in (None, "") else None,
        }

    def set_update_offset(self, offset: int) -> None:
        self.set_state("telegram_update_offset", {"offset": offset})

    def get_update_offset(self) -> int | None:
        state = self.get_state("telegram_update_offset")
        if not state:
            return None
        return int(state["offset"])

    def claim_telegram_update(
        self,
        *,
        update_id: int,
        callback_query_id: str | None,
        partition_key: str,
        worker_id: int,
    ) -> bool:
        now = utc_now()
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO telegram_update_tracking (
                        update_id,
                        callback_query_id,
                        status,
                        partition_key,
                        worker_id,
                        received_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(update_id),
                        callback_query_id or None,
                        "claimed",
                        partition_key,
                        int(worker_id),
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT status
                    FROM telegram_update_tracking
                    WHERE update_id = ?
                    """,
                    (int(update_id),),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO telegram_update_tracking (
                            update_id,
                            callback_query_id,
                            status,
                            partition_key,
                            worker_id,
                            received_at,
                            finished_at,
                            error
                        )
                        VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(update_id),
                            "skipped_duplicate",
                            partition_key,
                            int(worker_id),
                            now,
                            now,
                            (
                                f"duplicate callback_query_id={callback_query_id}"
                                if callback_query_id
                                else "duplicate telegram update"
                            ),
                        ),
                    )
                    return False
                if row[0] != "pending_retry":
                    return False
                conn.execute(
                    """
                    UPDATE telegram_update_tracking
                    SET callback_query_id = ?,
                        status = ?,
                        partition_key = ?,
                        worker_id = ?,
                        received_at = ?,
                        enqueued_at = NULL,
                        started_at = NULL,
                        finished_at = NULL,
                        error = NULL
                    WHERE update_id = ?
                    """,
                    (
                        callback_query_id or None,
                        "claimed",
                        partition_key,
                        int(worker_id),
                        now,
                        int(update_id),
                    ),
                )
        return True

    def mark_telegram_update_enqueued(self, update_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_update_tracking
                SET status = ?, enqueued_at = ?
                WHERE update_id = ? AND status = ?
                """,
                ("enqueued", utc_now(), int(update_id), "claimed"),
            )

    def mark_telegram_update_started(self, update_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_update_tracking
                SET status = ?, started_at = ?
                WHERE update_id = ? AND status IN ('claimed', 'enqueued', 'pending_retry')
                """,
                ("processing", utc_now(), int(update_id)),
            )

    def mark_telegram_update_processed(self, update_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_update_tracking
                SET status = ?, finished_at = ?, error = NULL
                WHERE update_id = ?
                """,
                ("processed", utc_now(), int(update_id)),
            )

    def mark_telegram_update_failed(
        self,
        update_id: int,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT attempt_count
                FROM telegram_update_tracking
                WHERE update_id = ?
                """,
                (int(update_id),),
            ).fetchone()
            attempt_count = (int(row[0]) if row is not None else 0) + 1
            status = "failed_terminal" if attempt_count >= max(1, int(max_attempts)) else "pending_retry"
            conn.execute(
                """
                UPDATE telegram_update_tracking
                SET status = ?, finished_at = ?, attempt_count = ?, error = ?
                WHERE update_id = ?
                """,
                (status, utc_now(), attempt_count, error[:1000], int(update_id)),
            )
        return status

    def cleanup_telegram_update_tracking(self, cutoff: datetime) -> int:
        cutoff_utc = cutoff
        if cutoff_utc.tzinfo is None:
            cutoff_utc = cutoff_utc.replace(tzinfo=timezone.utc)
        else:
            cutoff_utc = cutoff_utc.astimezone(timezone.utc)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM telegram_update_tracking
                WHERE status IN ('processed', 'skipped_duplicate', 'failed_terminal')
                  AND COALESCE(finished_at, received_at) < ?
                """,
                (cutoff_utc.isoformat(timespec="seconds"),),
            )
            return int(cursor.rowcount or 0)

    def reset_incomplete_telegram_updates(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_update_tracking
                SET status = ?, enqueued_at = NULL, started_at = NULL, finished_at = NULL, error = NULL
                WHERE status IN ('claimed', 'enqueued', 'processing')
                """,
                ("pending_retry",),
            )

    def advance_update_offset_from_tracking(self, current_offset: int | None) -> int | None:
        with self.connect() as conn:
            if current_offset is None:
                row = conn.execute(
                    """
                    SELECT MIN(update_id)
                    FROM telegram_update_tracking
                    """
                ).fetchone()
                if row is None or row[0] is None:
                    return None
                offset = int(row[0])
            else:
                offset = int(current_offset)

            while True:
                row = conn.execute(
                    """
                    SELECT status
                    FROM telegram_update_tracking
                    WHERE update_id = ?
                    """,
                    (offset,),
                ).fetchone()
                if row is None:
                    next_row = conn.execute(
                        """
                        SELECT MIN(update_id)
                        FROM telegram_update_tracking
                        WHERE update_id > ?
                        """,
                        (offset,),
                    ).fetchone()
                    if next_row is None or next_row[0] is None:
                        break
                    offset = int(next_row[0])
                    continue
                if row[0] not in {
                    "processed",
                    "skipped_duplicate",
                    "failed_terminal",
                }:
                    break
                offset += 1

        if current_offset is not None and offset == int(current_offset):
            return current_offset
        self.set_update_offset(offset)
        return offset

    def normalize_survey_kind(self, kind: str) -> str:
        normalized = kind.strip().lower()
        if normalized not in SURVEY_KINDS:
            raise ValueError(f"Unsupported survey kind: {kind}")
        return normalized

    def set_active_survey_id(self, survey_id: str, kind: str) -> None:
        kind = self.normalize_survey_kind(kind)
        self.set_state(active_survey_id_state_key(kind), {"id": survey_id})

    def clear_active_survey_id(self, kind: str) -> None:
        kind = self.normalize_survey_kind(kind)
        self.delete_state(active_survey_id_state_key(kind))

    def migrate_legacy_active_survey_state(self) -> None:
        legacy_id = self.get_state("active_survey_id")
        if legacy_id and legacy_id.get("id"):
            production = self.get_state(active_survey_id_state_key(SURVEY_KIND_PRODUCTION))
            if not production:
                self.set_state(
                    active_survey_id_state_key(SURVEY_KIND_PRODUCTION),
                    {"id": legacy_id["id"]},
                )
            self.delete_state("active_survey_id")

        legacy_phase = self.get_state("active_survey")
        if legacy_phase and legacy_phase.get("id"):
            production_phase = self.get_state(
                active_survey_phase_state_key(SURVEY_KIND_PRODUCTION)
            )
            if not production_phase:
                self.set_state(
                    active_survey_phase_state_key(SURVEY_KIND_PRODUCTION),
                    legacy_phase,
                )
            self.delete_state("active_survey")

    def active_survey_conflict_message(self, active: Survey, kind: str) -> str:
        status = active.status
        if status == "approved" and active.closes_at:
            status = f"approved until {active.closes_at}"
        return (
            f"Cannot create a new {kind} survey; survey {active.id} "
            f"({active.calendar_type} {active.year}-{active.month:02d}) "
            f"is still {status}. Pass replace_active=True / "
            f"--replace-active-survey to cancel it and start a new one."
        )

    def create_survey(
        self,
        *,
        survey_id: str,
        calendar_type: str,
        year: int,
        month: int,
        status: str,
        starts_at: str,
        closes_at: str,
        created_by: str,
        participants: list[Member],
        kind: str = SURVEY_KIND_PRODUCTION,
        replace_active: bool = False,
    ) -> Survey:
        """Create a survey.

        At most one still-active survey per kind. Approved surveys remain
        active until their closes_at window ends, unless canceled manually.
        Without replace_active, an existing active survey of the same kind is a
        hard conflict. With replace_active, every still-active survey of that
        kind is canceled in the same transaction before the new row is inserted.
        """
        kind = self.normalize_survey_kind(kind)
        self.migrate_legacy_active_survey_state()
        normalized_participants: list[tuple] = []
        seen_ids: set[int] = set()
        for member in participants:
            participant_id = stable_participant_id(member)
            if participant_id in seen_ids:
                raise ValueError(
                    f"Duplicate survey participant identity for {member.name!r} "
                    f"(id={participant_id}). Unregistered users need distinct usernames."
                )
            seen_ids.add(participant_id)
            normalized_participants.append(
                (
                    survey_id,
                    participant_id,
                    normalize_username(member.username),
                    member.name,
                    member.role,
                    member.access_level,
                    1 if member.participates_in_schedule else 0,
                    1 if member.active else 0,
                    json.dumps(sorted(member.unavailable_days)),
                    json.dumps(sorted(member.unavailable_weekdays)),
                )
            )

        now = utc_now()
        replaced_ids: list[str] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_rows = conn.execute(
                f"""
                SELECT {SURVEY_SELECT_COLUMNS}
                FROM surveys
                WHERE kind = ?
                  AND status != 'canceled'
                ORDER BY created_at DESC
                """
            , (kind,)).fetchall()
            active_conflicts = [
                self.survey_row_to_dataclass(row)
                for row in active_rows
                if survey_counts_as_active(self.survey_row_to_dataclass(row))
            ]
            if active_conflicts:
                if not replace_active:
                    active = active_conflicts[0]
                    raise ValueError(self.active_survey_conflict_message(active, kind))
                for active in active_conflicts:
                    conn.execute(
                        """
                        UPDATE surveys
                        SET status = ?, updated_at = ?
                        WHERE id = ? AND status != 'canceled'
                        """,
                        ("canceled", now, active.id),
                    )
                    replaced_ids.append(active.id)
            conn.execute(
                """
                INSERT INTO surveys (
                    id,
                    calendar_type,
                    year,
                    month,
                    status,
                    starts_at,
                    closes_at,
                    created_by,
                    requested_at,
                    created_at,
                    updated_at,
                    kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    survey_id,
                    calendar_type,
                    year,
                    month,
                    status,
                    starts_at,
                    closes_at,
                    created_by,
                    now,
                    now,
                    now,
                    kind,
                ),
            )
            conn.executemany(
                """
                INSERT INTO survey_participants (
                    survey_id,
                    telegram_id,
                    username,
                    name,
                    role,
                    access_level,
                    participates_in_schedule,
                    active,
                    unavailable_days,
                    unavailable_weekdays
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized_participants,
            )
            # Active pointer is owned by surveys; keep it in the same txn.
            conn.execute(
                """
                INSERT INTO bot_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (active_survey_id_state_key(kind), json.dumps({"id": survey_id})),
            )
            if status in TERMINAL_SURVEY_STATUSES:
                conn.execute(
                    "DELETE FROM bot_state WHERE key IN (?, ?)",
                    (active_survey_id_state_key(kind), active_survey_phase_state_key(kind)),
                )
        if replaced_ids:
            for old_id in replaced_ids:
                if kind == SURVEY_KIND_PRODUCTION:
                    old = self.get_survey(old_id)
                    if old is not None:
                        self.upsert_monthly_run(
                            old.calendar_type,
                            old.year,
                            old.month,
                            old.status,
                            survey_id=old.id,
                            requested_at=old.requested_at,
                            preview_sent_at=old.preview_sent_at,
                            approved_at=old.approved_at,
                            group_sent_at=old.group_sent_at,
                            image_path=old.image_path,
                            schedule_json=old.schedule_json,
                            stats_json=old.stats_json,
                            review_json=old.review_json,
                        )
        if kind == SURVEY_KIND_PRODUCTION:
            self.upsert_monthly_run(
                calendar_type,
                year,
                month,
                status,
                survey_id=survey_id,
                requested_at=now,
            )
        survey = self.get_survey(survey_id)
        if survey is None:
            raise RuntimeError(f"Survey {survey_id} was not created.")
        self.clear_revision_allowlist(kind)
        return survey

    def survey_row_to_dataclass(self, row: sqlite3.Row | tuple) -> Survey:
        kind = row[18] if len(row) > 18 and row[18] else SURVEY_KIND_PRODUCTION
        return Survey(
            id=row[0],
            calendar_type=row[1],
            year=int(row[2]),
            month=int(row[3]),
            status=row[4],
            starts_at=row[5] or "",
            closes_at=row[6] or "",
            created_by=row[7] or "",
            requested_at=row[8] or "",
            preview_sent_at=row[9],
            approved_at=row[10],
            group_sent_at=row[11],
            image_path=row[12],
            schedule_json=row[13],
            stats_json=row[14],
            review_json=row[15],
            created_at=row[16] or "",
            updated_at=row[17] or "",
            kind=kind,
        )

    def get_survey(self, survey_id: str) -> Survey | None:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {SURVEY_SELECT_COLUMNS}
                FROM surveys
                WHERE id = ?
                """,
                (survey_id,),
            ).fetchone()
        return self.survey_row_to_dataclass(row) if row else None

    def get_active_survey(self, kind: str = SURVEY_KIND_PRODUCTION) -> Survey | None:
        """Return the survey pointed at by active_survey_id:{kind}.

        The pointer is the only source of truth for "active". Orphan
        non-terminal rows are not revived; clear a stale pointer instead.
        """
        kind = self.normalize_survey_kind(kind)
        self.migrate_legacy_active_survey_state()
        state = self.get_state(active_survey_id_state_key(kind))
        if not state or not state.get("id"):
            return None
        survey = self.get_survey(str(state["id"]))
        if survey is None or not survey_counts_as_active(survey):
            self.clear_active_survey_id(kind)
            self.clear_active_survey_phase(kind)
            return None
        return survey

    def list_non_terminal_surveys(self, kind: str | None = None) -> list[Survey]:
        with self.connect() as conn:
            if kind is None:
                rows = conn.execute(
                    f"""
                    SELECT {SURVEY_SELECT_COLUMNS}
                    FROM surveys
                    WHERE status NOT IN ('approved', 'canceled')
                    ORDER BY created_at DESC
                    """
                ).fetchall()
            else:
                kind = self.normalize_survey_kind(kind)
                rows = conn.execute(
                    f"""
                    SELECT {SURVEY_SELECT_COLUMNS}
                    FROM surveys
                    WHERE status NOT IN ('approved', 'canceled')
                      AND kind = ?
                    ORDER BY created_at DESC
                    """,
                    (kind,),
                ).fetchall()
        return [
            survey
            for row in rows
            if (survey := self.survey_row_to_dataclass(row)) and survey_counts_as_active(survey)
        ]

    def get_latest_survey_for_month(
        self,
        calendar_type: str,
        year: int,
        month: int,
        *,
        kind: str | None = None,
    ) -> Survey | None:
        with self.connect() as conn:
            if kind is None:
                row = conn.execute(
                    f"""
                    SELECT {SURVEY_SELECT_COLUMNS}
                    FROM surveys
                    WHERE calendar_type = ? AND year = ? AND month = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (calendar_type, year, month),
                ).fetchone()
            else:
                kind = self.normalize_survey_kind(kind)
                row = conn.execute(
                    f"""
                    SELECT {SURVEY_SELECT_COLUMNS}
                    FROM surveys
                    WHERE calendar_type = ? AND year = ? AND month = ? AND kind = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (calendar_type, year, month, kind),
                ).fetchone()
        return self.survey_row_to_dataclass(row) if row else None

    def list_surveys(self) -> list[Survey]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {SURVEY_SELECT_COLUMNS}
                FROM surveys
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [self.survey_row_to_dataclass(row) for row in rows]

    def set_revision_allowlist(self, survey: Survey, member_ids: list[int]) -> None:
        """Store revision targeting allowlist for the active survey of this kind.

        This is the only business payload kept under active_survey:{kind}.
        Lifecycle status/deadline live exclusively on the surveys row.
        """
        self.set_state(
            active_survey_phase_state_key(survey.kind),
            {
                "id": survey.id,
                "kind": survey.kind,
                "allowed_member_ids": sorted({int(item) for item in member_ids}),
            },
        )

    def get_revision_allowlist(self, survey: Survey) -> list[int]:
        state = self.get_state(active_survey_phase_state_key(survey.kind))
        if not state or state.get("id") != survey.id:
            return []
        return [int(item) for item in state.get("allowed_member_ids") or []]

    def clear_revision_allowlist(self, kind: str) -> None:
        kind = self.normalize_survey_kind(kind)
        self.delete_state(active_survey_phase_state_key(kind))

    def remove_revision_allowlist_members(self, survey: Survey, member_ids: list[int]) -> None:
        if not member_ids:
            return
        current = self.get_revision_allowlist(survey)
        blocked = {int(item) for item in member_ids}
        remaining = [item for item in current if int(item) not in blocked]
        if remaining:
            self.set_revision_allowlist(survey, remaining)
        else:
            self.clear_revision_allowlist(survey.kind)

    def clear_active_survey_phase(self, kind: str) -> None:
        """Clear revision allowlist and active survey pointer for kind."""
        kind = self.normalize_survey_kind(kind)
        self.clear_revision_allowlist(kind)
        self.clear_active_survey_id(kind)

    def update_survey(
        self,
        survey_id: str,
        *,
        status: str | None = None,
        starts_at: str | None = None,
        closes_at: str | None = None,
        preview_sent_at: str | None = None,
        approved_at: str | None = None,
        group_sent_at: str | None = None,
        image_path: str | None = None,
        schedule_json: str | None = None,
        stats_json: str | None = None,
        review_json: str | None = None,
    ) -> None:
        existing = self.get_survey(survey_id)
        if existing is None:
            raise ValueError(f"Survey not found: {survey_id}")
        fields = {
            "status": status,
            "starts_at": starts_at,
            "closes_at": closes_at,
            "preview_sent_at": preview_sent_at,
            "approved_at": approved_at,
            "group_sent_at": group_sent_at,
            "image_path": image_path,
            "schedule_json": schedule_json,
            "stats_json": stats_json,
            "review_json": review_json,
        }
        updates = {key: value for key, value in fields.items() if value is not None}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        values.extend([utc_now(), survey_id])
        with self.connect() as conn:
            conn.execute(
                f"UPDATE surveys SET {assignments}, updated_at = ? WHERE id = ?",
                values,
            )
        updated = self.get_survey(survey_id)
        if updated:
            if survey_counts_as_active(updated):
                self.set_active_survey_id(survey_id, updated.kind)
            else:
                state = self.get_state(active_survey_id_state_key(updated.kind))
                if state and state.get("id") == survey_id:
                    self.clear_active_survey_id(updated.kind)
                    self.clear_active_survey_phase(updated.kind)
            if updated.kind == SURVEY_KIND_PRODUCTION:
                self.upsert_monthly_run(
                    updated.calendar_type,
                    updated.year,
                    updated.month,
                    updated.status,
                    survey_id=updated.id,
                    requested_at=updated.requested_at,
                    preview_sent_at=updated.preview_sent_at,
                    approved_at=updated.approved_at,
                    group_sent_at=updated.group_sent_at,
                    image_path=updated.image_path,
                    schedule_json=updated.schedule_json,
                    stats_json=updated.stats_json,
                    review_json=updated.review_json,
                )

    def transition_survey(
        self,
        survey_id: str,
        *,
        from_statuses: set[str],
        to_status: str,
        **fields,
    ) -> bool:
        placeholders = ", ".join("?" for _ in from_statuses)
        now = utc_now()
        updates = {"status": to_status, **fields, "updated_at": now}
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        values.extend([survey_id, *sorted(from_statuses)])
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE surveys
                SET {assignments}
                WHERE id = ? AND status IN ({placeholders})
                """,
                values,
            )
            changed = cursor.rowcount == 1
        if changed:
            updated = self.get_survey(survey_id)
            if updated:
                if survey_counts_as_active(updated):
                    self.set_active_survey_id(survey_id, updated.kind)
                else:
                    state = self.get_state(active_survey_id_state_key(updated.kind))
                    if state and state.get("id") == survey_id:
                        self.clear_active_survey_id(updated.kind)
                        self.clear_active_survey_phase(updated.kind)
                if updated.kind == SURVEY_KIND_PRODUCTION:
                    self.upsert_monthly_run(
                        updated.calendar_type,
                        updated.year,
                        updated.month,
                        updated.status,
                        survey_id=updated.id,
                        requested_at=updated.requested_at,
                        preview_sent_at=updated.preview_sent_at,
                        approved_at=updated.approved_at,
                        group_sent_at=updated.group_sent_at,
                        image_path=updated.image_path,
                        schedule_json=updated.schedule_json,
                        stats_json=updated.stats_json,
                        review_json=updated.review_json,
                    )
        return changed

    def set_active_schedule_source(self, year: int, month: int) -> None:
        self.set_state(ACTIVE_SCHEDULE_SOURCE_KEY, {"year": int(year), "month": int(month)})

    def get_active_schedule_source(self) -> tuple[int, int] | None:
        state = self.get_state(ACTIVE_SCHEDULE_SOURCE_KEY)
        if not state:
            return None
        year = state.get("year")
        month = state.get("month")
        if year is None or month is None:
            return None
        return int(year), int(month)

    def mark_daily_sent(self, work_date: str) -> None:
        """Record that today's reminder was delivered successfully."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_reminders (work_date, sent_at)
                VALUES (?, ?)
                """,
                (work_date, utc_now()),
            )

    def daily_was_sent(self, work_date: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_reminders WHERE work_date = ?",
                (work_date,),
            ).fetchone()
        return row is not None

    def clear_daily_reminder(self, work_date: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM daily_reminders WHERE work_date = ?", (work_date,))

    def has_schedule_month(self, year: int, month: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM schedule_entries
                WHERE year = ? AND month = ?
                LIMIT 1
                """,
                (year, month),
            ).fetchone()
        return row is not None

    def unconfirm_survey_responses(self, survey_id: str, telegram_ids: list[int]) -> None:
        """Clear confirmed flags so revision members must respond again."""
        if not telegram_ids:
            return
        placeholders = ", ".join("?" for _ in telegram_ids)
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE survey_responses
                SET confirmed = 0, updated_at = ?
                WHERE survey_id = ? AND telegram_id IN ({placeholders})
                """,
                (utc_now(), survey_id, *telegram_ids),
            )

    def complete_publish(self, survey_id: str, *, activate_live: bool = False) -> bool:
        """Finalize a publishing survey.

        Production surveys always write schedule_entries/monthly_stats for their
        year/month. active_schedule_source (live reminders) switches only when
        activate_live is True — callers must not activate a future month early.

        Any failure raises so the connection context rolls back; never leave
        schedule writes committed while the survey stays in publishing.
        """
        survey = self.get_survey(survey_id)
        if survey is None or survey.status != "publishing":
            return False

        # Validate artifacts before opening the write transaction.
        if survey.kind == SURVEY_KIND_PRODUCTION:
            schedule = json.loads(survey.schedule_json or "[]")
            plain_stats = stats_to_plain_dict(json.loads(survey.stats_json or "{}"))
            if not isinstance(schedule, list):
                raise ValueError(f"Survey {survey.id} schedule_json must be a list.")
        else:
            schedule = []
            plain_stats = {}

        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE surveys
                SET status = ?, approved_at = ?, group_sent_at = COALESCE(group_sent_at, ?), updated_at = ?
                WHERE id = ? AND status = ?
                """,
                ("approved", now, now, now, survey.id, "publishing"),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Failed to approve survey {survey.id} from publishing "
                    "(status changed concurrently)."
                )

            if survey.kind == SURVEY_KIND_PRODUCTION:
                conn.execute(
                    "DELETE FROM monthly_stats WHERE year = ? AND month = ?",
                    (survey.year, survey.month),
                )
                conn.execute(
                    "DELETE FROM schedule_entries WHERE year = ? AND month = ?",
                    (survey.year, survey.month),
                )
                conn.executemany(
                    """
                    INSERT INTO monthly_stats (
                        year,
                        month,
                        person,
                        main_count,
                        backup_count,
                        total_count,
                        thursday_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            survey.year,
                            survey.month,
                            person,
                            counts["main_count"],
                            counts["backup_count"],
                            counts["total_count"],
                            counts["thursday_count"],
                        )
                        for person, counts in sorted(plain_stats.items())
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO schedule_entries (
                        year,
                        month,
                        work_date,
                        weekday,
                        holiday,
                        main,
                        backup
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            survey.year,
                            survey.month,
                            item.get("gregorian_date", item["date"]),
                            item["weekday"],
                            item["holiday"],
                            item["main"],
                            item["backup"],
                        )
                        for item in schedule
                    ],
                )
                if activate_live:
                    conn.execute(
                        """
                        INSERT INTO bot_state (key, value)
                        VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (
                            ACTIVE_SCHEDULE_SOURCE_KEY,
                            json.dumps(
                                {"year": int(survey.year), "month": int(survey.month)}
                            ),
                        ),
                    )

            if survey_counts_as_active(
                Survey(
                    id=survey.id,
                    calendar_type=survey.calendar_type,
                    year=survey.year,
                    month=survey.month,
                    status="approved",
                    starts_at=survey.starts_at,
                    closes_at=survey.closes_at,
                    created_by=survey.created_by,
                    requested_at=survey.requested_at,
                    preview_sent_at=survey.preview_sent_at,
                    approved_at=now,
                    group_sent_at=survey.group_sent_at or now,
                    image_path=survey.image_path,
                    schedule_json=survey.schedule_json,
                    stats_json=survey.stats_json,
                    review_json=survey.review_json,
                    created_at=survey.created_at,
                    updated_at=now,
                    kind=survey.kind,
                )
            ):
                conn.execute(
                    """
                    INSERT INTO bot_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (active_survey_id_state_key(survey.kind), json.dumps({"id": survey.id})),
                )
                conn.execute(
                    "DELETE FROM bot_state WHERE key = ?",
                    (active_survey_phase_state_key(survey.kind),),
                )
            else:
                conn.execute(
                    "DELETE FROM bot_state WHERE key IN (?, ?)",
                    (
                        active_survey_id_state_key(survey.kind),
                        active_survey_phase_state_key(survey.kind),
                    ),
                )

        updated = self.get_survey(survey.id)
        if updated and updated.kind == SURVEY_KIND_PRODUCTION:
            self.upsert_monthly_run(
                updated.calendar_type,
                updated.year,
                updated.month,
                updated.status,
                survey_id=updated.id,
                requested_at=updated.requested_at,
                preview_sent_at=updated.preview_sent_at,
                approved_at=updated.approved_at,
                group_sent_at=updated.group_sent_at,
                image_path=updated.image_path,
                schedule_json=updated.schedule_json,
                stats_json=updated.stats_json,
                review_json=updated.review_json,
            )
        return True

    def get_schedule_entry(self, work_date: str) -> tuple[str, str] | None:
        """Read today's main/backup from the live schedule source only.

        Without active_schedule_source there is no live reminder schedule —
        never fall back to "latest year/month" ordering (that conflates months).
        """
        source = self.get_active_schedule_source()
        if source is None:
            return None
        year, month = source
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT main, backup
                FROM schedule_entries
                WHERE work_date = ? AND year = ? AND month = ?
                LIMIT 1
                """,
                (work_date, year, month),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def list_survey_participants(self, survey_id: str) -> list[Member]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    username,
                    telegram_id,
                    name,
                    role,
                    access_level,
                    participates_in_schedule,
                    active,
                    unavailable_days,
                    unavailable_weekdays
                FROM survey_participants
                WHERE survey_id = ?
                ORDER BY name, username
                """,
                (survey_id,),
            ).fetchall()
        return [
            Member(
                name=row[2],
                telegram_id=int(row[1] or 0),
                username=row[0],
                role=row[3],
                active=bool(row[6]),
                unavailable_days=[int(day) for day in json.loads(row[7] or "[]")],
                unavailable_weekdays=[
                    str(weekday).strip().lower()
                    for weekday in json.loads(row[8] or "[]")
                    if str(weekday).strip()
                ],
                access_level=row[4],
                participates_in_schedule=bool(row[5]),
            )
            for row in rows
        ]

    def add_survey_participants(
        self,
        survey_id: str,
        participants: list[Member],
    ) -> tuple[list[Member], list[Member]]:
        existing_ids = {
            member.telegram_id
            for member in self.list_survey_participants(survey_id)
        }
        seen_ids: set[int] = set()
        added: list[Member] = []
        already_present: list[Member] = []
        rows = []
        for member in participants:
            participant_id = stable_participant_id(member)
            if participant_id in seen_ids:
                raise ValueError(
                    f"Duplicate survey participant identity for {member.name!r} "
                    f"(id={participant_id})."
                )
            seen_ids.add(participant_id)
            normalized = Member(
                name=member.name,
                telegram_id=participant_id,
                username=normalize_username(member.username),
                role=member.role,
                active=member.active,
                unavailable_days=list(member.unavailable_days),
                unavailable_weekdays=list(member.unavailable_weekdays),
                access_level=member.access_level,
                participates_in_schedule=member.participates_in_schedule,
            )
            if participant_id in existing_ids:
                already_present.append(normalized)
                continue
            added.append(normalized)
            rows.append(
                (
                    survey_id,
                    participant_id,
                    normalized.username,
                    normalized.name,
                    normalized.role,
                    normalized.access_level,
                    1 if normalized.participates_in_schedule else 0,
                    1 if normalized.active else 0,
                    json.dumps(sorted(normalized.unavailable_days)),
                    json.dumps(sorted(normalized.unavailable_weekdays)),
                )
            )

        if rows:
            with self.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO survey_participants (
                        survey_id,
                        telegram_id,
                        username,
                        name,
                        role,
                        access_level,
                        participates_in_schedule,
                        active,
                        unavailable_days,
                        unavailable_weekdays
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        return added, already_present

    def remove_survey_participants(
        self,
        survey_id: str,
        participants: list[Member],
    ) -> tuple[list[Member], list[Member]]:
        existing = {
            member.telegram_id: member
            for member in self.list_survey_participants(survey_id)
        }
        seen_ids: set[int] = set()
        removed: list[Member] = []
        not_present: list[Member] = []
        target_ids: list[int] = []
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
                not_present.append(
                    Member(
                        name=member.name,
                        telegram_id=participant_id,
                        username=normalize_username(member.username),
                        role=member.role,
                        active=member.active,
                        unavailable_days=list(member.unavailable_days),
                        unavailable_weekdays=list(member.unavailable_weekdays),
                        access_level=member.access_level,
                        participates_in_schedule=member.participates_in_schedule,
                    )
                )
                continue
            removed.append(existing_member)
            target_ids.append(participant_id)

        if target_ids:
            placeholders = ", ".join("?" for _ in target_ids)
            params = [survey_id, *target_ids]
            with self.connect() as conn:
                conn.execute(
                    f"""
                    DELETE FROM survey_participants
                    WHERE survey_id = ? AND telegram_id IN ({placeholders})
                    """,
                    params,
                )
                conn.execute(
                    f"""
                    DELETE FROM survey_responses
                    WHERE survey_id = ? AND telegram_id IN ({placeholders})
                    """,
                    params,
                )
                conn.execute(
                    f"""
                    DELETE FROM survey_messages
                    WHERE survey_id = ? AND telegram_id IN ({placeholders})
                    """,
                    params,
                )
                conn.execute(
                    f"""
                    DELETE FROM survey_message_history
                    WHERE survey_id = ? AND telegram_id IN ({placeholders})
                    """,
                    params,
                )
        return removed, not_present

    def record_survey_message(
        self,
        survey_id: str,
        telegram_id: int,
        chat_id: int,
        message_id: int,
        kind: str = "form",
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO survey_messages (
                    survey_id,
                    telegram_id,
                    chat_id,
                    message_id,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(survey_id, telegram_id)
                DO UPDATE SET
                    chat_id = excluded.chat_id,
                    message_id = excluded.message_id,
                    updated_at = excluded.updated_at
                """,
                (survey_id, telegram_id, chat_id, message_id, now),
            )
            conn.execute(
                """
                INSERT INTO survey_message_history (
                    survey_id,
                    telegram_id,
                    chat_id,
                    message_id,
                    kind,
                    active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(survey_id, telegram_id, chat_id, message_id)
                DO UPDATE SET
                    kind = excluded.kind,
                    active = 1,
                    updated_at = excluded.updated_at,
                    closed_at = NULL
                """,
                (
                    survey_id,
                    int(telegram_id),
                    int(chat_id),
                    int(message_id),
                    kind,
                    now,
                    now,
                ),
            )

    def get_survey_message(self, survey_id: str, telegram_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT chat_id, message_id
                FROM survey_messages
                WHERE survey_id = ? AND telegram_id = ?
                """,
                (survey_id, telegram_id),
            ).fetchone()
        if row is None:
            return None
        return {"chat_id": int(row[0]), "message_id": int(row[1])}

    def list_survey_messages(self, survey_id: str) -> dict[int, dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT telegram_id, chat_id, message_id, updated_at
                FROM survey_messages
                WHERE survey_id = ?
                """,
                (survey_id,),
            ).fetchall()
        return {
            int(row[0]): {
                "chat_id": int(row[1]),
                "message_id": int(row[2]),
                "updated_at": row[3],
            }
            for row in rows
        }

    def list_active_survey_messages(
        self,
        survey_id: str,
        telegram_id: int | None = None,
        *,
        kind: str | None = None,
    ) -> list[dict]:
        clauses = ["survey_id = ?", "active = 1"]
        params: list = [survey_id]
        if telegram_id is not None:
            clauses.append("telegram_id = ?")
            params.append(int(telegram_id))
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, survey_id, telegram_id, chat_id, message_id, kind, active, created_at, updated_at, closed_at
                FROM survey_message_history
                WHERE {where}
                ORDER BY id ASC
                """,
                params,
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "survey_id": row[1],
                "telegram_id": int(row[2]),
                "chat_id": int(row[3]),
                "message_id": int(row[4]),
                "kind": row[5],
                "active": bool(row[6]),
                "created_at": row[7],
                "updated_at": row[8],
                "closed_at": row[9],
            }
            for row in rows
        ]

    def mark_survey_messages_inactive(
        self,
        survey_id: str,
        telegram_id: int | None = None,
        *,
        chat_id: int | None = None,
        message_id: int | None = None,
        kind: str | None = None,
    ) -> int:
        clauses = ["survey_id = ?", "active = 1"]
        params: list = [survey_id]
        if telegram_id is not None:
            clauses.append("telegram_id = ?")
            params.append(int(telegram_id))
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(int(chat_id))
        if message_id is not None:
            clauses.append("message_id = ?")
            params.append(int(message_id))
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE survey_message_history
                SET active = 0,
                    closed_at = ?,
                    updated_at = ?
                WHERE {where}
                """,
                [now, now, *params],
            )
            return int(cursor.rowcount or 0)

    def list_survey_response_details(self, survey_id: str) -> dict[int, dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    telegram_id,
                    name,
                    unavailable_days,
                    unavailable_weekdays,
                    confirmed,
                    availability_mode,
                    updated_at
                FROM survey_responses
                WHERE survey_id = ?
                """,
                (survey_id,),
            ).fetchall()
        return {
            int(row[0]): {
                "telegram_id": int(row[0]),
                "name": row[1],
                "unavailable_days": json.loads(row[2]),
                "unavailable_weekdays": json.loads(row[3]),
                "confirmed": bool(row[4]),
                "mode": row[5],
                "updated_at": row[6],
            }
            for row in rows
        }

    def upsert_monthly_run(
        self,
        calendar_type: str,
        year: int,
        month: int,
        status: str,
        **fields,
    ) -> None:
        existing = self.get_monthly_run(calendar_type, year, month) or {}
        merged = {**existing, **fields, "status": status}

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_monthly_runs (
                    survey_id,
                    calendar_type,
                    year,
                    month,
                    status,
                    requested_at,
                    preview_sent_at,
                    approved_at,
                    group_sent_at,
                    image_path,
                    schedule_json,
                    stats_json,
                    review_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_type, year, month)
                DO UPDATE SET
                    survey_id = excluded.survey_id,
                    status = excluded.status,
                    requested_at = excluded.requested_at,
                    preview_sent_at = excluded.preview_sent_at,
                    approved_at = excluded.approved_at,
                    group_sent_at = excluded.group_sent_at,
                    image_path = excluded.image_path,
                    schedule_json = excluded.schedule_json,
                    stats_json = excluded.stats_json,
                    review_json = excluded.review_json
                """,
                (
                    merged.get("survey_id"),
                    calendar_type,
                    year,
                    month,
                    status,
                    merged.get("requested_at"),
                    merged.get("preview_sent_at"),
                    merged.get("approved_at"),
                    merged.get("group_sent_at"),
                    merged.get("image_path"),
                    merged.get("schedule_json"),
                    merged.get("stats_json"),
                    merged.get("review_json"),
                ),
            )

    def get_monthly_run(
        self,
        calendar_type: str,
        year: int,
        month: int,
    ) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    survey_id,
                    status,
                    requested_at,
                    preview_sent_at,
                    approved_at,
                    group_sent_at,
                    image_path,
                    schedule_json,
                    stats_json,
                    review_json
                FROM bot_monthly_runs
                WHERE calendar_type = ? AND year = ? AND month = ?
                """,
                (calendar_type, year, month),
            ).fetchone()

        if row is None:
            return None
        return {
            "survey_id": row[0],
            "status": row[1],
            "requested_at": row[2],
            "preview_sent_at": row[3],
            "approved_at": row[4],
            "group_sent_at": row[5],
            "image_path": row[6],
            "schedule_json": row[7],
            "stats_json": row[8],
            "review_json": row[9],
        }

    def delete_monthly_run(self, calendar_type: str, year: int, month: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM bot_monthly_runs
                WHERE calendar_type = ? AND year = ? AND month = ?
                """,
                (calendar_type, year, month),
            )

    def set_schedule_entry_people(
        self,
        work_date: str,
        main: str | None = None,
        backup: str | None = None,
    ) -> bool:
        if main is None and backup is None:
            return False
        source = self.get_active_schedule_source()
        with self.connect() as conn:
            if source is None:
                return False
            year, month = source
            cursor = conn.execute(
                """
                UPDATE schedule_entries
                SET main = COALESCE(?, main),
                    backup = COALESCE(?, backup)
                WHERE work_date = ? AND year = ? AND month = ?
                """,
                (main, backup, work_date, year, month),
            )
        return cursor.rowcount == 1

    def upsert_user(
        self,
        *,
        username: str,
        display_name: str,
        role: str,
        access_level: str,
        active: bool = True,
        participates_in_schedule: bool | None = None,
        telegram_id: int | None = None,
        unavailable_days: list[int] | None = None,
        unavailable_weekdays: list[str] | None = None,
    ) -> None:
        username = normalize_username(username)
        if not username:
            raise ValueError("username is required")
        if access_level not in {"member", "admin"}:
            raise ValueError("access_level must be 'member' or 'admin'")
        display_name = display_name_or_username(display_name, username)

        existing = self.get_user_by_username(username) or {}
        now = utc_now()
        merged_telegram_id = telegram_id if telegram_id is not None else existing.get("telegram_id")
        if participates_in_schedule is None:
            participates_in_schedule = bool(existing.get("participates_in_schedule", True))
        registered_at = existing.get("registered_at")
        if merged_telegram_id and not registered_at:
            registered_at = now

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_users (
                    username,
                    telegram_id,
                    display_name,
                    role,
                    access_level,
                    participates_in_schedule,
                    active,
                    unavailable_days,
                    unavailable_weekdays,
                    registered_at,
                    last_seen_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username)
                DO UPDATE SET
                    telegram_id = excluded.telegram_id,
                    display_name = excluded.display_name,
                    role = excluded.role,
                    access_level = excluded.access_level,
                    participates_in_schedule = excluded.participates_in_schedule,
                    active = excluded.active,
                    unavailable_days = excluded.unavailable_days,
                    unavailable_weekdays = excluded.unavailable_weekdays,
                    registered_at = COALESCE(bot_users.registered_at, excluded.registered_at),
                    last_seen_at = COALESCE(excluded.last_seen_at, bot_users.last_seen_at),
                    updated_at = excluded.updated_at
                """,
                (
                    username,
                    merged_telegram_id,
                    display_name,
                    role,
                    access_level,
                    1 if participates_in_schedule else 0,
                    1 if active else 0,
                    json.dumps(sorted(unavailable_days or existing.get("unavailable_days", []))),
                    json.dumps(
                        sorted(unavailable_weekdays or existing.get("unavailable_weekdays", []))
                    ),
                    registered_at,
                    existing.get("last_seen_at"),
                    existing.get("created_at") or now,
                    now,
                ),
            )

    def get_user_by_username(self, username: str) -> dict | None:
        username = normalize_username(username)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    username,
                    telegram_id,
                    display_name,
                    role,
                    access_level,
                    participates_in_schedule,
                    active,
                    unavailable_days,
                    unavailable_weekdays,
                    registered_at,
                    last_seen_at,
                    created_at,
                    updated_at
                FROM bot_users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()
        return self.user_row_to_dict(row) if row else None

    def get_user_by_telegram_id(self, telegram_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    username,
                    telegram_id,
                    display_name,
                    role,
                    access_level,
                    participates_in_schedule,
                    active,
                    unavailable_days,
                    unavailable_weekdays,
                    registered_at,
                    last_seen_at,
                    created_at,
                    updated_at
                FROM bot_users
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            ).fetchone()
        return self.user_row_to_dict(row) if row else None

    def deactivate_user(self, username: str) -> bool:
        username = normalize_username(username)
        if not username:
            raise ValueError("username is required")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE bot_users
                SET active = 0, updated_at = ?
                WHERE username = ?
                """,
                (utc_now(), username),
            )
        return cursor.rowcount == 1

    def delete_user(self, username: str) -> bool:
        username = normalize_username(username)
        if not username:
            raise ValueError("username is required")
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM bot_users WHERE username = ?",
                (username,),
            )
        return cursor.rowcount == 1

    def authorize_user_from_start(
        self,
        username: str,
        telegram_id: int,
        display_name: str = "",
    ) -> dict | None:
        """Bind a Telegram id to an allowlisted username once.

        Username is only an invite key for the first bind. Rebinding an already
        owned telegram_id (or claiming a username already bound to another id)
        is rejected — username alone must not transfer privileged identity.
        """
        username = normalize_username(username)
        telegram_id = int(telegram_id)
        if telegram_id <= 0:
            return None
        user = self.get_user_by_username(username)
        if user is None or not user["active"]:
            return None

        existing_bound_id = user.get("telegram_id")
        if existing_bound_id is not None and int(existing_bound_id) != telegram_id:
            return None

        other = self.get_user_by_telegram_id(telegram_id)
        if other is not None and normalize_username(other["username"]) != username:
            return None

        now = utc_now()
        display_name = display_name_or_username(
            display_name,
            username,
        ) or display_name_or_username(user["display_name"], user["username"])
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE bot_users
                SET telegram_id = ?,
                    display_name = ?,
                    registered_at = COALESCE(registered_at, ?),
                    last_seen_at = ?,
                    updated_at = ?
                WHERE username = ?
                  AND (telegram_id IS NULL OR telegram_id = ?)
                """,
                (telegram_id, display_name, now, now, now, username, telegram_id),
            )
        self.rebind_open_survey_identity(username, telegram_id)
        return self.get_user_by_telegram_id(telegram_id)

    def rebind_open_survey_identity(self, username: str, telegram_id: int) -> None:
        """Move open-survey roster/response rows onto a newly bound Telegram id.

        Pre-registered users may join surveys before first /start, using a
        stable synthetic participant id. After bind, those rows must follow.
        """
        username = normalize_username(username)
        telegram_id = int(telegram_id)
        if not username or telegram_id <= 0:
            return

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT sp.survey_id, sp.telegram_id
                FROM survey_participants sp
                JOIN surveys s ON s.id = sp.survey_id
                WHERE sp.username = ?
                  AND sp.telegram_id != ?
                  AND s.status NOT IN ('approved', 'canceled')
                """,
                (username, telegram_id),
            ).fetchall()
            for survey_id, old_id in rows:
                old_id = int(old_id)
                already = conn.execute(
                    """
                    SELECT 1 FROM survey_participants
                    WHERE survey_id = ? AND telegram_id = ?
                    """,
                    (survey_id, telegram_id),
                ).fetchone()
                if already:
                    conn.execute(
                        """
                        DELETE FROM survey_participants
                        WHERE survey_id = ? AND telegram_id = ?
                        """,
                        (survey_id, old_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE survey_participants
                        SET telegram_id = ?
                        WHERE survey_id = ? AND telegram_id = ?
                        """,
                        (telegram_id, survey_id, old_id),
                    )

                for table in ("survey_responses", "survey_messages"):
                    target = conn.execute(
                        f"""
                        SELECT 1 FROM {table}
                        WHERE survey_id = ? AND telegram_id = ?
                        """,
                        (survey_id, telegram_id),
                    ).fetchone()
                    if target:
                        conn.execute(
                            f"""
                            DELETE FROM {table}
                            WHERE survey_id = ? AND telegram_id = ?
                            """,
                            (survey_id, old_id),
                        )
                    else:
                        conn.execute(
                            f"""
                            UPDATE {table}
                            SET telegram_id = ?
                            WHERE survey_id = ? AND telegram_id = ?
                            """,
                            (telegram_id, survey_id, old_id),
                        )

    def mark_user_seen(
        self,
        telegram_id: int,
        display_name: str = "",
    ) -> dict | None:
        user = self.get_user_by_telegram_id(telegram_id)
        if user is None:
            return None

        now = utc_now()
        display_name = display_name_or_username(
            display_name,
            user["username"],
        ) or display_name_or_username(user["display_name"], user["username"])
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE bot_users
                SET display_name = ?,
                    last_seen_at = ?,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (display_name, now, now, telegram_id),
            )
        return self.get_user_by_telegram_id(telegram_id)

    def list_members(self, *, active_only: bool = True) -> list[Member]:
        with self.connect() as conn:
            where = "WHERE active = 1" if active_only else ""
            rows = conn.execute(
                f"""
                SELECT
                    username,
                    telegram_id,
                    display_name,
                    role,
                    access_level,
                    participates_in_schedule,
                    active,
                    unavailable_days,
                    unavailable_weekdays,
                    registered_at,
                    last_seen_at,
                    created_at,
                    updated_at
                FROM bot_users
                {where}
                ORDER BY display_name, username
                """
            ).fetchall()
        return [self.user_to_member(self.user_row_to_dict(row)) for row in rows]

    def list_schedule_members(self, *, active_only: bool = True) -> list[Member]:
        return [
            member
            for member in self.list_members(active_only=active_only)
            if member.participates_in_schedule
        ]

    def user_row_to_dict(self, row: sqlite3.Row | tuple) -> dict:
        return {
            "username": row[0],
            "telegram_id": row[1],
            "display_name": row[2],
            "role": row[3],
            "access_level": row[4],
            "participates_in_schedule": bool(row[5]),
            "active": bool(row[6]),
            "unavailable_days": json.loads(row[7] or "[]"),
            "unavailable_weekdays": json.loads(row[8] or "[]"),
            "registered_at": row[9],
            "last_seen_at": row[10],
            "created_at": row[11],
            "updated_at": row[12],
        }

    def user_to_member(self, user: dict) -> Member:
        telegram_id = int(user["telegram_id"] or 0)
        member = Member(
            name=display_name_or_username(user["display_name"], user["username"]),
            telegram_id=telegram_id,
            username=user["username"],
            role=user["role"],
            active=bool(user["active"]),
            unavailable_days=[int(day) for day in user.get("unavailable_days", [])],
            unavailable_weekdays=[
                str(weekday).strip().lower()
                for weekday in user.get("unavailable_weekdays", [])
                if str(weekday).strip()
            ],
            access_level=user["access_level"],
            participates_in_schedule=bool(user.get("participates_in_schedule", True)),
        )
        if telegram_id <= 0 and normalize_username(member.username):
            return Member(
                name=member.name,
                telegram_id=stable_participant_id(member),
                username=member.username,
                role=member.role,
                active=member.active,
                unavailable_days=list(member.unavailable_days),
                unavailable_weekdays=list(member.unavailable_weekdays),
                access_level=member.access_level,
                participates_in_schedule=member.participates_in_schedule,
            )
        return member
