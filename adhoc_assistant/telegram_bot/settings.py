import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Member:
    name: str
    telegram_id: int
    username: str
    role: str
    active: bool
    unavailable_days: list[int] = field(default_factory=list)
    unavailable_weekdays: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BotSettings:
    bot_name: str
    bot_username: str
    bot_id: int
    timezone: str
    calendar: str
    admin_telegram_ids: list[int]
    group_chat_id: int
    topic_id: int | None
    survey_days_before_month: int
    survey_start_at: str
    survey_collect_for: str
    revision_collect_for: str
    target_year: int | None
    target_month: int | None
    daily_reminder_time: str
    poll_interval_seconds: int
    database_path: Path
    schedule_config_path: Path
    members_path: Path
    debug_members_path: Path
    output_dir: Path
    token_env: str
    debug_enabled: bool
    debug_auto_preview_on_confirm: bool

    @property
    def token(self) -> str:
        token = os.environ.get(self.token_env, "")
        if not token:
            raise RuntimeError(
                f"Telegram token is missing. Set {self.token_env} in the environment."
            )
        return token


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


def load_bot_settings(path: Path) -> BotSettings:
    load_env_file(Path(".env"))
    load_env_file(path.parent / ".env")

    with path.open("rb") as f:
        raw = tomllib.load(f)

    bot = raw.get("bot", {})
    telegram = raw.get("telegram", {})
    schedule = raw.get("schedule", {})
    debug = raw.get("debug", {})
    paths = raw.get("paths", {})

    topic_id = telegram.get("topic_id")
    target_year = schedule.get("target_year")
    target_month = schedule.get("target_month")
    return BotSettings(
        bot_name=str(bot.get("name", "TODO_BOT_NAME")),
        bot_username=str(bot.get("username", "TODO_BOT_USERNAME")),
        bot_id=int(bot.get("id", 0)),
        timezone=str(bot.get("timezone", "Asia/Tehran")),
        calendar=str(bot.get("calendar", "jalali")),
        admin_telegram_ids=[int(item) for item in telegram.get("admin_ids", [])],
        group_chat_id=int(telegram.get("group_chat_id", 0)),
        topic_id=int(topic_id) if topic_id not in (None, "") else None,
        survey_days_before_month=int(schedule.get("survey_days_before_month", 2)),
        survey_start_at=str(schedule.get("survey_start_at", "")).strip(),
        survey_collect_for=str(schedule.get("survey_collect_for", "")).strip(),
        revision_collect_for=str(schedule.get("revision_collect_for", "+2h")).strip(),
        target_year=int(target_year) if target_year not in (None, "", 0) else None,
        target_month=int(target_month) if target_month not in (None, "", 0) else None,
        daily_reminder_time=str(schedule.get("daily_reminder_time", "09:00")),
        poll_interval_seconds=int(schedule.get("poll_interval_seconds", 10)),
        database_path=Path(paths.get("database", "adhoc_history.sqlite3")),
        schedule_config_path=Path(paths.get("schedule_config", "adhoc_config.toml")),
        members_path=Path(paths.get("members", "members.toml")),
        debug_members_path=Path(paths.get("debug_members", "debug_members.toml")),
        output_dir=Path(paths.get("output_dir", "output")),
        token_env=str(bot.get("token_env", "TELEGRAM_BOT_TOKEN")),
        debug_enabled=bool(debug.get("enabled", False)),
        debug_auto_preview_on_confirm=bool(debug.get("auto_preview_on_confirm", True)),
    )


def load_members(path: Path) -> list[Member]:
    with path.open("rb") as f:
        raw = tomllib.load(f)

    members = []
    for item in raw.get("members", []):
        members.append(
            Member(
                name=str(item["name"]),
                telegram_id=int(item["telegram_id"]),
                username=str(item.get("username", "")),
                role=str(item.get("role", "")),
                active=bool(item.get("active", True)),
                unavailable_days=[int(day) for day in item.get("unavailable_days", [])],
                unavailable_weekdays=[
                    str(weekday).strip().lower()
                    for weekday in item.get("unavailable_weekdays", [])
                    if str(weekday).strip()
                ],
            )
        )
    return members


def load_bot_members(settings: BotSettings) -> list[Member]:
    members = load_members(settings.members_path)
    if settings.debug_enabled and settings.debug_members_path.exists():
        members.extend(load_members(settings.debug_members_path))
    return members
