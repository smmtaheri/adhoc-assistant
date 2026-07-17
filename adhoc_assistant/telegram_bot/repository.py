import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from adhoc_assistant.storage import init_db as init_schedule_db
from adhoc_assistant.telegram_bot.settings import Member


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


def normalize_username(username: str) -> str:
    return username.strip().lower().lstrip("@")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BotRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

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
    ) -> Survey:
        active = self.get_active_survey()
        if active is not None:
            raise ValueError(
                f"Cannot create survey {survey_id}; survey {active.id} is still {active.status}."
            )

        now = utc_now()
        with self.connect() as conn:
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
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                [
                    (
                        survey_id,
                        int(member.telegram_id or 0),
                        normalize_username(member.username),
                        member.name,
                        member.role,
                        member.access_level,
                        1 if member.participates_in_schedule else 0,
                        1 if member.active else 0,
                        json.dumps(sorted(member.unavailable_days)),
                        json.dumps(sorted(member.unavailable_weekdays)),
                    )
                    for member in participants
                ],
            )
        self.set_state("active_survey_id", {"id": survey_id})
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
        return survey

    def survey_row_to_dataclass(self, row: sqlite3.Row | tuple) -> Survey:
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
        )

    def get_survey(self, survey_id: str) -> Survey | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
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
                    updated_at
                FROM surveys
                WHERE id = ?
                """,
                (survey_id,),
            ).fetchone()
        return self.survey_row_to_dataclass(row) if row else None

    def get_active_survey(self) -> Survey | None:
        state = self.get_state("active_survey_id")
        if state and state.get("id"):
            survey = self.get_survey(str(state["id"]))
            if survey is not None and survey.status not in {"approved", "canceled"}:
                return survey
            self.delete_state("active_survey_id")

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
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
                    updated_at
                FROM surveys
                WHERE status NOT IN ('approved', 'canceled')
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        survey = self.survey_row_to_dataclass(row)
        self.set_state("active_survey_id", {"id": survey.id})
        return survey

    def get_latest_survey_for_month(
        self,
        calendar_type: str,
        year: int,
        month: int,
    ) -> Survey | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
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
                    updated_at
                FROM surveys
                WHERE calendar_type = ? AND year = ? AND month = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (calendar_type, year, month),
            ).fetchone()
        return self.survey_row_to_dataclass(row) if row else None

    def list_surveys(self) -> list[Survey]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
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
                    updated_at
                FROM surveys
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [self.survey_row_to_dataclass(row) for row in rows]

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
        if updated and updated.status in {"approved", "canceled"}:
            state = self.get_state("active_survey_id")
            if state and state.get("id") == survey_id:
                self.delete_state("active_survey_id")
        elif updated:
            self.set_state("active_survey_id", {"id": survey_id})
        if updated:
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
            if updated and updated.status in {"approved", "canceled"}:
                self.delete_state("active_survey_id")
            elif updated:
                self.set_state("active_survey_id", {"id": survey_id})
        return changed

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

    def get_schedule_entry(self, work_date: str) -> tuple[str, str] | None:
        with self.connect() as conn:
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
        return Member(
            name=user["display_name"],
            telegram_id=int(user["telegram_id"] or 0),
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
