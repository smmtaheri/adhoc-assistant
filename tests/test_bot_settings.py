import os
import tempfile
import unittest
from pathlib import Path

from adhoc_assistant.telegram_bot.settings import load_bot_settings


class BotSettingsTests(unittest.TestCase):
    def test_loads_token_from_env_file_next_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "bot_config.toml"
            config_path.write_text(
                """
[bot]
token_env = "TELEGRAM_BOT_TOKEN_FOR_TEST"
""".strip(),
                encoding="utf-8",
            )
            (tmp_path / ".env").write_text(
                "TELEGRAM_BOT_TOKEN_FOR_TEST=token-from-file\n",
                encoding="utf-8",
            )

            old_value = os.environ.pop("TELEGRAM_BOT_TOKEN_FOR_TEST", None)
            try:
                settings = load_bot_settings(config_path)
                self.assertEqual(settings.token, "token-from-file")
            finally:
                os.environ.pop("TELEGRAM_BOT_TOKEN_FOR_TEST", None)
                if old_value is not None:
                    os.environ["TELEGRAM_BOT_TOKEN_FOR_TEST"] = old_value

    def test_user_paths_are_not_loaded_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "bot_config.toml"
            config_path.write_text(
                """
[bot]
token_env = "TELEGRAM_BOT_TOKEN_FOR_TEST"

[paths]
members = "members.toml"
debug_members = "debug_members.toml"
""".strip(),
                encoding="utf-8",
            )

            settings = load_bot_settings(config_path)
            self.assertFalse(hasattr(settings, "members_path"))
            self.assertFalse(hasattr(settings, "debug_members_path"))

    def test_timed_profile_defaults_live_in_toml(self) -> None:
        settings = load_bot_settings(Path("bot_config.timed.toml"))

        self.assertEqual(settings.survey_start_at, "+2m")
        self.assertEqual(settings.survey_collect_for, "+10m")
        self.assertEqual(settings.revision_collect_for, "+2h")
        self.assertEqual(settings.target_year, 1405)
        self.assertEqual(settings.target_month, 5)
        self.assertEqual(settings.database_path, Path("data/timed_adhoc.sqlite3"))
        self.assertEqual(settings.output_dir, Path("data/timed_output"))


if __name__ == "__main__":
    unittest.main()
