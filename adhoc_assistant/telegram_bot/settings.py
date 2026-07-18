"""Infra-only bootstrap for the Telegram bot. Domain policy lives in the DB."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from adhoc_assistant.constants import DEFAULT_DB_PATH
from adhoc_assistant.telegram_bot.messages import BotMessages


@dataclass(frozen=True)
class Member:
    name: str
    telegram_id: int
    username: str
    role: str
    active: bool
    unavailable_days: list[int] = field(default_factory=list)
    unavailable_weekdays: list[str] = field(default_factory=list)
    access_level: str = "member"
    participates_in_schedule: bool = True

# Keys that must exist in DB runtime_settings before the bot can run.
RUNTIME_REQUIRED_KEYS = (
    "timezone",
    "calendar",
    "survey_days_before_month",
    "survey_start_at",
    "survey_collect_for",
    "revision_collect_for",
    "daily_reminder_time",
    "poll_interval_seconds",
    "bot_name",
    "bot_username",
    "bot_id",
    "telegram_token",
    "output_dir",
    "holidays",
)


@dataclass(frozen=True)
class RuntimeSettings:
    """DB-backed bot domain policy. No live fallbacks outside the DB blob."""

    timezone: str
    calendar: str
    survey_days_before_month: int
    survey_start_at: str
    survey_collect_for: str
    revision_collect_for: str
    target_year: int | None
    target_month: int | None
    daily_reminder_time: str
    poll_interval_seconds: int
    bot_name: str
    bot_username: str
    bot_id: int
    telegram_token: str
    output_dir: str
    holidays: list
    allow_production_destination: bool = False
    bot_messages: BotMessages = field(
        default_factory=lambda: BotMessages.resolve(None, "gregorian")
    )

    def as_dict(self) -> dict:
        return {
            "timezone": self.timezone,
            "calendar": self.calendar,
            "survey_days_before_month": self.survey_days_before_month,
            "survey_start_at": self.survey_start_at,
            "survey_collect_for": self.survey_collect_for,
            "revision_collect_for": self.revision_collect_for,
            "target_year": self.target_year,
            "target_month": self.target_month,
            "daily_reminder_time": self.daily_reminder_time,
            "poll_interval_seconds": self.poll_interval_seconds,
            "bot_name": self.bot_name,
            "bot_username": self.bot_username,
            "bot_id": self.bot_id,
            "telegram_token": self.telegram_token,
            "output_dir": self.output_dir,
            "holidays": list(self.holidays),
            "allow_production_destination": bool(self.allow_production_destination),
            "bot_messages": self.bot_messages.as_dict(),
        }

    @staticmethod
    def missing_keys(raw: dict | None) -> list[str]:
        raw = raw or {}
        missing = [key for key in RUNTIME_REQUIRED_KEYS if key not in raw]
        if "telegram_token" in raw and not str(raw.get("telegram_token") or "").strip():
            missing.append("telegram_token(empty)")
        if "output_dir" in raw and not str(raw.get("output_dir") or "").strip():
            missing.append("output_dir(empty)")
        return missing

    @classmethod
    def from_dict(cls, raw: dict) -> "RuntimeSettings":
        missing = cls.missing_keys(raw)
        if missing:
            raise RuntimeError(
                "Incomplete runtime_settings in the database. Missing: "
                + ", ".join(missing)
                + ". Set them with local CLI --set-* flags "
                "(see --show-runtime-settings)."
            )
        target_year = raw.get("target_year")
        target_month = raw.get("target_month")
        holidays = raw.get("holidays")
        if holidays is None:
            holidays = []
        if not isinstance(holidays, list):
            raise RuntimeError("runtime_settings.holidays must be a JSON list.")
        calendar = str(raw["calendar"])
        bot_messages_raw = raw.get("bot_messages")
        if bot_messages_raw is not None and not isinstance(bot_messages_raw, dict):
            raise RuntimeError("runtime_settings.bot_messages must be a JSON object.")
        return cls(
            timezone=str(raw["timezone"]),
            calendar=calendar,
            survey_days_before_month=int(raw["survey_days_before_month"]),
            survey_start_at=str(raw["survey_start_at"]).strip(),
            survey_collect_for=str(raw["survey_collect_for"]).strip(),
            revision_collect_for=str(raw["revision_collect_for"]).strip(),
            target_year=int(target_year) if target_year not in (None, "", 0) else None,
            target_month=int(target_month) if target_month not in (None, "", 0) else None,
            daily_reminder_time=str(raw["daily_reminder_time"]),
            poll_interval_seconds=int(raw["poll_interval_seconds"]),
            bot_name=str(raw["bot_name"]),
            bot_username=str(raw["bot_username"]),
            bot_id=int(raw["bot_id"]),
            telegram_token=str(raw["telegram_token"]).strip(),
            output_dir=str(raw["output_dir"]).strip(),
            holidays=list(holidays),
            allow_production_destination=bool(raw.get("allow_production_destination", False)),
            bot_messages=BotMessages.resolve(bot_messages_raw, calendar),
        )


@dataclass(frozen=True)
class InfraSettings:
    """DevOps-only bootstrap. Domain policy is never stored here."""

    database_path: Path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_database_path(*, cli_path: str | None = None) -> Path:
    """Resolve the SQLite path from CLI, DATABASE_URL, or the project-root default.

    Only ``sqlite:///`` / ``sqlite://`` DATABASE_URL values are supported today.
    PostgreSQL URLs are rejected explicitly; a Postgres backend is not implemented.
    """
    if cli_path:
        return Path(cli_path)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        if database_url.startswith("sqlite:///"):
            return Path(database_url.removeprefix("sqlite:///"))
        if database_url.startswith("sqlite://"):
            return Path(database_url.removeprefix("sqlite://"))
        parsed = urlparse(database_url)
        if parsed.scheme in {"postgres", "postgresql"}:
            raise RuntimeError(
                "DATABASE_URL postgres/postgresql is not supported yet; "
                "the Telegram bot storage layer is SQLite-only. "
                "Use sqlite:///path/to.db or --database."
            )
        raise RuntimeError(
            f"Unsupported DATABASE_URL scheme: {parsed.scheme or '(empty)'}. "
            "Supported today: sqlite:/// for local SQLite only."
        )

    return Path(DEFAULT_DB_PATH)


def load_infra_settings(*, cli_database: str | None = None) -> InfraSettings:
    load_env_file(Path(".env"))
    return InfraSettings(database_path=resolve_database_path(cli_path=cli_database))
