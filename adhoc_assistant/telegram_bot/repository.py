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
        return (
            f"Cannot create a new {kind} survey; survey {active.id} "
            f"({active.calendar_type} {active.year}-{active.month:02d}) "
            f"is still {active.status}. Cancel or deactivate it first."
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
    ) -> Survey:
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
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_state = conn.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (active_survey_id_state_key(kind),),
            ).fetchone()
            if active_state:
                payload = json.loads(active_state[0])
                active_id = payload.get("id")
                if active_id:
                    row = conn.execute(
                        "SELECT status FROM surveys WHERE id = ?",
                        (active_id,),
                    ).fetchone()
                    if row and row[0] not in TERMINAL_SURVEY_STATUSES:
                        raise ValueError(
                            f"Cannot create a new {kind} survey; survey {active_id} "
                            f"is still {row[0]}. Cancel or deactivate it first."
                        )
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
        self.set_active_survey_id(survey_id, kind)
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
        self.set_active_survey_phase(survey)
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
        kind = self.normalize_survey_kind(kind)
        self.migrate_legacy_active_survey_state()
        state = self.get_state(active_survey_id_state_key(kind))
        if state and state.get("id"):
            survey = self.get_survey(str(state["id"]))
            if survey is not None and survey.status not in TERMINAL_SURVEY_STATUSES:
                return survey
            self.clear_active_survey_id(kind)

        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT {SURVEY_SELECT_COLUMNS}
                FROM surveys
                WHERE status NOT IN ('approved', 'canceled')
                  AND kind = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (kind,),
            ).fetchone()
        if row is None:
            return None
        survey = self.survey_row_to_dataclass(row)
        self.set_active_survey_id(survey.id, kind)
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
        return [self.survey_row_to_dataclass(row) for row in rows]

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

    def set_active_survey_phase(self, survey: Survey, **extra) -> None:
        payload = {
            "id": survey.id,
            "kind": survey.kind,
            "calendar": survey.calendar_type,
            "year": survey.year,
            "month": survey.month,
            "phase": survey.status,
            "collect_until": survey.closes_at,
            "allowed_member_ids": [],
        }
        payload.update(extra)
        self.set_state(active_survey_phase_state_key(survey.kind), payload)
        self.set_active_survey_id(survey.id, survey.kind)

    def get_active_survey_phase(self, kind: str = SURVEY_KIND_PRODUCTION) -> dict | None:
        kind = self.normalize_survey_kind(kind)
        self.migrate_legacy_active_survey_state()
        return self.get_state(active_survey_phase_state_key(kind))

    def clear_active_survey_phase(self, kind: str) -> None:
        kind = self.normalize_survey_kind(kind)
        self.delete_state(active_survey_phase_state_key(kind))
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
            if updated.status in TERMINAL_SURVEY_STATUSES:
                state = self.get_state(active_survey_id_state_key(updated.kind))
                if state and state.get("id") == survey_id:
                    self.clear_active_survey_id(updated.kind)
                    self.clear_active_survey_phase(updated.kind)
            else:
                self.set_active_survey_id(survey_id, updated.kind)
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
                if updated.status in TERMINAL_SURVEY_STATUSES:
                    state = self.get_state(active_survey_id_state_key(updated.kind))
                    if state and state.get("id") == survey_id:
                        self.clear_active_survey_id(updated.kind)
                        self.clear_active_survey_phase(updated.kind)
                else:
                    self.set_active_survey_id(survey_id, updated.kind)
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

    def claim_daily_reminder(self, work_date: str) -> bool:
        """Atomically claim today's reminder. Returns True only for the winning claim."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO daily_reminders (work_date, sent_at)
                VALUES (?, ?)
                """,
                (work_date, utc_now()),
            )
            return cursor.rowcount == 1

    def mark_daily_sent(self, work_date: str) -> None:
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

    def complete_publish(self, survey_id: str) -> bool:
        survey = self.get_survey(survey_id)
        if survey is None or survey.status != "publishing":
            return False

        if survey.kind == SURVEY_KIND_PRODUCTION:
            schedule = json.loads(survey.schedule_json or "[]")
            plain_stats = stats_to_plain_dict(json.loads(survey.stats_json or "{}"))
        else:
            schedule = []
            plain_stats = {}

        now = utc_now()
        with self.connect() as conn:
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
                conn.execute(
                    """
                    INSERT INTO bot_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (
                        ACTIVE_SCHEDULE_SOURCE_KEY,
                        json.dumps({"year": int(survey.year), "month": int(survey.month)}),
                    ),
                )

            cursor = conn.execute(
                """
                UPDATE surveys
                SET status = ?, approved_at = ?, group_sent_at = COALESCE(group_sent_at, ?), updated_at = ?
                WHERE id = ? AND status = ?
                """,
                ("approved", now, now, now, survey.id, "publishing"),
            )
            if cursor.rowcount != 1:
                return False

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
        source = self.get_active_schedule_source()
        with self.connect() as conn:
            if source is not None:
                year, month = source
                row = conn.execute(
                    """
                    SELECT main, backup
                    FROM schedule_entries
                    WHERE work_date = ? AND year = ? AND month = ?
                    LIMIT 1
                    """,
                    (work_date, year, month),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT main, backup
                    FROM schedule_entries
                    WHERE work_date = ?
                    ORDER BY year DESC, month DESC
                    LIMIT 1
                    """,
                    (work_date,),
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

    def record_survey_message(
        self,
        survey_id: str,
        telegram_id: int,
        chat_id: int,
        message_id: int,
    ) -> None:
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
                (survey_id, telegram_id, chat_id, message_id, utc_now()),
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
        main: str,
        backup: str,
    ) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE schedule_entries
                SET main = ?, backup = ?
                WHERE work_date = ?
                """,
                (main, backup, work_date),
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

    def authorize_user_from_start(
        self,
        username: str,
        telegram_id: int,
        display_name: str = "",
    ) -> dict | None:
        username = normalize_username(username)
        user = self.get_user_by_username(username)
        if user is None or not user["active"]:
            return None

        now = utc_now()
        display_name = display_name.strip() or user["display_name"]
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
                """,
                (telegram_id, display_name, now, now, now, username),
            )
        return self.get_user_by_username(username)

    def mark_user_seen(
        self,
        telegram_id: int,
        display_name: str = "",
    ) -> dict | None:
        user = self.get_user_by_telegram_id(telegram_id)
        if user is None:
            return None

        now = utc_now()
        display_name = display_name.strip() or user["display_name"]
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
            name=user["display_name"],
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
