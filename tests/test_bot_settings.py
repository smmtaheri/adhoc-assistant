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

        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DATABASE_URL", "SQLITE_PATH"}
        }
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["SQLITE_PATH"] = "from-sqlite-path.sqlite3"
            self.assertEqual(resolve_database_path(), Path("from-sqlite-path.sqlite3"))
            del os.environ["SQLITE_PATH"]
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

    def test_timed_compose_does_not_pass_domain_policy_or_config_files(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("bot_config", compose)
        self.assertNotIn("--config", compose)
        timed_block = compose.split("adhoc-assistant-timed:")[1].split("adhoc-assistant-cli:")[0]
        for flag in (
            "--target-month",
            "--survey-start-at",
            "--survey-collect-for",
            "--revision-collect-for",
            "TELEGRAM_BOT_TOKEN",
        ):
            self.assertNotIn(flag, timed_block)
        self.assertIn("--database", timed_block)
        self.assertNotIn("--reset-target-month", timed_block)

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


if __name__ == "__main__":
    unittest.main()
