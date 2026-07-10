import os
import tempfile
import unittest
from pathlib import Path

from adhoc_assistant.telegram_bot.settings import load_bot_members, load_bot_settings


class BotSettingsTests(unittest.TestCase):
    def test_loads_token_from_env_file_next_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "bot_config.toml"
            config_path.write_text(
                """
[bot]
token_env = "TELEGRAM_BOT_TOKEN_FOR_TEST"

[telegram]
admin_ids = []
group_chat_id = 0
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

    def test_debug_members_are_loaded_only_in_debug_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "bot_config.toml"
            config_path.write_text(
                """
[bot]
token_env = "TELEGRAM_BOT_TOKEN_FOR_TEST"

[telegram]
admin_ids = []
group_chat_id = 0
""".strip(),
                encoding="utf-8",
            )
            (tmp_path / "members.toml").write_text(
                """
[[members]]
name = "Real"
telegram_id = 1
username = ""
role = "backend"
active = true
""".strip(),
                encoding="utf-8",
            )
            (tmp_path / "debug_members.toml").write_text(
                """
[[members]]
name = "Fake"
telegram_id = 0
username = ""
role = "backend"
active = true
""".strip(),
                encoding="utf-8",
            )

            settings = load_bot_settings(config_path)
            settings = settings.__class__(
                **{
                    **settings.__dict__,
                    "members_path": tmp_path / "members.toml",
                    "debug_members_path": tmp_path / "debug_members.toml",
                }
            )
            self.assertEqual([member.name for member in load_bot_members(settings)], ["Real"])

            debug_settings = settings.__class__(
                **{**settings.__dict__, "debug_enabled": True}
            )
            self.assertEqual(
                [member.name for member in load_bot_members(debug_settings)],
                ["Real", "Fake"],
            )


if __name__ == "__main__":
    unittest.main()
