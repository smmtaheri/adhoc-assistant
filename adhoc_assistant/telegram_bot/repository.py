import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from adhoc_assistant.storage import init_db as init_schedule_db


@dataclass
class AvailabilityResponse:
    telegram_id: int
    name: str
    unavailable_days: list[int]
    unavailable_weekdays: list[str]
    confirmed: bool = False
    mode: str = "custom"


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

    def set_update_offset(self, offset: int) -> None:
        self.set_state("telegram_update_offset", {"offset": offset})

    def get_update_offset(self) -> int | None:
        state = self.get_state("telegram_update_offset")
        if not state:
            return None
        return int(state["offset"])

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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_type, year, month)
                DO UPDATE SET
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
            "status": row[0],
            "requested_at": row[1],
            "preview_sent_at": row[2],
            "approved_at": row[3],
            "group_sent_at": row[4],
            "image_path": row[5],
            "schedule_json": row[6],
            "stats_json": row[7],
            "review_json": row[8],
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
