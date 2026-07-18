import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adhoc_assistant.constants import DEFAULT_DB_PATH
from adhoc_assistant.telegram_bot.repository import BotRepository
from adhoc_assistant.telegram_bot.settings import (
    RuntimeSettings,
    load_infra_settings,
    resolve_database_path,
)


class BotSettingsTests(unittest.TestCase):
    def test_resolve_database_path_prefers_env_over_default(self) -> None:
        with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///from-url.sqlite3"}, clear=False):
            self.assertEqual(resolve_database_path(), Path("from-url.sqlite3"))

        env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_database_path(), Path(DEFAULT_DB_PATH))
            self.assertEqual(
                resolve_database_path(cli_path="from-cli.sqlite3"),
                Path("from-cli.sqlite3"),
            )

    def test_load_infra_settings_is_database_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bot.sqlite3"
            settings = load_infra_settings(cli_database=str(db_path))
            self.assertEqual(settings.database_path, db_path)
            self.assertFalse(hasattr(settings, "token_env"))
            self.assertFalse(hasattr(settings, "seed_runtime"))
            self.assertFalse(hasattr(settings, "schedule_config_path"))

    def test_incomplete_runtime_settings_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = BotRepository(Path(tmp) / "adhoc.sqlite3")
            repo.update_runtime_settings(timezone="Asia/Tehran")
            with self.assertRaises(RuntimeError) as ctx:
                repo.get_runtime_settings()
            self.assertIn("Incomplete runtime_settings", str(ctx.exception))

    def test_compose_does_not_define_implicit_timed_database(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("bot_config", compose)
        self.assertNotIn("--config", compose)
        self.assertNotIn("adhoc-assistant-timed", compose)
        self.assertNotIn("TIMED_DATABASE_URL", compose)
        self.assertNotIn("timed_adhoc.sqlite3", compose)

    def test_postgres_database_url_is_rejected_clearly(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://user:pass@localhost:5432/adhoc"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                resolve_database_path()
            self.assertIn("not supported", str(ctx.exception).lower())
            self.assertIn("sqlite", str(ctx.exception).lower())

    def test_required_runtime_keys_include_token_and_output(self) -> None:
        missing = RuntimeSettings.missing_keys({})
        self.assertIn("telegram_token", missing)
        self.assertIn("output_dir", missing)
        self.assertIn("bot_name", missing)
        self.assertIn("holidays", missing)

    def test_bot_messages_merge_defaults_from_runtime_settings(self) -> None:
        settings = RuntimeSettings.from_dict(
            {
                "timezone": "Asia/Tehran",
                "calendar": "gregorian",
                "survey_days_before_month": 2,
                "survey_start_at": "",
                "survey_collect_for": "",
                "revision_collect_for": "+2h",
                "target_year": None,
                "target_month": None,
                "daily_reminder_time": "09:00",
                "poll_interval_seconds": 1,
                "bot_name": "test_bot",
                "bot_username": "@test_bot",
                "bot_id": 1,
                "telegram_token": "TOKEN",
                "output_dir": "output",
                "holidays": [],
                "bot_messages": {"unauthorized": "NOPE"},
            }
        )
        self.assertEqual(settings.bot_messages.unauthorized, "NOPE")
        self.assertEqual(
            settings.bot_messages.no_active_survey,
            "There is no active availability survey right now.",
        )


if __name__ == "__main__":
    unittest.main()
