import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from adhoc_assistant.config import load_config
from adhoc_assistant.scheduler import build_schedule
from adhoc_assistant.telegram_bot.keyboards import dates_keyboard, weekdays_keyboard
from adhoc_assistant.telegram_bot.repository import AvailabilityResponse, BotRepository
from adhoc_assistant.telegram_bot.service import (
    AdhocTelegramBot,
    anchor_scheduled_survey_start,
    build_schedule_config,
    due_survey_month,
    mention,
    parse_scheduled_datetime,
    reset_target_month_state,
)
from adhoc_assistant.telegram_bot.settings import BotSettings, Member
from adhoc_assistant.telegram_bot.telegram import TelegramApiError


class FakeTelegram:
    def __init__(self) -> None:
        self.messages = []
        self.documents = []
        self.photos = []
        self.edits = []
        self.answers = []
        self.updates = []
        self.edit_error = None
        self.answer_error = None
        self.document_error = None
        self.pin_error = None
        self.events = []
        self.pins = []
        self.reply_markup_edits = []

    def send_message(
        self,
        chat_id,
        text,
        reply_markup=None,
        message_thread_id=None,
    ):
        self.events.append(("send_message", chat_id, text))
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
            }
        )
        return {"message_id": len(self.messages)}

    def send_document(
        self,
        chat_id,
        document_path,
        caption="",
        reply_markup=None,
        message_thread_id=None,
    ):
        if self.document_error is not None:
            raise self.document_error
        self.events.append(("send_document", chat_id, str(document_path)))
        self.documents.append(
            {
                "chat_id": chat_id,
                "document_path": Path(document_path),
                "caption": caption,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
            }
        )
        return {"message_id": len(self.documents)}

    def send_photo(
        self,
        chat_id,
        photo_path,
        caption="",
        reply_markup=None,
        message_thread_id=None,
    ):
        if self.document_error is not None:
            raise self.document_error
        self.events.append(("send_photo", chat_id, str(photo_path)))
        self.photos.append(
            {
                "chat_id": chat_id,
                "photo_path": Path(photo_path),
                "caption": caption,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
            }
        )
        return {"message_id": len(self.photos)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.events.append(("edit_message_text", chat_id, message_id))
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": message_id}

    def answer_callback_query(self, callback_query_id, text="", show_alert=False):
        if self.answer_error is not None:
            raise self.answer_error
        self.events.append(("answer_callback_query", callback_query_id, text, show_alert))
        self.answers.append({"id": callback_query_id, "text": text, "show_alert": show_alert})
        return {"ok": True}

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.events.append(("edit_message_reply_markup", chat_id, message_id))
        self.reply_markup_edits.append(
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
        )
        return {"message_id": message_id}

    def pin_chat_message(
        self,
        chat_id,
        message_id,
        disable_notification=True,
        message_thread_id=None,
    ):
        if self.pin_error is not None:
            raise self.pin_error
        self.events.append(("pin_chat_message", chat_id, message_id, message_thread_id))
        self.pins.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": disable_notification,
                "message_thread_id": message_thread_id,
            }
        )
        return {"ok": True}

    def get_updates(self, offset=None, timeout=30):
        return self.updates


def members() -> list[Member]:
    return [
        Member("Ali", 1, "ali_user", "backend", True),
        Member("Sara", 2, "sara_user", "frontend", True),
        Member("Former", 3, "former_user", "backend", False),
        Member("Admin", 99, "admin_user", "manager", True, access_level="admin", participates_in_schedule=False),
    ]


def settings(
    tmp_path: Path,
    *,
    debug_enabled: bool = False,
    survey_start_at: str = "",
    target_year: int | None = None,
    target_month: int | None = None,
) -> BotSettings:
    return BotSettings(
        bot_name="TODO_BOT_NAME",
        bot_username="TODO_BOT_USERNAME",
        bot_id=0,
        timezone="Asia/Tehran",
        calendar="gregorian",
        survey_days_before_month=2,
        survey_start_at=survey_start_at,
        survey_collect_for="",
        revision_collect_for="+2h",
        target_year=target_year,
        target_month=target_month,
        daily_reminder_time="09:00",
        poll_interval_seconds=1,
        database_path=tmp_path / "adhoc.sqlite3",
        schedule_config_path=tmp_path / "adhoc_config.toml",
        output_dir=tmp_path / "output",
        token_env="TELEGRAM_BOT_TOKEN",
        debug_enabled=debug_enabled,
        debug_auto_preview_on_confirm=True,
    )


def write_schedule_config(path: Path) -> None:
    path.write_text(
        """
[date]
calendar = "gregorian"
year = 2026
month = 8

[[people]]
name = "Ali"
role = "backend"

[[people]]
name = "Sara"
role = "frontend"
""".strip(),
        encoding="utf-8",
    )


def active_survey_id(bot: AdhocTelegramBot) -> str:
    survey = bot.active_survey_record()
    assert survey is not None
    return survey.id


def av(bot: AdhocTelegramBot, action: str) -> str:
    return f"av:{active_survey_id(bot)}:{action}"


def admin(bot: AdhocTelegramBot, action: str) -> str:
    return f"admin:{action}:{active_survey_id(bot)}"


class TelegramBotBusinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image_exporter_patch = mock.patch(
            "adhoc_assistant.telegram_bot.service.export_image_calendar",
            side_effect=self.fake_export_image_calendar,
        )
        self.image_exporter_patch.start()

    def tearDown(self) -> None:
        self.image_exporter_patch.stop()

    def fake_export_image_calendar(
        self,
        schedule,
        year,
        month,
        calendar_type,
        output_path,
        stats=None,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake jpg")

    def test_due_survey_month_only_before_next_month(self) -> None:
        self.assertIsNone(due_survey_month(date(2026, 7, 29), "gregorian", 2))
        self.assertEqual(
            due_survey_month(date(2026, 7, 30), "gregorian", 2),
            (2026, 8),
        )
        self.assertEqual(
            due_survey_month(date(2026, 7, 31), "gregorian", 2),
            (2026, 8),
        )
        self.assertIsNone(due_survey_month(date(2026, 8, 1), "gregorian", 2))

    def test_parse_scheduled_datetime_accepts_relative_and_exact_values(self) -> None:
        now = datetime(2026, 7, 10, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))

        self.assertEqual(parse_scheduled_datetime("+2m", now), now + timedelta(minutes=2))
        self.assertEqual(parse_scheduled_datetime("2h", now), now + timedelta(hours=2))
        self.assertEqual(parse_scheduled_datetime("+2d", now), now + timedelta(days=2))
        self.assertEqual(
            parse_scheduled_datetime("2026-07-10 10:30", now),
            datetime(2026, 7, 10, 10, 30, tzinfo=ZoneInfo("Asia/Tehran")),
        )

    def test_build_schedule_config_uses_only_active_members_and_specific_dates(self) -> None:
        base_config = {"calendar": "gregorian", "year": 2026, "month": 8, "people": []}
        response = AvailabilityResponse(
            telegram_id=1,
            name="Ali",
            unavailable_days=[1],
            unavailable_weekdays=["sunday"],
            confirmed=True,
        )

        config = build_schedule_config(
            base_config,
            members(),
            {1: response},
            "gregorian",
            2026,
            8,
        )

        self.assertEqual([person["name"] for person in config["people"]], ["Ali", "Sara"])
        self.assertEqual(config["people"][0]["unavailable_dates"], ["2026-08-01"])
        self.assertEqual(config["people"][0]["unavailable_weekdays"], ["sunday"])
        self.assertEqual(config["people"][1]["unavailable_dates"], [])

        schedule, _stats = build_schedule(config)
        first_day = next(item for item in schedule if item["gregorian_date"] == "2026-08-01")
        self.assertNotEqual(first_day["main"], "Ali")
        self.assertNotEqual(first_day["backup"], "Ali")

    def test_custom_range_config_builds_only_that_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "range.toml"
            config_path.write_text(
                """
[date]
calendar = "gregorian"
start_date = "2026-08-03"
end_date = "2026-08-06"

[[people]]
name = "Ali"
role = "backend"

[[people]]
name = "Sara"
role = "frontend"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)
            schedule, _stats = build_schedule(config)

            self.assertEqual(
                [item["gregorian_date"] for item in schedule],
                ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"],
            )

    def test_local_members_use_configured_availability_and_do_not_block_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_members = [
                Member("Ali", 1, "ali_user", "backend", True),
                Member(
                    "Local",
                    0,
                    "",
                    "backend",
                    True,
                    unavailable_days=[3],
                    unavailable_weekdays=["monday"],
                ),
            ]
            base_config = {"calendar": "gregorian", "year": 2026, "month": 8, "people": []}

            config = build_schedule_config(
                base_config,
                local_members,
                {},
                "gregorian",
                2026,
                8,
            )

            local_config = next(person for person in config["people"] if person["name"] == "Local")
            self.assertEqual(local_config["unavailable_dates"], ["2026-08-03"])
            self.assertEqual(local_config["unavailable_weekdays"], ["monday"])

            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, local_members, repo, fake)
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], [], True),
            )

            self.assertTrue(bot.all_members_responded(2026, 8))

    def test_keyboards_are_inline_only_and_skip_fridays(self) -> None:
        response = AvailabilityResponse(1, "Ali", [], [], False)

        weekday_markup = weekdays_keyboard(response, "jalali")
        self.assertEqual(set(weekday_markup), {"inline_keyboard"})

        date_markup = dates_keyboard(response, 2026, 8, "gregorian")
        self.assertEqual(set(date_markup), {"inline_keyboard"})
        buttons = [
            button
            for row in date_markup["inline_keyboard"]
            for button in row
        ]
        self.assertTrue(all("callback_data" in button for button in buttons))

        visible_texts = {button["text"].strip() for button in buttons}
        self.assertNotIn("7", visible_texts)
        self.assertNotIn("14", visible_texts)
        self.assertNotIn("21", visible_texts)
        self.assertNotIn("28", visible_texts)

    def test_mention_accepts_usernames_with_or_without_at_sign(self) -> None:
        self.assertEqual(
            mention(Member("Ali", 1, "ali_user", "backend", True)),
            "@ali_user",
        )
        self.assertEqual(
            mention(Member("Ali", 1, "@ali_user", "backend", True)),
            "@ali_user",
        )

    def test_collection_starts_only_in_due_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.ensure_survey_started(date(2026, 7, 29))
            self.assertEqual(fake.messages, [])
            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))

            bot.ensure_survey_started(date(2026, 7, 30))
            self.assertEqual(len(fake.messages), 2)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting")
            self.assertEqual(len(repo.list_responses("gregorian", 2026, 8)), 2)

    def test_start_opens_availability_form_for_member_in_debug_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, debug_enabled=True)
            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            debug_members = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, debug_members, repo, fake)
            bot.target_month = lambda today=None: (2026, 8)

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting")
            state = repo.get_state("active_survey")
            self.assertTrue(state["id"].startswith("S-gregorian-2026-08-"))
            self.assertEqual(
                {key: state[key] for key in ("calendar", "year", "month", "phase", "collect_until", "allowed_member_ids")},
                {
                    "calendar": "gregorian",
                    "year": 2026,
                    "month": 8,
                    "phase": "collecting",
                    "collect_until": "",
                    "allowed_member_ids": [],
                },
            )
            self.assertEqual(len(repo.list_responses("gregorian", 2026, 8)), 1)
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertEqual(set(fake.messages[-1]["reply_markup"]), {"inline_keyboard"})

    def test_debug_mode_rejects_non_admin_members_with_inactive_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, debug_enabled=True)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            bot.target_month = lambda today=None: (2026, 8)

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertEqual(fake.messages[-1]["text"], "The bot is currently inactive.")
            self.assertIsNone(repo.get_state("active_survey"))

    def test_debug_admin_start_resets_month_and_confirm_sends_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, debug_enabled=True, target_year=2026, target_month=8)
            write_schedule_config(bot_settings.schedule_config_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            debug_members = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, debug_members, repo, fake)
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [5], ["sunday"], True),
            )
            repo.upsert_monthly_run("gregorian", 2026, 8, "pending_admin_review")

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            response = repo.get_response("gregorian", 2026, 8, 1)
            self.assertEqual(response.unavailable_days, [])
            self.assertEqual(response.unavailable_weekdays, [])
            self.assertFalse(response.confirmed)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting")

            bot.handle_availability_callback(
                {
                    "id": "confirm-1",
                    "from": {"id": 1},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(len(fake.photos), 1)
            self.assertEqual(fake.photos[0]["chat_id"], 1)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "pending_admin_review")

    def test_start_does_not_create_survey_in_normal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            bot.target_month = lambda today=None: (2026, 8)

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))
            self.assertEqual(fake.messages[-1]["text"], "There is no active availability survey right now.")

    def test_start_authorizes_allowed_username_and_stores_telegram_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            repo.upsert_user(
                username="new_user",
                display_name="New User",
                role="backend",
                access_level="member",
            )
            repo.set_state(
                "active_survey",
                {
                    "calendar": "gregorian",
                    "year": 2026,
                    "month": 8,
                    "phase": "collecting",
                    "collect_until": "",
                    "allowed_member_ids": [],
                },
            )
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, [], repo, fake)

            bot.handle_update(
                {
                    "message": {
                        "from": {
                            "id": 777,
                            "username": "new_user",
                            "first_name": "Telegram",
                            "last_name": "Name",
                        },
                        "chat": {"id": 777, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            user = repo.get_user_by_username("new_user")
            self.assertEqual(user["telegram_id"], 777)
            self.assertEqual(user["display_name"], "Telegram")
            self.assertEqual(bot.members[0].name, "Telegram")
            self.assertEqual(fake.messages[-1]["chat_id"], 777)
            self.assertEqual(set(fake.messages[-1]["reply_markup"]), {"inline_keyboard"})

    def test_unknown_private_user_is_rejected_before_any_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, debug_enabled=True)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 404, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            self.assertEqual(fake.messages[-1]["chat_id"], 404)
            self.assertEqual(fake.messages[-1]["text"], "The bot is currently inactive.")
            self.assertIsNone(repo.get_state("active_survey"))

    def test_unknown_callback_user_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})

            bot.handle_availability_callback(
                {
                    "id": "unknown-1",
                    "from": {"id": 404},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 404}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers[-1]["id"], "unknown-1")
            self.assertEqual(fake.answers[-1]["text"], "You are not allowed to use this bot.")
            self.assertEqual(fake.edits, [])

    def test_debug_mode_rejects_non_admin_callback_with_inactive_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, debug_enabled=True)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})

            bot.handle_availability_callback(
                {
                    "id": "debug-member-1",
                    "from": {"id": 1},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers[-1]["id"], "debug-member-1")
            self.assertEqual(fake.answers[-1]["text"], "The bot is currently inactive.")
            self.assertTrue(fake.answers[-1]["show_alert"])
            self.assertEqual(fake.edits, [])

    def test_today_command_returns_main_and_backup_with_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            with sqlite3.connect(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2026, 8, "2026-08-01", "Saturday", "", "Ali", "Sara"),
                )

            bot.send_today_bug_day(1, datetime(2026, 8, 1, 12, 0))

            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertIn("Bug day: Ali (1)", fake.messages[-1]["text"])
            self.assertIn("Helper: Sara (2)", fake.messages[-1]["text"])

    def test_admin_can_use_today_command_without_being_a_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            with sqlite3.connect(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2026, 8, "2026-08-01", "Saturday", "", "Ali", "Sara"),
                )

            bot.handle_private_text(99, "/today")
            bot.send_today_bug_day(99, datetime(2026, 8, 1, 12, 0))

            self.assertEqual(fake.messages[-1]["chat_id"], 99)
            self.assertIn("Bug day: Ali (1)", fake.messages[-1]["text"])

    def test_scheduled_start_at_starts_collection_after_configured_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(
                tmp_path,
                survey_start_at="2026-07-10 09:01",
                target_year=2026,
                target_month=8,
            )
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            before = datetime(2026, 7, 10, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
            after = datetime(2026, 7, 10, 9, 2, tzinfo=ZoneInfo("Asia/Tehran"))

            self.assertIsNone(bot.scheduled_survey_due(before))
            self.assertEqual(bot.scheduled_survey_due(after), (2026, 8))

            bot.ensure_survey_started(after)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting")

    def test_run_once_passes_current_datetime_to_ensure_survey_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(
                tmp_path,
                survey_start_at="+2m",
                target_year=2026,
                target_month=8,
            )
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            captured: list[date | datetime | None] = []
            original = bot.ensure_survey_started

            def spy(today: date | datetime | None = None) -> None:
                captured.append(today)
                original(today)

            bot.ensure_survey_started = spy
            bot.run_once()

            self.assertEqual(len(captured), 1)
            self.assertIsInstance(captured[0], datetime)

    def test_relative_timed_flow_survey_then_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(
                tmp_path,
                survey_start_at="+2m",
                target_year=2026,
                target_month=8,
            )
            bot_settings = bot_settings.__class__(
                **{**bot_settings.__dict__, "survey_collect_for": "+2m"}
            )
            write_schedule_config(bot_settings.schedule_config_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            tehran = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 7, 10, 9, 0, tzinfo=tehran)
            reset_target_month_state(bot)
            anchor_scheduled_survey_start(bot, start)

            before_survey = start + timedelta(minutes=1)
            survey_time = start + timedelta(minutes=2)
            preview_time = start + timedelta(minutes=4)

            self.assertIsNone(bot.scheduled_survey_due(before_survey))
            self.assertEqual(bot.scheduled_survey_due(survey_time), (2026, 8))

            bot.ensure_survey_started(before_survey)
            self.assertEqual(fake.messages, [])

            bot.ensure_survey_started(survey_time)
            self.assertEqual(len(fake.messages), 2)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting")
            self.assertTrue(bot.collection_deadline_reached(preview_time))

            preview_calls: list[tuple[int, int, str | None]] = []
            bot.create_preview = (
                lambda year, month, survey_id=None: preview_calls.append((year, month, survey_id))
            )
            bot.maybe_create_preview(preview_time)
            self.assertEqual(preview_calls[0][:2], (2026, 8))
            self.assertTrue(preview_calls[0][2].startswith("S-gregorian-2026-08-"))

    def test_relative_survey_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(
                tmp_path,
                survey_start_at="+2m",
                target_year=2026,
                target_month=8,
            )
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            tehran = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 7, 10, 9, 0, tzinfo=tehran)
            anchor_scheduled_survey_start(bot, start)

            bot.ensure_survey_started(start + timedelta(minutes=1))
            self.assertEqual(len(fake.messages), 0)

            bot.ensure_survey_started(start + timedelta(minutes=2))
            self.assertEqual(len(fake.messages), 2)

            bot.ensure_survey_started(start + timedelta(minutes=3))
            self.assertEqual(len(fake.messages), 2)

            bot.ensure_survey_started(start + timedelta(minutes=4))
            self.assertEqual(len(fake.messages), 2)

            bot.ensure_survey_started(start + timedelta(minutes=6))
            self.assertEqual(len(fake.messages), 2)
            self.assertEqual(
                repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting"
            )
            self.assertTrue(repo.get_state("scheduled_survey_start")["consumed"])

    def test_relative_survey_late_tick_sends_once_without_bursting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(
                tmp_path,
                survey_start_at="+2m",
                target_year=2026,
                target_month=8,
            )
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            tehran = ZoneInfo("Asia/Tehran")
            start = datetime(2026, 7, 10, 9, 0, tzinfo=tehran)
            anchor_scheduled_survey_start(bot, start)

            bot.ensure_survey_started(start + timedelta(minutes=20))
            self.assertEqual(len(fake.messages), 2)

            state = repo.get_state("scheduled_survey_start")
            self.assertTrue(state["consumed"])
            self.assertEqual(state["start_at"], (start + timedelta(minutes=2)).isoformat())

    def test_empty_survey_start_uses_monthly_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.ensure_survey_started(date(2026, 7, 29))
            self.assertEqual(fake.messages, [])

            bot.ensure_survey_started(date(2026, 7, 30))
            self.assertEqual(len(fake.messages), 2)

            bot.ensure_survey_started(date(2026, 7, 31))
            self.assertEqual(len(fake.messages), 2)

    def test_collection_deadline_creates_preview_before_month_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, target_year=2026, target_month=8)
            bot_settings = bot_settings.__class__(
                **{**bot_settings.__dict__, "survey_collect_for": "+2m"}
            )
            write_schedule_config(bot_settings.schedule_config_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state(
                "active_survey",
                {
                    "calendar": "gregorian",
                    "year": 2026,
                    "month": 8,
                    "collect_until": "2026-07-10T09:02:00+03:30",
                },
            )
            repo.upsert_monthly_run("gregorian", 2026, 8, "collecting")
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], [], True),
            )

            before = datetime(2026, 7, 10, 9, 1, tzinfo=ZoneInfo("Asia/Tehran"))
            after = datetime(2026, 7, 10, 9, 3, tzinfo=ZoneInfo("Asia/Tehran"))

            self.assertFalse(bot.collection_deadline_reached(before))
            self.assertTrue(bot.collection_deadline_reached(after))
            bot.collection_deadline_reached = lambda now=None: False
            bot.maybe_create_preview(date(2026, 7, 10))
            self.assertEqual(fake.photos, [])

            bot.collection_deadline_reached = lambda now=None: True
            bot.maybe_create_preview(date(2026, 7, 10))

            self.assertEqual(len(fake.photos), 1)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "pending_admin_review")

    def test_bad_update_does_not_stop_loop_and_offset_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, debug_enabled=True)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            fake.updates = [
                {"update_id": 10, "callback_query": {"id": "bad", "data": "av:full"}},
                {
                    "update_id": 11,
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    },
                },
            ]
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            bot.target_month = lambda today=None: (2026, 8)
            bot.run_once = lambda: None

            offset = bot.run_loop_once(None)

            self.assertEqual(offset, 12)
            self.assertEqual(repo.get_update_offset(), 12)
            self.assertEqual(fake.messages[-1]["chat_id"], 1)

    def test_confirm_ignores_message_not_modified_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            fake.edit_error = TelegramApiError(
                "Telegram HTTP error for editMessageText: 400 "
                "Bad Request: message is not modified"
            )
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], [], True),
            )

            bot.handle_availability_callback(
                {
                    "id": "confirm-1",
                    "from": {"id": 1},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers[-1]["id"], "confirm-1")

    def test_callback_is_answered_before_message_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})

            bot.handle_availability_callback(
                {
                    "id": "dates-1",
                    "from": {"id": 1},
                    "data": av(bot, "dates"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.events[0][0], "answer_callback_query")
            self.assertEqual(fake.events[1][0], "edit_message_text")

    def test_callback_failure_sends_temporary_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            fake.edit_error = TelegramApiError("Telegram network error for editMessageText: timed out")
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})

            bot.handle_availability_callback(
                {
                    "id": "dates-2",
                    "from": {"id": 1},
                    "data": av(bot, "dates"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers[0]["id"], "dates-2")
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertIn("Temporary Telegram problem", fake.messages[-1]["text"])

    def test_full_available_locks_custom_choices_until_user_changes_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state(
                "active_survey",
                {
                    "calendar": "gregorian",
                    "year": 2026,
                    "month": 8,
                    "phase": "collecting",
                    "collect_until": "",
                    "allowed_member_ids": [],
                },
            )

            bot.handle_availability_callback(
                {
                    "id": "full-1",
                    "from": {"id": 1},
                    "data": av(bot, "full"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            response = repo.get_response("gregorian", 2026, 8, 1)
            self.assertEqual(response.mode, "full")
            self.assertTrue(response.confirmed)
            buttons = [
                button["callback_data"]
                for row in fake.edits[-1]["reply_markup"]["inline_keyboard"]
                for button in row
            ]
            self.assertNotIn("av:dates", buttons)
            self.assertNotIn("av:weekdays", buttons)

            bot.handle_availability_callback(
                {
                    "id": "dates-after-full",
                    "from": {"id": 1},
                    "data": av(bot, "dates"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers[-1]["text"], "Use Change availability first.")
            self.assertEqual(len(fake.edits), 1)

            bot.handle_availability_callback(
                {
                    "id": "custom-1",
                    "from": {"id": 1},
                    "data": av(bot, "custom"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            response = repo.get_response("gregorian", 2026, 8, 1)
            self.assertEqual(response.mode, "custom")
            self.assertFalse(response.confirmed)

    def test_preview_approval_and_daily_reminder_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], [], True),
            )

            bot.maybe_create_preview(date(2026, 8, 1))

            self.assertEqual(len(fake.photos), 1)
            self.assertTrue(fake.photos[0]["photo_path"].exists())
            self.assertEqual(fake.photos[0]["photo_path"].suffix, ".jpg")
            self.assertIn("Preview for August 2026", fake.photos[0]["caption"])
            self.assertLessEqual(len(fake.photos[0]["caption"]), 1024)
            self.assertIn("Preview report for August 2026", fake.messages[-1]["text"])
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "pending_admin_review")

            bot.handle_admin_callback(
                {
                    "id": "approve-1",
                    "from": {"id": 99},
                    "data": "admin:approve:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            self.assertEqual(fake.photos[-1]["chat_id"], -100123)
            self.assertEqual(fake.photos[-1]["message_thread_id"], 456)
            self.assertEqual(fake.photos[-1]["caption"], "Adhoc schedule - August 2026")
            self.assertEqual(fake.pins[-1]["chat_id"], -100123)
            self.assertEqual(fake.pins[-1]["message_id"], 2)
            self.assertEqual(fake.reply_markup_edits[-1]["reply_markup"], None)
            self.assertIn("posted and pinned", fake.messages[-1]["text"])
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "approved")

            with sqlite3.connect(bot_settings.database_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM schedule_entries").fetchone()[0]
            self.assertGreater(count, 0)

            bot.send_daily_reminder(datetime(2026, 8, 1, 9, 0))

            self.assertEqual(fake.messages[-1]["chat_id"], -100123)
            self.assertEqual(fake.messages[-1]["message_thread_id"], 456)
            self.assertIn("Good morning", fake.messages[-1]["text"])
            self.assertIn("@", fake.messages[-1]["text"])
            self.assertTrue(repo.daily_was_sent("2026-08-01"))

    def test_preview_is_sent_to_db_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            repo.upsert_user(
                username="admin_user",
                display_name="Admin User",
                role="backend",
                access_level="admin",
                telegram_id=909,
            )
            repo.upsert_user(
                username="member_user",
                display_name="Member User",
                role="frontend",
                access_level="member",
                telegram_id=808,
            )
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, [], repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})

            bot.create_preview(2026, 8)

            self.assertEqual(fake.photos[0]["chat_id"], 909)

    def test_approve_reports_pin_failure_to_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            fake.pin_error = TelegramApiError("not enough rights to pin a message")
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.maybe_create_preview(date(2026, 8, 1))

            bot.handle_admin_callback(
                {
                    "id": "approve-pin-fail",
                    "from": {"id": 99},
                    "data": "admin:approve:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            self.assertEqual(len(fake.photos), 2)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "approved")
            admin_message = fake.messages[-1]["text"]
            self.assertIn("posted", admin_message.lower())
            self.assertIn("pinning failed", admin_message.lower())
            self.assertIn("not enough rights to pin a message", admin_message)
            self.assertNotIn("posted and pinned", admin_message)

    def test_coverage_blocker_cannot_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], ["sunday"], True),
            )
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(2, "Sara", [], [], True),
            )

            bot.maybe_create_preview(date(2026, 8, 1))

            run = repo.get_monthly_run("gregorian", 2026, 8)
            self.assertEqual(run["status"], "blocked")
            self.assertIn("Coverage blockers", fake.messages[-1]["text"])

            bot.handle_admin_callback(
                {
                    "id": "approve-blocked",
                    "from": {"id": 99},
                    "data": "admin:approve:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            self.assertEqual(len(fake.photos), 1)
            self.assertIn("coverage blockers", fake.messages[-1]["text"])

    def test_admin_cancel_closes_cycle_and_removes_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state(
                "active_survey",
                {
                    "calendar": "gregorian",
                    "year": 2026,
                    "month": 8,
                    "phase": "collecting",
                    "collect_until": "",
                    "allowed_member_ids": [],
                },
            )

            bot.create_preview(2026, 8)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})

            bot.handle_admin_callback(
                {
                    "id": "cancel-1",
                    "from": {"id": 99},
                    "data": "admin:cancel:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "canceled")
            self.assertIsNone(repo.get_state("active_survey"))
            self.assertEqual(fake.reply_markup_edits[-1]["reply_markup"], None)
            self.assertIn("canceled", fake.messages[-1]["text"])
            self.assertTrue(
                fake.messages[-1]["reply_markup"]["inline_keyboard"][0][0][
                    "callback_data"
                ].startswith("admin:restart:S-")
            )

            bot.handle_admin_callback(
                {
                    "id": "restart-1",
                    "from": {"id": 99},
                    "data": "admin:restart:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 21},
                }
            )

            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting")
            self.assertEqual(repo.get_state("active_survey")["phase"], "collecting")
            self.assertEqual([message["chat_id"] for message in fake.messages[-3:]], [1, 2, 99])
            self.assertIn("restarted", fake.messages[-1]["text"])

    def test_admin_requests_corrections_for_flagged_members_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], ["sunday"], True),
            )
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(2, "Sara", [], [], True),
            )

            bot.maybe_create_preview(date(2026, 8, 1))
            self.assertLessEqual(len(fake.photos[0]["caption"]), 1024)
            self.assertIn("Request corrections will be sent to: Ali", fake.messages[-1]["text"])
            fake.messages.clear()

            bot.handle_admin_callback(
                {
                    "id": "correct-1",
                    "from": {"id": 99},
                    "data": "admin:correct:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            state = repo.get_state("active_survey")
            self.assertEqual(state["phase"], "revision_requested")
            self.assertEqual(state["allowed_member_ids"], [1])
            self.assertEqual(fake.messages[0]["chat_id"], 1)
            self.assertIn("needs review", fake.messages[0]["text"])
            self.assertEqual(fake.messages[1]["chat_id"], 99)
            self.assertIn("Revision window opened for: Ali", fake.messages[1]["text"])
            self.assertEqual(
                fake.messages[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                admin(bot, "close"),
            )

            bot.handle_private_text(2, "/start")
            self.assertIn("closed", fake.messages[-1]["text"])

    def test_debug_survey_messages_only_reachable_admin_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path, debug_enabled=True)
            write_schedule_config(bot_settings.schedule_config_path)
            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            debug_members = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
                Member("Local Debug", 0, "", "backend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, debug_members, repo, fake)

            bot.send_survey(
                2026,
                8,
                datetime(2026, 7, 29, 9, 0, tzinfo=ZoneInfo("Asia/Tehran")),
            )

            self.assertEqual([message["chat_id"] for message in fake.messages], [1])
            self.assertEqual(repo.get_state("active_survey")["phase"], "collecting")
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            self.assertTrue(bot.all_members_responded(2026, 8))
            bot.create_preview(2026, 8)
            self.assertIn("Sara", fake.messages[-1]["text"])
            self.assertNotIn("not confirmed", fake.messages[-1]["text"])

    def test_admin_approve_acknowledges_before_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})
            repo.save_response(
                "gregorian",
                2026,
                8,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.maybe_create_preview(date(2026, 8, 1))
            fake.events.clear()

            bot.handle_admin_callback(
                {
                    "id": "approve-2",
                    "from": {"id": 99},
                    "data": "admin:approve:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 21},
                }
            )

            self.assertEqual(fake.events[0][0], "answer_callback_query")
            self.assertEqual(fake.events[1][0], "send_photo")
            self.assertEqual(fake.events[2][0], "pin_chat_message")

    def test_preview_falls_back_when_output_dir_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            blocked_output = tmp_path / "blocked-output"
            blocked_output.mkdir()
            blocked_output.chmod(0o500)
            bot_settings = settings(tmp_path)
            bot_settings = bot_settings.__class__(
                **{**bot_settings.__dict__, "output_dir": blocked_output}
            )
            write_schedule_config(bot_settings.schedule_config_path)

            try:
                repo = BotRepository(bot_settings.database_path)
                fake = FakeTelegram()
                bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
                repo.set_state("active_survey", {"calendar": "gregorian", "year": 2026, "month": 8})

                bot.create_preview(2026, 8)

                image_path = fake.photos[0]["photo_path"]
                self.assertEqual(image_path.parent, bot_settings.database_path.parent / "output")
                self.assertTrue(image_path.exists())
            finally:
                blocked_output.chmod(0o700)

    def test_first_day_fallback_creates_preview_with_default_availability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            write_schedule_config(bot_settings.schedule_config_path)

            repo = BotRepository(bot_settings.database_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.ensure_month_started_preview(date(2026, 7, 31))
            self.assertEqual(fake.photos, [])

            bot.ensure_month_started_preview(date(2026, 8, 1))

            self.assertEqual(len(fake.photos), 1)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "pending_admin_review")


if __name__ == "__main__":
    unittest.main()
