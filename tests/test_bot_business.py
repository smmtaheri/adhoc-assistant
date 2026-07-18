import argparse
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from adhoc_assistant.config import load_config
from adhoc_assistant.scheduler import build_schedule
from adhoc_assistant.telegram_bot.keyboards import dates_keyboard, weekdays_keyboard
from adhoc_assistant.telegram_bot.service import (
    AdhocTelegramBot,
    anchor_scheduled_survey_start,
    build_bot,
    build_schedule_config,
    create_local_survey,
    due_survey_month,
    handle_user_admin_command,
    handle_local_db_command,
    is_local_only_member,
    mention,
    parse_args,
    parse_scheduled_datetime,
    local_only_member,
    reset_target_month_state,
    resolve_cli_survey_kind,
    response_for_survey_member,
    schedule_members,
    survey_identity,
)
from adhoc_assistant.telegram_bot.repository import (
    SURVEY_KIND_DEBUG,
    SURVEY_KIND_PRODUCTION,
    AvailabilityResponse,
    BotRepository,
    Survey,
    normalize_username,
    utc_now,
)
from adhoc_assistant.telegram_bot.settings import InfraSettings, Member, RuntimeSettings
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
        self.photo_errors_by_chat: dict[int, Exception] = {}
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
        if chat_id in self.photo_errors_by_chat:
            raise self.photo_errors_by_chat[chat_id]
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


@contextmanager
def connect_db(path: Path):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def members() -> list[Member]:
    return [
        Member("Ali", 1, "ali_user", "backend", True),
        Member("Sara", 2, "sara_user", "frontend", True),
        Member("Former", 3, "former_user", "backend", False),
        Member("Admin", 99, "admin_user", "manager", True, access_level="admin", participates_in_schedule=False),
    ]


def settings(tmp_path: Path) -> InfraSettings:
    return InfraSettings(database_path=tmp_path / "adhoc.sqlite3")


def complete_runtime(**overrides) -> dict:
    data = {
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
        "telegram_token": "TEST_TOKEN",
        "output_dir": "output",
        "holidays": [],
    }
    data.update(overrides)
    return data


def seed_allowlisted_members(repo: BotRepository, roster: list[Member]) -> None:
    """Persist real allowlisted users. Skip synthetic local-only debug participants."""
    for member in roster:
        if is_local_only_member(member):
            continue
        username = normalize_username(member.username)
        if not username:
            continue
        repo.upsert_user(
            username=username,
            display_name=member.name,
            role=member.role,
            access_level=member.access_level,
            active=member.active,
            participates_in_schedule=member.participates_in_schedule,
            telegram_id=member.telegram_id if member.telegram_id > 0 else None,
            unavailable_days=list(member.unavailable_days),
            unavailable_weekdays=list(member.unavailable_weekdays),
        )


def configure_runtime(
    repo: BotRepository,
    tmp_path: Path | None = None,
    *,
    allowlist: list[Member] | None = None,
    **overrides,
) -> RuntimeSettings:
    data = complete_runtime(**overrides)
    if tmp_path is not None and "output_dir" not in overrides:
        data["output_dir"] = str(tmp_path / "output")
    repo.set_state("runtime_settings", data)
    seed_allowlisted_members(repo, members() if allowlist is None else allowlist)
    return RuntimeSettings.from_dict(data)

def make_bot(
    tmp_path: Path,
    roster: list[Member] | None = None,
    *,
    repo: BotRepository | None = None,
    fake: FakeTelegram | None = None,
    **runtime_overrides,
) -> tuple[AdhocTelegramBot, BotRepository, FakeTelegram, InfraSettings]:
    bot_settings = settings(tmp_path)
    repository = repo or BotRepository(bot_settings.database_path)
    configure_runtime(repository, tmp_path, **runtime_overrides)
    members_list = roster if roster is not None else members()
    seed_allowlisted_members(repository, members_list)
    telegram = fake or FakeTelegram()
    bot = AdhocTelegramBot(bot_settings, members_list, repository, telegram)
    return bot, repository, telegram, bot_settings




def seed_collecting_survey(
    repo: BotRepository,
    *,
    year: int = 2026,
    month: int = 8,
    kind: str = SURVEY_KIND_PRODUCTION,
    participants: list[Member] | None = None,
    collect_until: str = "",
    status: str = "collecting",
    survey_id: str | None = None,
) -> Survey:
    roster = participants or schedule_members(members())
    seed_allowlisted_members(repo, roster)
    now = datetime.now(ZoneInfo("Asia/Tehran")).isoformat()
    survey = repo.create_survey(
        survey_id=survey_id or survey_identity("gregorian", year, month),
        calendar_type="gregorian",
        year=year,
        month=month,
        status=status,
        starts_at=now,
        closes_at=collect_until,
        created_by="test",
        participants=roster,
        kind=kind,
    )
    return survey


def active_survey_id(bot: AdhocTelegramBot) -> str:
    survey = bot.active_survey_record()
    assert survey is not None
    return survey.id


def av(bot: AdhocTelegramBot, action: str, survey_id: str | None = None) -> str:
    survey_key = survey_id or active_survey_id(bot)
    return f"av:{survey_key}:{action}"


def admin(bot: AdhocTelegramBot, action: str) -> str:
    return f"admin:{action}:{active_survey_id(bot)}"


class TelegramBotBusinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.last_export_schedule = None
        self.last_export_stats = None
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
        self.last_export_schedule = schedule
        self.last_export_stats = stats
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

        self.assertEqual([person["name"] for person in config["people"]], ["ali_user", "sara_user"])
        self.assertEqual(config["people"][0]["unavailable_dates"], ["2026-08-01"])
        self.assertEqual(config["people"][0]["unavailable_weekdays"], ["sunday"])
        self.assertEqual(config["people"][1]["unavailable_dates"], [])

        schedule, _stats = build_schedule(config)
        first_day = next(item for item in schedule if item["gregorian_date"] == "2026-08-01")
        self.assertNotEqual(first_day["main"], "ali_user")
        self.assertNotEqual(first_day["backup"], "ali_user")

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
            local = local_only_member("Local", 0)
            local = Member(
                local.name,
                local.telegram_id,
                local.username,
                local.role,
                True,
                unavailable_days=[3],
                unavailable_weekdays=["monday"],
            )
            local_members = [
                Member("Ali", 1, "ali_user", "backend", True),
                local,
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
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, local_members, repo, fake)
            survey = seed_collecting_survey(repo, participants=schedule_members(local_members))
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )

            # Local-only participants are not messageable and do not block collection.
            self.assertTrue(bot.all_members_responded(2026, 8, survey))

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
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.ensure_survey_started(date(2026, 7, 29))
            self.assertEqual(fake.messages, [])
            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))

            bot.ensure_survey_started(date(2026, 7, 30))
            self.assertEqual(len(fake.messages), 2)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "collecting")
            survey = repo.get_active_survey(SURVEY_KIND_PRODUCTION)
            self.assertIsNotNone(survey)
            self.assertEqual(len(repo.list_survey_responses(survey.id)), 2)

    def test_start_resumes_debug_survey_form_for_participant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            debug_members = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, debug_members, repo, fake)
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(debug_members),
            )

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            state = bot.survey_state_for(repo.get_survey(survey.id))
            self.assertEqual(state["id"], survey.id)
            self.assertEqual(state["phase"], "collecting")
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertEqual(set(fake.messages[-1]["reply_markup"]), {"inline_keyboard"})
    def test_start_without_active_survey_shows_no_survey_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            self.assertEqual(fake.messages[-1]["text"], "There is no active availability survey right now.")
            self.assertIsNone(repo.get_active_survey(SURVEY_KIND_PRODUCTION))
    def test_start_resumes_existing_survey_without_resetting_responses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, target_year=2026, target_month=8)
            fake = FakeTelegram()
            debug_members = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, debug_members, repo, fake)
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(debug_members),
            )
            repo.save_survey_response(
                survey.id,
                AvailabilityResponse(1, "Ali", [5], ["sunday"], True),
            )

            bot.handle_update(
                {
                    "message": {
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            response = repo.get_survey_response(survey.id, 1)
            self.assertEqual(response.unavailable_days, [5])
            self.assertEqual(response.unavailable_weekdays, ["sunday"])
            self.assertTrue(response.confirmed)

            bot.handle_availability_callback(
                {
                    "id": "confirm-1",
                    "from": {"id": 1},
                    "data": av(bot, "confirm", survey.id),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(len(fake.photos), 0)
            self.assertEqual(repo.get_survey(survey.id).status, "collecting")
    def test_start_does_not_create_survey_in_normal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
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
            configure_runtime(repo, tmp_path, allowlist=[])
            repo.upsert_user(
                username="new_user",
                display_name="New User",
                role="backend",
                access_level="member",
                telegram_id=None,
            )
            seed_collecting_survey(
                repo,
                participants=[Member("New User", 0, "new_user", "backend", True)],
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
            self.assertEqual(user["display_name"], "Telegram Name")
            self.assertEqual(bot.members[0].name, "Telegram Name")
            self.assertEqual(fake.messages[-1]["chat_id"], 777)
            self.assertEqual(set(fake.messages[-1]["reply_markup"]), {"inline_keyboard"})

    def test_preview_uses_db_display_names_not_usernames_in_exported_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot, repo, fake, _ = make_bot(tmp_path)
            survey = seed_collecting_survey(repo)
            repo.upsert_user(
                username="ali_user",
                display_name="Ali Display",
                role="backend",
                access_level="member",
                telegram_id=1,
            )
            repo.upsert_user(
                username="sara_user",
                display_name="Sara Display",
                role="frontend",
                access_level="member",
                telegram_id=2,
            )
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.persist_member_response(
                survey,
                AvailabilityResponse(2, "Sara", [], [], True),
            )

            bot.create_preview(2026, 8, survey_id=survey.id)

            exported_names = {
                item["main"]
                for item in (self.last_export_schedule or [])
                if item["main"] not in {"NO_AVAILABLE_PERSON", "NO_AVAILABLE_BACKUP"}
            } | {
                item["backup"]
                for item in (self.last_export_schedule or [])
                if item["backup"] not in {"NO_AVAILABLE_PERSON", "NO_AVAILABLE_BACKUP"}
            }
            self.assertIn("Ali Display", exported_names)
            self.assertIn("Sara Display", exported_names)
            self.assertNotIn("ali_user", exported_names)
            self.assertNotIn("sara_user", exported_names)

    def test_unknown_private_user_is_rejected_before_any_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.handle_update(
                {
                    "message": {
                        "from": {"id": 404, "username": "stranger", "first_name": "Nope"},
                        "chat": {"id": 404, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            self.assertEqual(fake.messages[-1]["chat_id"], 404)
            self.assertEqual(fake.messages[-1]["text"], "You are not allowed to use this bot.")
            self.assertIsNone(repo.get_active_survey(SURVEY_KIND_PRODUCTION))

    def test_inactive_allowlisted_user_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, allowlist=[])
            repo.upsert_user(
                username="former_user",
                display_name="Former",
                role="backend",
                access_level="member",
                active=False,
                telegram_id=None,
            )
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, [], repo, fake)

            bot.handle_update(
                {
                    "message": {
                        "from": {
                            "id": 303,
                            "username": "former_user",
                            "first_name": "Former",
                        },
                        "chat": {"id": 303, "type": "private"},
                        "text": "/start",
                    }
                }
            )

            self.assertEqual(fake.messages[-1]["text"], "You are not in the active member list.")
            self.assertIsNone(repo.get_user_by_telegram_id(303))

    def test_db_backed_bot_messages_override_access_and_gating_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(
                repo,
                tmp_path,
                allowlist=[],
                bot_messages={
                    "unauthorized": "CUSTOM_DENY",
                    "no_active_survey": "CUSTOM_NO_SURVEY",
                },
            )
            repo.upsert_user(
                username="ali_user",
                display_name="Ali",
                role="backend",
                access_level="member",
                telegram_id=1,
            )
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, [], repo, fake)

            bot.handle_update(
                {
                    "message": {
                        "from": {"id": 404, "username": "stranger"},
                        "chat": {"id": 404, "type": "private"},
                        "text": "/start",
                    }
                }
            )
            self.assertEqual(fake.messages[-1]["text"], "CUSTOM_DENY")

            bot.handle_update(
                {
                    "message": {
                        "from": {"id": 1, "username": "ali_user"},
                        "chat": {"id": 1, "type": "private"},
                        "text": "/start",
                    }
                }
            )
            self.assertEqual(fake.messages[-1]["text"], "CUSTOM_NO_SURVEY")

    def test_debug_survey_with_local_only_participants_still_collects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                local_only_member("Local Fake", 0),
            ]
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(roster),
            )
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            self.assertTrue(bot.all_members_responded(2026, 8, survey))
            self.assertIsNone(repo.get_user_by_username(""))
            participants = repo.list_survey_participants(survey.id)
            self.assertEqual(len(participants), 2)
            self.assertTrue(any(is_local_only_member(member) for member in participants))

    def test_unknown_callback_user_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

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

    def test_member_callback_updates_active_survey_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

            bot.handle_availability_callback(
                {
                    "id": "member-1",
                    "from": {"id": 1},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers[-1]["id"], "member-1")
            self.assertEqual(fake.edits[-1]["chat_id"], 1)
    def test_today_command_returns_main_and_backup_with_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2026, 8, "2026-08-01", "Saturday", "", "Ali", "Sara"),
                )
            repo.set_active_schedule_source(2026, 8)

            bot.send_today_bug_day(1, datetime(2026, 8, 1, 12, 0))

            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertIn("Bug day: Ali (1)", fake.messages[-1]["text"])
            self.assertIn("Helper: Sara (2)", fake.messages[-1]["text"])

    def test_admin_can_use_today_command_without_being_a_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2026, 8, "2026-08-01", "Saturday", "", "Ali", "Sara"),
                )
            repo.set_active_schedule_source(2026, 8)

            bot.handle_private_text(99, "/today")
            bot.send_today_bug_day(99, datetime(2026, 8, 1, 12, 0))

            self.assertEqual(fake.messages[-1]["chat_id"], 99)
            self.assertIn("Bug day: Ali (1)", fake.messages[-1]["text"])

    def test_scheduled_start_at_starts_collection_after_configured_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(
                repo,
                tmp_path,
                survey_start_at="2026-07-10 09:01",
                target_year=2026,
                target_month=8,
            )
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
            bot_settings = settings(tmp_path)
            _runtime_kw = dict(survey_start_at="+2m",
                target_year=2026,
                target_month=8)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, **_runtime_kw)
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
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(
                repo,
                tmp_path,
                survey_start_at="+2m",
                survey_collect_for="+2m",
                target_year=2026,
                target_month=8,
            )
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
            bot_settings = settings(tmp_path)
            _runtime_kw = dict(survey_start_at="+2m",
                target_year=2026,
                target_month=8)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, **_runtime_kw)
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
            bot_settings = settings(tmp_path)
            _runtime_kw = dict(survey_start_at="+2m",
                target_year=2026,
                target_month=8)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, **_runtime_kw)
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
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.ensure_survey_started(date(2026, 7, 29))
            self.assertEqual(fake.messages, [])

            bot.ensure_survey_started(date(2026, 7, 30))
            self.assertEqual(len(fake.messages), 2)

            bot.ensure_survey_started(date(2026, 7, 31))
            self.assertEqual(len(fake.messages), 2)

    def test_month_start_does_not_create_preview_before_all_confirm_or_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.maybe_create_preview(date(2026, 8, 1))

            self.assertEqual(fake.photos, [])
            self.assertEqual(repo.get_survey(survey.id).status, "collecting")

    def test_collection_deadline_creates_preview_before_month_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, target_year=2026, target_month=8, survey_collect_for="+2m")
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo, collect_until="2026-07-10T09:02:00+03:30")
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )

            before = datetime(2026, 7, 10, 9, 1, tzinfo=ZoneInfo("Asia/Tehran"))
            after = datetime(2026, 7, 10, 9, 3, tzinfo=ZoneInfo("Asia/Tehran"))

            self.assertFalse(bot.collection_deadline_reached(before, survey=survey))
            self.assertTrue(bot.collection_deadline_reached(after, survey=survey))
            bot.collection_deadline_reached = lambda now=None, survey=None: False
            bot.maybe_create_preview(date(2026, 7, 10))
            self.assertEqual(fake.photos, [])

            bot.collection_deadline_reached = lambda now=None, survey=None: True
            bot.maybe_create_preview(date(2026, 7, 10))

            self.assertEqual(len(fake.photos), 1)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "pending_admin_review")

    def test_bad_update_does_not_stop_loop_and_offset_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
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
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            fake.edit_error = TelegramApiError(
                "Telegram HTTP error for editMessageText: 400 "
                "Bad Request: message is not modified"
            )
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

            bot.handle_availability_callback(
                {
                    "id": "confirm-1",
                    "from": {"id": 1},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers[-1]["id"], "confirm-1")
            self.assertEqual(fake.answers[-1]["text"], "")
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertEqual(fake.messages[-1]["text"], "Availability saved.")

    def test_callback_is_answered_before_message_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

            bot.handle_availability_callback(
                {
                    "id": "dates-1",
                    "from": {"id": 1},
                    "data": av(bot, "dates"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.events[0][0], "edit_message_text")
            self.assertEqual(fake.events[1][0], "answer_callback_query")

    def test_callback_failure_sends_temporary_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            fake.edit_error = TelegramApiError("Telegram network error for editMessageText: timed out")
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

            bot.handle_availability_callback(
                {
                    "id": "dates-2",
                    "from": {"id": 1},
                    "data": av(bot, "dates"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertEqual(fake.answers, [])
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertIn("Temporary Telegram problem", fake.messages[-1]["text"])

    def test_confirm_callback_reports_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

            bot.handle_availability_callback(
                {
                    "id": "confirm-saved",
                    "from": {"id": 1},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            survey = repo.get_active_survey(SURVEY_KIND_PRODUCTION)
            response = repo.get_survey_response(survey.id, 1)
            self.assertTrue(response.confirmed)
            self.assertEqual(fake.answers[-1]["id"], "confirm-saved")
            self.assertEqual(fake.answers[-1]["text"], "")
            self.assertEqual(fake.edits[-1]["reply_markup"], None)
            self.assertIn("Availability saved.", fake.edits[-1]["text"])
            self.assertIn("Status: confirmed", fake.edits[-1]["text"])
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertEqual(fake.messages[-1]["text"], "Availability saved.")

    def test_confirm_callback_reports_saved_when_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            fake.edit_error = TelegramApiError("Telegram network error for editMessageText: timed out")
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

            bot.handle_availability_callback(
                {
                    "id": "confirm-refresh-failed",
                    "from": {"id": 1},
                    "data": av(bot, "confirm"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            survey = repo.get_active_survey(SURVEY_KIND_PRODUCTION)
            response = repo.get_survey_response(survey.id, 1)
            self.assertTrue(response.confirmed)
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertIn("Saved, but the form could not be refreshed.", fake.messages[-1]["text"])

    def test_full_available_locks_custom_choices_until_user_changes_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            seed_collecting_survey(repo)

            bot.handle_availability_callback(
                {
                    "id": "full-1",
                    "from": {"id": 1},
                    "data": av(bot, "full"),
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            survey = repo.get_active_survey(SURVEY_KIND_PRODUCTION)
            response = repo.get_survey_response(survey.id, 1)
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

            response = repo.get_survey_response(survey.id, 1)
            self.assertEqual(response.mode, "custom")
            self.assertFalse(response.confirmed)

    def test_preview_approval_and_daily_reminder_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.persist_member_response(
                survey,
                AvailabilityResponse(2, "Sara", [], [], True),
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

            with connect_db(bot_settings.database_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM schedule_entries").fetchone()[0]
            self.assertGreater(count, 0)

            # Publish may happen before the target month; live cutover is explicit.
            bot.maybe_activate_due_live_schedule(date(2026, 8, 1))
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

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
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
            seed_collecting_survey(repo)

            bot.create_preview(2026, 8)

            self.assertEqual(fake.photos[0]["chat_id"], 909)

    def test_approve_reports_pin_failure_to_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            fake.pin_error = TelegramApiError("not enough rights to pin a message")
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.persist_member_response(
                survey,
                AvailabilityResponse(2, "Sara", [], [], True),
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

    def test_coverage_warning_can_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            repo.save_survey_response(
                survey.id,
                AvailabilityResponse(1, "Ali", [], ["sunday"], True),
            )
            repo.save_survey_response(
                survey.id,
                AvailabilityResponse(2, "Sara", [], [], True),
            )

            bot.maybe_create_preview(date(2026, 8, 1))

            run = repo.get_monthly_run("gregorian", 2026, 8)
            self.assertEqual(run["status"], "pending_admin_review")
            self.assertIn("Coverage gap", fake.messages[-1]["text"])
            approve_button = fake.photos[0]["reply_markup"]["inline_keyboard"][0][0]["text"]
            self.assertEqual(approve_button, "Approve")

            bot.handle_admin_callback(
                {
                    "id": "approve-warning",
                    "from": {"id": 99},
                    "data": f"admin:approve:{survey.id}",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            self.assertEqual(len(fake.photos), 2)
            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "approved")
    def test_admin_cancel_closes_cycle_and_removes_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)

            bot.create_preview(2026, 8, survey_id=survey.id)

            bot.handle_admin_callback(
                {
                    "id": "cancel-1",
                    "from": {"id": 99},
                    "data": "admin:cancel:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            self.assertEqual(repo.get_monthly_run("gregorian", 2026, 8)["status"], "canceled")
            self.assertIsNone(repo.get_active_survey(SURVEY_KIND_PRODUCTION))
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
            restarted = repo.get_active_survey(SURVEY_KIND_PRODUCTION)
            self.assertIsNotNone(restarted)
            self.assertEqual(restarted.status, "collecting")
            self.assertEqual([message["chat_id"] for message in fake.messages[-3:]], [1, 2, 99])
            self.assertIn("restarted", fake.messages[-1]["text"])

    def test_admin_requests_corrections_for_flagged_members_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], ["sunday"], True),
            )
            bot.persist_member_response(
                survey,
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

            state = bot.survey_state_for(repo.get_survey(survey.id))
            self.assertEqual(state["phase"], "revision_requested")
            self.assertEqual(state["allowed_member_ids"], [1])
            self.assertFalse(repo.get_survey_response(survey.id, 1).confirmed)
            self.assertTrue(repo.get_survey_response(survey.id, 2).confirmed)
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

    def test_local_only_participants_are_not_messaged_and_do_not_block_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            debug_members = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
                local_only_member("Local Debug", 0),
            ]
            bot = AdhocTelegramBot(bot_settings, debug_members, repo, fake)
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(debug_members),
            )

            bot.send_survey_by_id(
                survey.id,
                datetime(2026, 7, 29, 9, 0, tzinfo=ZoneInfo("Asia/Tehran")),
            )

            self.assertEqual([message["chat_id"] for message in fake.messages], [1, 2])
            self.assertEqual(repo.get_survey(survey.id).status, "collecting")
            repo.save_survey_response(
                survey.id,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            repo.save_survey_response(
                survey.id,
                AvailabilityResponse(2, "Sara", [], [], True),
            )
            self.assertTrue(bot.all_members_responded(2026, 8, survey))
            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertIn("Sara", fake.messages[-1]["text"])
            self.assertNotIn("not confirmed", fake.messages[-1]["text"])

    def test_admin_approve_acknowledges_before_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.persist_member_response(
                survey,
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.persist_member_response(
                survey,
                AvailabilityResponse(2, "Sara", [], [], True),
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
            photo_index = next(
                i for i, event in enumerate(fake.events) if event[0] == "send_photo" and event[1] == -100123
            )
            pin_index = next(i for i, event in enumerate(fake.events) if event[0] == "pin_chat_message")
            self.assertLess(photo_index, pin_index)

    def test_preview_fails_when_output_dir_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            blocked_output = tmp_path / "blocked-output"
            blocked_output.mkdir()
            blocked_output.chmod(0o500)
            bot_settings = settings(tmp_path)
            try:
                repo = BotRepository(bot_settings.database_path)
                configure_runtime(repo, tmp_path, output_dir=str(blocked_output))
                fake = FakeTelegram()
                bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
                seed_collecting_survey(repo)

                with self.assertRaises(RuntimeError) as ctx:
                    bot.create_preview(2026, 8)
                self.assertIn("Configured output_dir is not writable", str(ctx.exception))
                self.assertEqual(fake.photos, [])
            finally:
                blocked_output.chmod(0o700)

    def test_preview_fails_when_default_output_dir_itself_is_unwritable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_output = tmp_path / "output"
            repo_output.mkdir()
            repo_output.chmod(0o500)
            bot_settings = settings(tmp_path)
            try:
                repo = BotRepository(bot_settings.database_path)
                configure_runtime(repo, tmp_path, output_dir=str(repo_output))
                fake = FakeTelegram()
                bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
                seed_collecting_survey(repo)

                with self.assertRaises(RuntimeError) as ctx:
                    bot.create_preview(2026, 8)
                self.assertIn("Configured output_dir is not writable", str(ctx.exception))
                self.assertEqual(fake.photos, [])
            finally:
                repo_output.chmod(0o700)

    def test_first_day_does_not_invent_survey_without_active_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)

            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            bot.maybe_activate_due_live_schedule(date(2026, 8, 1))
            self.assertEqual(fake.photos, [])
            self.assertIsNone(repo.get_active_survey(SURVEY_KIND_PRODUCTION))
            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))

    def test_active_production_survey_blocks_second_production_survey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            seed_collecting_survey(repo, survey_id="S-gregorian-2026-08-prod1")
            with self.assertRaises(ValueError) as ctx:
                repo.create_survey(
                    survey_id="S-gregorian-2026-08-prod2",
                    calendar_type="gregorian",
                    year=2026,
                    month=8,
                    status="collecting",
                    starts_at=datetime.now(ZoneInfo("Asia/Tehran")).isoformat(),
                    closes_at="",
                    created_by="test",
                    participants=schedule_members(members()),
                    kind=SURVEY_KIND_PRODUCTION,
                )
            self.assertIn("S-gregorian-2026-08-prod1", str(ctx.exception))

    def test_active_debug_survey_blocks_second_debug_survey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                survey_id="S-gregorian-2026-08-dbg1",
            )
            with self.assertRaises(ValueError) as ctx:
                repo.create_survey(
                    survey_id="S-gregorian-2026-08-dbg2",
                    calendar_type="gregorian",
                    year=2026,
                    month=8,
                    status="collecting",
                    starts_at=datetime.now(ZoneInfo("Asia/Tehran")).isoformat(),
                    closes_at="",
                    created_by="test",
                    participants=schedule_members(members()),
                    kind=SURVEY_KIND_DEBUG,
                )
            self.assertIn("S-gregorian-2026-08-dbg1", str(ctx.exception))

    def test_production_and_debug_surveys_track_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            production = seed_collecting_survey(
                repo,
                survey_id="S-gregorian-2026-08-prod",
            )
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                survey_id="S-gregorian-2026-08-dbg",
            )
            self.assertEqual(repo.get_active_survey(SURVEY_KIND_PRODUCTION).id, production.id)
            self.assertEqual(repo.get_active_survey(SURVEY_KIND_DEBUG).id, debug.id)

    def test_daily_reminder_reads_latest_schedule_entries_and_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            repo.set_state("daily_reminder_time", {"time": "09:00"})
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            work_date = "2026-08-01"
            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2026, 8, work_date, "Saturday", "", "Ali", "Sara"),
                )
            repo.set_active_schedule_source(2026, 8)
            bot.send_daily_reminder(datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo("Asia/Tehran")))
            self.assertIn("@ali_user", fake.messages[-1]["text"])

            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    "UPDATE schedule_entries SET main = ?, backup = ? WHERE work_date = ?",
                    ("Sara", "Ali", work_date),
                )
            repo.upsert_user(
                username="sara_user",
                display_name="Sara",
                role="frontend",
                access_level="member",
                telegram_id=2,
            )
            repo.clear_daily_reminder(work_date)
            bot.refresh_members()
            bot.send_daily_reminder(datetime(2026, 8, 1, 9, 5, tzinfo=ZoneInfo("Asia/Tehran")))
            self.assertIn("@sara_user", fake.messages[-1]["text"])
            self.assertIn("@ali_user", fake.messages[-1]["text"])

    def test_restart_send_does_not_duplicate_canonical_survey_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.send_survey_by_id(survey.id)
            self.assertEqual(len(fake.messages), 2)
            bot.send_survey_by_id(survey.id)
            self.assertEqual(len(fake.messages), 2)
            self.assertEqual(len(fake.edits), 2)

    def test_stale_callback_from_old_survey_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            old = seed_collecting_survey(repo, survey_id="S-gregorian-2026-08-old")
            repo.update_survey(old.id, status="canceled")
            repo.clear_active_survey_phase(SURVEY_KIND_PRODUCTION)
            seed_collecting_survey(repo, survey_id="S-gregorian-2026-08-new")

            bot.handle_availability_callback(
                {
                    "id": "stale-1",
                    "from": {"id": 1},
                    "data": "av:S-gregorian-2026-08-old:confirm",
                    "message": {"chat": {"id": 1}, "message_id": 10},
                }
            )

            self.assertIn("ended", fake.answers[-1]["text"].lower())
            self.assertEqual(fake.reply_markup_edits[-1]["reply_markup"], None)

    def test_debug_survey_does_not_overwrite_production_monthly_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            production = seed_collecting_survey(
                repo,
                survey_id="S-gregorian-2026-08-prod",
            )
            self.assertEqual(
                repo.get_monthly_run("gregorian", 2026, 8)["survey_id"],
                production.id,
            )
            seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                survey_id="S-gregorian-2026-08-dbg",
            )
            run = repo.get_monthly_run("gregorian", 2026, 8)
            self.assertEqual(run["survey_id"], production.id)
            self.assertEqual(run["status"], "collecting")

    def test_debug_responses_do_not_leak_into_production_survey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                survey_id="S-gregorian-2026-08-dbg",
            )
            bot.persist_member_response(
                debug,
                AvailabilityResponse(1, "Ali", [3], ["monday"], True, mode="custom"),
            )
            self.assertEqual(repo.list_responses("gregorian", 2026, 8), {})
            self.assertEqual(
                repo.get_survey_response(debug.id, 1).unavailable_days,
                [3],
            )

            production = seed_collecting_survey(
                repo,
                survey_id="S-gregorian-2026-08-prod",
            )
            responses = bot.responses_for_survey(production)
            self.assertNotIn(1, responses)
            ali = response_for_survey_member(repo, production.id, members()[0])
            self.assertEqual(ali.unavailable_days, [])
            self.assertFalse(ali.confirmed)

    def test_start_collecting_does_not_mutate_debug_survey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                status="scheduled",
                survey_id="S-gregorian-2026-08-dbg",
            )
            bot.start_collecting(2026, 8)
            debug_after = repo.get_survey(debug.id)
            self.assertEqual(debug_after.status, "scheduled")
            production = bot.active_or_latest_survey_for_month(2026, 8)
            self.assertIsNotNone(production)
            self.assertEqual(production.kind, SURVEY_KIND_PRODUCTION)
            self.assertEqual(production.status, "collecting")
            self.assertNotEqual(production.id, debug.id)

    def test_reset_target_month_only_resets_production_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, target_year=2026, target_month=8)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            production = seed_collecting_survey(
                repo,
                survey_id="S-gregorian-2026-08-prod",
            )
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                survey_id="S-gregorian-2026-08-dbg",
            )
            reset_target_month_state(bot)
            self.assertEqual(repo.get_survey(production.id).status, "canceled")
            self.assertEqual(repo.get_survey(debug.id).status, "collecting")
            self.assertIsNone(repo.get_active_survey(SURVEY_KIND_PRODUCTION))
            self.assertEqual(repo.get_active_survey(SURVEY_KIND_DEBUG).id, debug.id)

    def test_start_prefers_production_when_user_in_both_surveys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            roster = schedule_members(members())
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            production = seed_collecting_survey(
                repo,
                participants=roster,
                survey_id="S-gregorian-2026-08-prod",
            )
            seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=roster,
                survey_id="S-gregorian-2026-08-dbg",
            )
            bot.handle_private_text(1, "/start")
            self.assertEqual(fake.messages[-1]["chat_id"], 1)
            self.assertIn(production.id, fake.messages[-1]["text"])

    def test_debug_admin_reopen_revises_debug_survey_not_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            debug_roster = [
                Member("Ali", 1, "ali_user", "backend", True),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            production = seed_collecting_survey(
                repo,
                survey_id="S-gregorian-2026-08-prod",
            )
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=debug_roster,
                survey_id="S-gregorian-2026-08-dbg",
            )
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    debug,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )
            bot.create_preview(2026, 8, survey_id=debug.id)
            self.assertEqual(repo.get_survey(debug.id).status, "pending_admin_review")

            bot.handle_admin_callback(
                {
                    "id": "reopen-debug",
                    "from": {"id": 99},
                    "data": f"admin:reopen:{debug.id}",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )

            self.assertEqual(repo.get_survey(debug.id).status, "revision_requested")
            self.assertEqual(repo.get_survey(production.id).status, "collecting")
            self.assertEqual(
                sorted(repo.get_revision_allowlist(repo.get_survey(debug.id))),
                [1, 2],
            )
            self.assertEqual(
                [message["chat_id"] for message in fake.messages if "needs review" in message["text"]],
                [1, 2],
            )

    def test_debug_admin_correct_revises_only_debug_survey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            debug_roster = [
                Member("Ali", 1, "ali_user", "backend", True),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            production = seed_collecting_survey(
                repo,
                survey_id="S-gregorian-2026-08-prod",
            )
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=debug_roster,
                survey_id="S-gregorian-2026-08-dbg",
            )
            bot.persist_member_response(
                debug,
                AvailabilityResponse(1, "Ali", [], ["sunday"], True),
            )
            bot.persist_member_response(
                debug,
                AvailabilityResponse(2, "Sara", [], [], True),
            )
            bot.create_preview(2026, 8, survey_id=debug.id)

            bot.handle_admin_callback(
                {
                    "id": "correct-debug",
                    "from": {"id": 99},
                    "data": f"admin:correct:{debug.id}",
                    "message": {"chat": {"id": 99}, "message_id": 21},
                }
            )

            self.assertEqual(repo.get_survey(debug.id).status, "revision_requested")
            self.assertEqual(repo.get_survey(production.id).status, "collecting")
            self.assertEqual(repo.get_revision_allowlist(repo.get_survey(debug.id)), [1])

    def test_legacy_admin_callback_targets_production_not_newer_debug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            production = seed_collecting_survey(
                repo,
                survey_id="S-gregorian-2026-08-prod",
            )
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    production,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )
            bot.create_preview(2026, 8, survey_id=production.id)
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                survey_id="S-gregorian-2026-08-dbg-later",
            )
            self.assertEqual(repo.get_survey(production.id).status, "pending_admin_review")
            self.assertEqual(repo.get_survey(debug.id).status, "collecting")

            bot.handle_admin_callback(
                {
                    "id": "legacy-approve",
                    "from": {"id": 99},
                    "data": "admin:approve:gregorian:2026:8",
                    "message": {"chat": {"id": 99}, "message_id": 30},
                }
            )

            self.assertEqual(repo.get_survey(production.id).status, "approved")
            self.assertEqual(repo.get_survey(debug.id).status, "collecting")

    def test_debug_survey_lifecycle_mirrors_production_without_monthly_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)
            repo.update_runtime_settings(allow_production_destination=True)
            bot.refresh_runtime()
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(roster),
                survey_id="S-gregorian-2026-08-dbg-life",
            )
            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))

            bot.send_survey_by_id(survey.id)
            self.assertEqual([message["chat_id"] for message in fake.messages], [1, 2])
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    survey,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )
            self.assertEqual(repo.list_responses("gregorian", 2026, 8), {})

            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertEqual(repo.get_survey(survey.id).status, "pending_admin_review")
            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))

            bot.handle_admin_callback(
                {
                    "id": "approve-debug",
                    "from": {"id": 1},
                    "data": f"admin:approve:{survey.id}",
                    "message": {"chat": {"id": 1}, "message_id": 40},
                }
            )
            self.assertEqual(repo.get_survey(survey.id).status, "approved")
            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))
            self.assertEqual(fake.photos[-1]["chat_id"], -100123)
            self.assertEqual(fake.photos[-1]["message_thread_id"], 456)

    def test_runtime_settings_are_read_from_db_not_external_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, survey_start_at="+9h")
            repo.update_runtime_settings(
                survey_start_at="+1h",
                survey_collect_for="+30m",
                daily_reminder_time="10:15",
                target_year=1405,
                target_month=5,
            )
            # Creating another bot must keep DB values (no external overwrite).
            bot = AdhocTelegramBot(bot_settings, members(), repo, FakeTelegram())
            self.assertEqual(bot.runtime.survey_start_at, "+1h")
            self.assertEqual(bot.runtime.survey_collect_for, "+30m")
            self.assertEqual(bot.runtime.daily_reminder_time, "10:15")
            self.assertEqual(bot.runtime.target_year, 1405)
            self.assertEqual(bot.runtime.target_month, 5)
            self.assertEqual(bot.target_month(), (1405, 5))

    def test_build_bot_fails_fast_when_rsvg_convert_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            with mock.patch(
                "adhoc_assistant.telegram_bot.service.ensure_jpg_export_support",
                side_effect=RuntimeError("rsvg-convert is required to export JPG calendar images."),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    build_bot(bot_settings, repo)
            self.assertIn("rsvg-convert is required", str(ctx.exception))

    def _runtime_cli_args(self, **overrides) -> argparse.Namespace:
        args = argparse.Namespace(
            show_runtime_settings=False,
            set_telegram_group_chat_id=None,
            set_telegram_topic_id=None,
            clear_telegram_topic_id=False,
            list_surveys=False,
            create_survey=False,
            cancel_survey_id=None,
            restart_survey_id=None,
            finalize_publishing_survey_id=None,
            activate_survey_id=None,
            survey_id=None,
            set_survey_status=None,
            set_survey_starts_at=None,
            set_survey_closes_at=None,
            set_schedule_entry_date=None,
            entry_main=None,
            entry_backup=None,
            set_daily_reminder_time=None,
            clear_daily_reminder_date=None,
            set_runtime_survey_start_at=None,
            set_runtime_survey_collect_for=None,
            set_runtime_revision_collect_for=None,
            set_runtime_survey_days_before_month=None,
            set_runtime_target_month=None,
            clear_runtime_target_month=False,
            set_runtime_poll_interval_seconds=None,
            set_runtime_timezone=None,
            set_runtime_calendar=None,
            set_bot_name=None,
            set_bot_username=None,
            set_bot_id=None,
            set_telegram_token=None,
            set_output_dir=None,
            set_holidays=None,
            survey_kind=None,
            participants=None,
            target_month=None,
            survey_start_at=None,
            survey_collect_for=None,
            revision_collect_for=None,
            send_now=False,
            force_preview=False,
            direct_preview=False,
            publish_now=False,
            allow_production_destination=False,
            clear_allow_production_destination=False,
            replace_active_survey=False,
            upsert_user=None,
            deactivate_user=None,
            delete_user=None,
            user_display_name=None,
            user_role="",
            user_access_level="member",
            user_telegram_id=None,
            user_inactive=False,
            user_schedule_participant=False,
            user_manager_only=False,
            list_users=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_cli_can_deactivate_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            repo.upsert_user(
                username="ali_user",
                display_name="Ali",
                role="backend",
                access_level="member",
                active=True,
                telegram_id=101,
            )

            args = self._runtime_cli_args(deactivate_user="ali_user")
            self.assertTrue(handle_user_admin_command(repo, args))

            user = repo.get_user_by_username("ali_user")
            self.assertIsNotNone(user)
            self.assertFalse(user["active"])
            by_telegram_id = repo.get_user_by_telegram_id(101)
            self.assertIsNotNone(by_telegram_id)
            self.assertFalse(by_telegram_id["active"])

    def test_cli_can_delete_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            repo.upsert_user(
                username="sara_user",
                display_name="Sara",
                role="frontend",
                access_level="member",
                active=True,
                telegram_id=202,
            )

            args = self._runtime_cli_args(delete_user="@sara_user")
            self.assertTrue(handle_user_admin_command(repo, args))

            self.assertIsNone(repo.get_user_by_username("sara_user"))
            self.assertIsNone(repo.get_user_by_telegram_id(202))

    def test_cli_rejects_ambiguous_user_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = BotRepository(settings(Path(tmp)).database_path)
            args = self._runtime_cli_args(
                upsert_user="ali_user",
                deactivate_user="ali_user",
            )
            with self.assertRaises(SystemExit) as ctx:
                handle_user_admin_command(repo, args)
            self.assertIn("Use only one user command", str(ctx.exception))

    def test_cli_can_set_and_show_survey_policy_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            args = self._runtime_cli_args(
                set_runtime_survey_start_at="+2m",
                set_runtime_survey_collect_for="+10m",
                set_runtime_revision_collect_for="+1h",
                set_runtime_survey_days_before_month=3,
                set_runtime_target_month="1405-05",
                set_runtime_poll_interval_seconds=7,
                set_runtime_timezone="Asia/Tehran",
                set_runtime_calendar="jalali",
            )
            self.assertTrue(handle_local_db_command(repo, bot_settings, args))
            runtime = repo.get_runtime_settings()
            self.assertEqual(runtime.survey_start_at, "+2m")
            self.assertEqual(runtime.survey_collect_for, "+10m")
            self.assertEqual(runtime.revision_collect_for, "+1h")
            self.assertEqual(runtime.survey_days_before_month, 3)
            self.assertEqual(runtime.target_year, 1405)
            self.assertEqual(runtime.target_month, 5)
            self.assertEqual(runtime.poll_interval_seconds, 7)

            clear_args = self._runtime_cli_args(clear_runtime_target_month=True)
            self.assertTrue(handle_local_db_command(repo, bot_settings, clear_args))
            runtime = repo.get_runtime_settings()
            self.assertIsNone(runtime.target_year)
            self.assertIsNone(runtime.target_month)

    def test_daily_reminder_uses_db_reminder_time_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.update_runtime_settings(daily_reminder_time="11:00")
            repo.set_telegram_destination(group_chat_id=-100999, topic_id=77)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            work_date = "2026-08-01"
            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2026, 8, work_date, "Saturday", "", "Ali", "Sara"),
                )

            bot.send_daily_reminder(datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo("Asia/Tehran")))
            self.assertFalse(fake.messages)

            repo.set_active_schedule_source(2026, 8)
            bot.send_daily_reminder(datetime(2026, 8, 1, 11, 0, tzinfo=ZoneInfo("Asia/Tehran")))
            self.assertEqual(fake.messages[-1]["chat_id"], -100999)
            self.assertEqual(fake.messages[-1]["message_thread_id"], 77)
            self.assertIn("@ali_user", fake.messages[-1]["text"])

    def test_operational_cli_flags_do_not_mutate_existing_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(
                repo,
                tmp_path,
                survey_start_at="+1h",
                survey_collect_for="+2h",
                revision_collect_for="+3h",
                target_year=1405,
                target_month=5,
            )
            before = repo.get_runtime_settings().as_dict()
            # Operational create-survey flags only; no --set-* applied.
            _ = self._runtime_cli_args(
                survey_start_at="+9m",
                survey_collect_for="+20m",
                revision_collect_for="+30m",
                target_month="1404-01",
            )
            AdhocTelegramBot(bot_settings, members(), repo, FakeTelegram())
            self.assertEqual(repo.get_runtime_settings().as_dict(), before)

    def test_only_set_flags_mutate_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            before = repo.get_runtime_settings().as_dict()

            noop_args = self._runtime_cli_args(
                target_month="1405-05",
                survey_start_at="+2m",
                survey_collect_for="+10m",
                revision_collect_for="+2h",
            )
            self.assertFalse(handle_local_db_command(repo, bot_settings, noop_args))
            self.assertEqual(repo.get_runtime_settings().as_dict(), before)

            set_args = self._runtime_cli_args(
                set_runtime_survey_start_at="+15m",
                set_runtime_survey_collect_for="+25m",
            )
            self.assertTrue(handle_local_db_command(repo, bot_settings, set_args))
            runtime = repo.get_runtime_settings()
            self.assertEqual(runtime.survey_start_at, "+15m")
            self.assertEqual(runtime.survey_collect_for, "+25m")

    def test_bot_restart_keeps_db_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first = settings(tmp_path)
            repo = BotRepository(first.database_path)
            configure_runtime(repo, tmp_path, survey_start_at="+1h", survey_collect_for="+2h")
            AdhocTelegramBot(first, members(), repo, FakeTelegram())
            repo.update_runtime_settings(
                survey_start_at="+4h",
                survey_collect_for="+5h",
                daily_reminder_time="08:30",
            )

            second = settings(tmp_path)
            bot = AdhocTelegramBot(second, members(), repo, FakeTelegram())
            self.assertEqual(bot.runtime.survey_start_at, "+4h")
            self.assertEqual(bot.runtime.survey_collect_for, "+5h")
            self.assertEqual(bot.runtime.daily_reminder_time, "08:30")

    def test_missing_runtime_settings_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            with self.assertRaises(RuntimeError) as ctx:
                repo.get_runtime_settings()
            self.assertIn("runtime_settings is missing", str(ctx.exception))
            configure_runtime(repo, tmp_path)
            self.assertEqual(repo.get_runtime_settings().daily_reminder_time, "09:00")

    def test_debug_direct_preview_publishes_without_waiting_for_responses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
                local_only_member("Local Fake", 0),
            ]
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)
            repo.update_runtime_settings(allow_production_destination=True)
            bot.refresh_runtime()
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(roster),
                survey_id="S-gregorian-2026-08-direct",
            )
            bot.create_preview(2026, 8, survey_id=survey.id)
            survey = repo.get_survey(survey.id)
            self.assertEqual(survey.status, "pending_admin_review")
            self.assertIsNone(repo.get_monthly_run("gregorian", 2026, 8))
            bot.publish_survey(survey)
            self.assertEqual(repo.get_survey(survey.id).status, "approved")
            self.assertEqual(fake.photos[-1]["chat_id"], -100123)
            with connect_db(bot_settings.database_path) as conn:
                entries = conn.execute("SELECT COUNT(*) FROM schedule_entries").fetchone()[0]
                stats = conn.execute("SELECT COUNT(*) FROM monthly_stats").fetchone()[0]
            self.assertEqual(entries, 0)
            self.assertEqual(stats, 0)

    def test_publish_notifies_members_about_assigned_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    survey,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )

            bot.create_preview(2026, 8, survey_id=survey.id)
            bot.publish_survey(repo.get_survey(survey.id))

            member_messages = {
                message["chat_id"]: message["text"]
                for message in fake.messages
                if message["chat_id"] in {1, 2}
            }
            self.assertIn(1, member_messages)
            self.assertIn(2, member_messages)
            self.assertIn("The August 2026 schedule is live.", member_messages[1])
            self.assertIn("Main days:", member_messages[1])
            self.assertIn("Backup days:", member_messages[2])

    def test_debug_publish_does_not_overwrite_production_schedule_or_reminder_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)
            repo.update_runtime_settings(allow_production_destination=True)
            bot.refresh_runtime()

            production = seed_collecting_survey(
                repo,
                participants=schedule_members(roster),
                survey_id="S-gregorian-2026-08-prod-live",
            )
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    production,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )
            bot.create_preview(2026, 8, survey_id=production.id)
            bot.publish_survey(repo.get_survey(production.id))

            with connect_db(bot_settings.database_path) as conn:
                production_entries = conn.execute(
                    """
                    SELECT work_date, main, backup
                    FROM schedule_entries
                    WHERE year = ? AND month = ?
                    ORDER BY work_date
                    """,
                    (2026, 8),
                ).fetchall()
                production_stats = conn.execute(
                    """
                    SELECT person, main_count, backup_count, total_count, thursday_count
                    FROM monthly_stats
                    WHERE year = ? AND month = ?
                    ORDER BY person
                    """,
                    (2026, 8),
                ).fetchall()
            self.assertGreater(len(production_entries), 0)
            self.assertGreater(len(production_stats), 0)
            first_work_date, first_main, first_backup = production_entries[0]

            debug_roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
                local_only_member("Local Overwrite", 0),
            ]
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(debug_roster),
                survey_id="S-gregorian-2026-08-dbg-overwrite",
            )
            bot.create_preview(2026, 8, survey_id=debug.id)
            bot.publish_survey(repo.get_survey(debug.id))
            self.assertEqual(repo.get_survey(debug.id).status, "approved")

            with connect_db(bot_settings.database_path) as conn:
                after_entries = conn.execute(
                    """
                    SELECT work_date, main, backup
                    FROM schedule_entries
                    WHERE year = ? AND month = ?
                    ORDER BY work_date
                    """,
                    (2026, 8),
                ).fetchall()
                after_stats = conn.execute(
                    """
                    SELECT person, main_count, backup_count, total_count, thursday_count
                    FROM monthly_stats
                    WHERE year = ? AND month = ?
                    ORDER BY person
                    """,
                    (2026, 8),
                ).fetchall()
            self.assertEqual(after_entries, production_entries)
            self.assertEqual(after_stats, production_stats)

            bot.maybe_activate_due_live_schedule(date(2026, 8, 1))
            repo.clear_daily_reminder(first_work_date)
            reminder_at = datetime.fromisoformat(f"{first_work_date}T09:00:00+03:30")
            bot.send_daily_reminder(reminder_at)
            reminder_text = fake.messages[-1]["text"]
            if first_main in {"Ali", "ali_user"}:
                self.assertIn("@ali_user", reminder_text)
            elif first_main in {"Sara", "sara_user"}:
                self.assertIn("@sara_user", reminder_text)
            else:
                self.assertIn(first_main, reminder_text)
            self.assertNotIn("Local Overwrite", reminder_text)

    def test_activate_survey_id_explicitly_switches_live_reminder_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)

            production = seed_collecting_survey(
                repo,
                participants=schedule_members(roster),
                survey_id="S-gregorian-2026-08-prod-activate",
            )
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    production,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )
            bot.create_preview(2026, 8, survey_id=production.id)
            bot.publish_survey(repo.get_survey(production.id))

            debug_roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
                local_only_member("Local Overwrite", 0),
            ]
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(debug_roster),
                survey_id="S-gregorian-2026-08-dbg-activate",
            )
            bot.create_preview(2026, 8, survey_id=debug.id)

            with connect_db(bot_settings.database_path) as conn:
                before_activation = conn.execute(
                    """
                    SELECT work_date, main, backup
                    FROM schedule_entries
                    WHERE year = ? AND month = ?
                    ORDER BY work_date
                    """,
                    (2026, 8),
                ).fetchall()

            activate_args = self._runtime_cli_args(activate_survey_id=debug.id)
            self.assertTrue(handle_local_db_command(repo, bot_settings, activate_args))

            with connect_db(bot_settings.database_path) as conn:
                after_activation = conn.execute(
                    """
                    SELECT work_date, main, backup
                    FROM schedule_entries
                    WHERE year = ? AND month = ?
                    ORDER BY work_date
                    """,
                    (2026, 8),
                ).fetchall()

            self.assertNotEqual(after_activation, before_activation)
            self.assertTrue(
                any(
                    main == "Local Overwrite" or backup == "Local Overwrite"
                    for _, main, backup in after_activation
                )
            )

    def test_bot_flow_does_not_read_toml_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, holidays=[{"date": "2026-08-15", "name": "X"}])
            # No adhoc_config.toml / bot_config.toml present under tmp_path.
            self.assertFalse((tmp_path / "bot_config.toml").exists())
            self.assertFalse((tmp_path / "adhoc_config.toml").exists())
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertEqual(repo.get_survey(survey.id).status, "pending_admin_review")
            self.assertEqual(bot.runtime.holidays, [{"date": "2026-08-15", "name": "X"}])

    def test_stale_approve_during_revision_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            bot.handle_admin_callback(
                {
                    "id": "reopen-1",
                    "from": {"id": 99},
                    "data": f"admin:reopen:{survey.id}",
                    "message": {"chat": {"id": 99}, "message_id": 20},
                }
            )
            self.assertEqual(repo.get_survey(survey.id).status, "revision_requested")
            photos_before = len(fake.photos)
            bot.handle_admin_callback(
                {
                    "id": "stale-approve",
                    "from": {"id": 99},
                    "data": f"admin:approve:{survey.id}",
                    "message": {"chat": {"id": 99}, "message_id": 21},
                }
            )
            self.assertEqual(repo.get_survey(survey.id).status, "revision_requested")
            self.assertEqual(len(fake.photos), photos_before)
            self.assertIn("revision is in progress", fake.messages[-1]["text"])

    def test_preview_fanout_tolerates_unreachable_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, allowlist=[])
            repo.upsert_user(
                username="admin_ok",
                display_name="Admin OK",
                role="backend",
                access_level="admin",
                telegram_id=901,
                participates_in_schedule=False,
            )
            repo.upsert_user(
                username="admin_bad",
                display_name="Admin Bad",
                role="backend",
                access_level="admin",
                telegram_id=902,
                participates_in_schedule=False,
            )
            repo.upsert_user(
                username="ali_user",
                display_name="Ali",
                role="backend",
                access_level="member",
                telegram_id=1,
            )
            repo.upsert_user(
                username="sara_user",
                display_name="Sara",
                role="frontend",
                access_level="member",
                telegram_id=2,
            )
            fake = FakeTelegram()
            fake.photo_errors_by_chat[902] = TelegramApiError("Forbidden: bot was blocked by the user")
            bot = AdhocTelegramBot(bot_settings, [], repo, fake)
            survey = seed_collecting_survey(repo, participants=repo.list_schedule_members())
            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertEqual(repo.get_survey(survey.id).status, "pending_admin_review")
            self.assertEqual([photo["chat_id"] for photo in fake.photos], [901])
            self.assertTrue(
                any("delivery failed" in message["text"] for message in fake.messages)
            )

    def test_collecting_survey_resumes_missing_form_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            # Simulate partial crash: only Ali received the form.
            repo.record_survey_message(survey.id, 1, 1, 11)
            self.assertIsNone(repo.get_survey_message(survey.id, 2))
            bot.resume_collecting_form_delivery()
            self.assertEqual([message["chat_id"] for message in fake.messages], [2])
            self.assertIsNotNone(repo.get_survey_message(survey.id, 2))

    def test_production_unregistered_members_are_warned_not_auto_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Unreg", 0, "unreg_user", "frontend", True),
            ]
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)
            survey = seed_collecting_survey(
                repo,
                participants=schedule_members(roster),
            )
            participants = repo.list_survey_participants(survey.id)
            unreg = next(member for member in participants if member.username == "unreg_user")
            self.assertLess(unreg.telegram_id, 0)
            response = response_for_survey_member(repo, survey.id, unreg)
            self.assertFalse(response.confirmed)
            bot.create_preview(2026, 8, survey_id=survey.id)
            review_text = fake.messages[-1]["text"]
            self.assertIn("Unregistered member @unreg_user", review_text)

    def test_duplicate_unregistered_usernames_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            roster = [
                Member("One", 0, "same_user", "backend", True),
                Member("Two", 0, "same_user", "frontend", True),
            ]
            with self.assertRaises(ValueError):
                seed_collecting_survey(repo, participants=roster)

    def test_get_schedule_entry_uses_active_schedule_source_after_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    ) VALUES
                    (1405, 5, '2026-08-01', 'saturday', '', 'old_main', 'old_backup'),
                    (2026, 8, '2026-08-01', 'saturday', '', 'new_main', 'new_backup')
                    """
                )
            # Without an explicit live source, reminders have no schedule.
            self.assertIsNone(repo.get_schedule_entry("2026-08-01"))
            repo.set_active_schedule_source(1405, 5)
            self.assertEqual(repo.get_schedule_entry("2026-08-01"), ("old_main", "old_backup"))
            repo.set_active_schedule_source(2026, 8)
            self.assertEqual(repo.get_schedule_entry("2026-08-01"), ("new_main", "new_backup"))

    def test_debug_publish_requires_explicit_production_destination_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(roster),
            )
            bot.create_preview(2026, 8, survey_id=survey.id)
            with self.assertRaises(RuntimeError) as ctx:
                bot.publish_survey(repo.get_survey(survey.id))
            self.assertIn("--allow-production-destination", str(ctx.exception))
            self.assertEqual(repo.get_survey(survey.id).status, "pending_admin_review")
            self.assertEqual(len([photo for photo in fake.photos if photo["chat_id"] == -100123]), 0)

            repo.update_runtime_settings(allow_production_destination=True)
            bot.refresh_runtime()
            bot.publish_survey(repo.get_survey(survey.id))
            self.assertEqual(repo.get_survey(survey.id).status, "approved")
            self.assertEqual(fake.photos[-1]["chat_id"], -100123)

    def test_debug_publish_opt_in_is_db_backed_across_bot_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            survey = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(roster),
            )
            first = AdhocTelegramBot(bot_settings, roster, repo, FakeTelegram())
            first.create_preview(2026, 8, survey_id=survey.id)
            with self.assertRaises(RuntimeError):
                first.publish_survey(repo.get_survey(survey.id))

            args = self._runtime_cli_args(allow_production_destination=True)
            self.assertTrue(handle_local_db_command(repo, bot_settings, args))
            self.assertTrue(repo.get_runtime_settings().allow_production_destination)

            second = AdhocTelegramBot(bot_settings, roster, repo, FakeTelegram())
            second.publish_survey(repo.get_survey(survey.id))
            self.assertEqual(repo.get_survey(survey.id).status, "approved")

    def test_ambiguous_publish_send_failure_stays_publishing_until_manual_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            photos_after_preview = len(fake.photos)
            send_attempts = {"count": 0}
            original_send_photo = fake.send_photo

            def boom(*args, **kwargs):
                send_attempts["count"] += 1
                raise TelegramApiError("Telegram network error for sendPhoto: timed out")

            fake.send_photo = boom  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError) as ctx:
                bot.publish_survey(repo.get_survey(survey.id))
            self.assertIn("left in publishing", str(ctx.exception))
            self.assertEqual(repo.get_survey(survey.id).status, "publishing")
            self.assertEqual(send_attempts["count"], 1)
            self.assertEqual(len(fake.photos), photos_after_preview)

            fake.send_photo = original_send_photo  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError) as retry_ctx:
                bot.publish_survey(repo.get_survey(survey.id))
            self.assertIn("stuck in publishing", str(retry_ctx.exception))
            self.assertEqual(repo.get_survey(survey.id).status, "publishing")
            self.assertEqual(send_attempts["count"], 1)
            self.assertEqual(len(fake.photos), photos_after_preview)

    def test_publish_finalize_requires_confirmed_group_delivery_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertTrue(
                repo.transition_survey(
                    survey.id,
                    from_statuses={"pending_admin_review"},
                    to_status="publishing",
                    group_sent_at=utc_now(),
                )
            )
            photos_before = len(fake.photos)
            bot.publish_survey(repo.get_survey(survey.id))
            self.assertEqual(repo.get_survey(survey.id).status, "approved")
            self.assertEqual(len(fake.photos), photos_before)

    def test_finalize_publish_does_not_approve_when_live_schedule_activation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            repo.update_survey(survey.id, schedule_json="{")
            self.assertTrue(
                repo.transition_survey(
                    survey.id,
                    from_statuses={"pending_admin_review"},
                    to_status="publishing",
                    group_sent_at=utc_now(),
                )
            )

            with self.assertRaises(json.JSONDecodeError):
                bot.publish_survey(repo.get_survey(survey.id))

            current = repo.get_survey(survey.id)
            self.assertEqual(current.status, "publishing")
            self.assertIsNone(current.approved_at)
            self.assertIsNone(repo.get_active_schedule_source())

    def test_preview_with_no_reachable_admins_records_delivery_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, allowlist=[])
            repo.upsert_user(
                username="ali_user",
                display_name="Ali",
                role="backend",
                access_level="member",
                telegram_id=1,
            )
            repo.upsert_user(
                username="admin_bad",
                display_name="Admin Bad",
                role="backend",
                access_level="admin",
                telegram_id=902,
                participates_in_schedule=False,
            )
            fake = FakeTelegram()
            fake.photo_errors_by_chat[902] = TelegramApiError("Forbidden: bot was blocked")
            bot = AdhocTelegramBot(bot_settings, [], repo, fake)
            survey = seed_collecting_survey(repo, participants=repo.list_schedule_members())
            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertEqual(repo.get_survey(survey.id).status, "pending_admin_review")
            review = json.loads(repo.get_survey(survey.id).review_json or "{}")
            self.assertTrue(review.get("preview_delivery_failed"))
            self.assertTrue(
                any("no admin could be reached" in warning for warning in review.get("warnings", []))
            )

    def test_daily_reminder_claim_prevents_duplicate_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    ) VALUES (2026, 8, '2026-08-01', 'saturday', '', 'ali_user', 'sara_user')
                    """
                )
            repo.set_active_schedule_source(2026, 8)
            now = datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))
            bot.send_daily_reminder(now)
            bot.send_daily_reminder(now)
            reminder_messages = [
                message
                for message in fake.messages
                if message["chat_id"] == -100123 and "Good morning" in message["text"]
            ]
            self.assertEqual(len(reminder_messages), 1)
            self.assertTrue(repo.daily_was_sent("2026-08-01"))

    def test_publish_is_idempotent_after_publishing_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertTrue(
                repo.transition_survey(
                    survey.id,
                    from_statuses={"pending_admin_review"},
                    to_status="publishing",
                    group_sent_at=utc_now(),
                )
            )
            photos_before = len(fake.photos)
            bot.publish_survey(repo.get_survey(survey.id))
            self.assertEqual(repo.get_survey(survey.id).status, "approved")
            # Finalize without another group photo send.
            self.assertEqual(len(fake.photos), photos_before)
            # Early publish of a future month must not cut over live reminders yet.
            self.assertIsNone(repo.get_active_schedule_source())
            bot.maybe_activate_due_live_schedule(date(2026, 8, 1))
            self.assertEqual(repo.get_active_schedule_source(), (2026, 8))

    def test_replace_active_survey_cancels_current_and_starts_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            old = seed_collecting_survey(repo, survey_id="S-gregorian-2026-08-old")
            repo.save_survey_response(
                old.id,
                AvailabilityResponse(1, "Ali", [2], [], True),
            )
            new = repo.create_survey(
                survey_id="S-gregorian-2026-08-new",
                calendar_type="gregorian",
                year=2026,
                month=8,
                status="collecting",
                starts_at=datetime.now(ZoneInfo("Asia/Tehran")).isoformat(),
                closes_at="",
                created_by="test-replace",
                participants=schedule_members(members()),
                kind=SURVEY_KIND_PRODUCTION,
                replace_active=True,
            )
            self.assertEqual(repo.get_survey(old.id).status, "canceled")
            self.assertEqual(repo.get_active_survey(SURVEY_KIND_PRODUCTION).id, new.id)
            self.assertNotEqual(old.id, new.id)
            self.assertEqual(repo.list_survey_responses(new.id), {})

    def test_replace_active_required_when_survey_already_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            seed_collecting_survey(repo, survey_id="S-gregorian-2026-08-keep")
            with self.assertRaises(ValueError) as ctx:
                repo.create_survey(
                    survey_id="S-gregorian-2026-08-blocked",
                    calendar_type="gregorian",
                    year=2026,
                    month=8,
                    status="collecting",
                    starts_at=datetime.now(ZoneInfo("Asia/Tehran")).isoformat(),
                    closes_at="",
                    created_by="test",
                    participants=schedule_members(members()),
                    kind=SURVEY_KIND_PRODUCTION,
                    replace_active=False,
                )
            self.assertIn("--replace-active-survey", str(ctx.exception))
            self.assertEqual(
                repo.get_active_survey(SURVEY_KIND_PRODUCTION).id,
                "S-gregorian-2026-08-keep",
            )

    def test_approved_survey_blocks_new_survey_until_close_window_ends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            survey = repo.create_survey(
                survey_id="S-gregorian-2026-08-approved-live",
                calendar_type="gregorian",
                year=2026,
                month=8,
                status="publishing",
                starts_at="2026-07-17T09:00:00+03:30",
                closes_at="2026-08-01T00:00:00+03:30",
                created_by="test",
                participants=schedule_members(members()),
                kind=SURVEY_KIND_PRODUCTION,
                replace_active=False,
            )
            repo.update_survey(
                survey.id,
                status="approved",
                group_sent_at="2026-07-17T10:00:00+03:30",
                approved_at="2026-07-17T10:00:00+03:30",
            )
            self.assertEqual(repo.get_active_survey(SURVEY_KIND_PRODUCTION).id, survey.id)

            with self.assertRaises(ValueError) as ctx:
                repo.create_survey(
                    survey_id="S-gregorian-2026-08-blocked-by-live-approved",
                    calendar_type="gregorian",
                    year=2026,
                    month=8,
                    status="collecting",
                    starts_at="2026-07-17T11:00:00+03:30",
                    closes_at="2026-07-17T12:00:00+03:30",
                    created_by="test",
                    participants=schedule_members(members()),
                    kind=SURVEY_KIND_PRODUCTION,
                    replace_active=False,
                )
            self.assertIn("still approved until", str(ctx.exception))

    def test_approved_survey_is_not_active_after_close_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            survey = repo.create_survey(
                survey_id="S-gregorian-2026-08-approved-expired",
                calendar_type="gregorian",
                year=2026,
                month=8,
                status="publishing",
                starts_at="2026-07-16T09:00:00+03:30",
                closes_at="2026-07-16T10:00:00+03:30",
                created_by="test",
                participants=schedule_members(members()),
                kind=SURVEY_KIND_PRODUCTION,
                replace_active=False,
            )
            repo.update_survey(
                survey.id,
                status="approved",
                group_sent_at="2026-07-16T09:30:00+03:30",
                approved_at="2026-07-16T09:30:00+03:30",
            )
            self.assertIsNone(repo.get_active_survey(SURVEY_KIND_PRODUCTION))

    def test_revision_does_not_autocomplete_from_old_confirms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, revision_collect_for="+2h")
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    survey,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )
            bot.create_preview(2026, 8, survey_id=survey.id)
            bot.start_revision(repo.get_survey(survey.id), [1, 2])
            survey = repo.get_survey(survey.id)
            self.assertEqual(survey.status, "revision_requested")
            self.assertFalse(repo.get_survey_response(survey.id, 1).confirmed)
            self.assertFalse(repo.get_survey_response(survey.id, 2).confirmed)
            photos_before = len(fake.photos)
            # Month already started must not close revision while confirms are cleared.
            bot.maybe_create_preview(date(2026, 8, 1))
            self.assertEqual(repo.get_survey(survey.id).status, "revision_requested")
            self.assertEqual(len(fake.photos), photos_before)

    def test_early_next_month_publish_keeps_current_month_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)

            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    ) VALUES (2026, 7, '2026-07-17', 'friday', '', 'ali_user', 'sara_user')
                    """
                )
            repo.set_active_schedule_source(2026, 7)

            next_month = seed_collecting_survey(repo, year=2026, month=8)
            bot.create_preview(2026, 8, survey_id=next_month.id)
            bot.publish_survey(repo.get_survey(next_month.id))
            self.assertEqual(repo.get_survey(next_month.id).status, "approved")
            self.assertEqual(repo.get_active_schedule_source(), (2026, 7))
            self.assertEqual(repo.get_schedule_entry("2026-07-17"), ("ali_user", "sara_user"))

            bot.maybe_activate_due_live_schedule(date(2026, 8, 1))
            self.assertEqual(repo.get_active_schedule_source(), (2026, 8))

    def test_canceled_survey_cannot_be_activated_as_live_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            survey = seed_collecting_survey(repo)
            bot = AdhocTelegramBot(bot_settings, members(), repo, FakeTelegram())
            bot.create_preview(2026, 8, survey_id=survey.id)
            repo.transition_survey(
                survey.id,
                from_statuses={"pending_admin_review"},
                to_status="canceled",
            )
            with self.assertRaises(SystemExit) as ctx:
                handle_local_db_command(
                    repo,
                    bot_settings,
                    self._runtime_cli_args(activate_survey_id=survey.id),
                )
            self.assertIn("canceled", str(ctx.exception).lower())
            self.assertIsNone(repo.get_active_schedule_source())

    def test_cli_can_finalize_stuck_publishing_survey_after_manual_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            self.assertTrue(
                repo.transition_survey(
                    survey.id,
                    from_statuses={"pending_admin_review"},
                    to_status="publishing",
                )
            )
            args = self._runtime_cli_args(finalize_publishing_survey_id=survey.id)
            self.assertTrue(handle_local_db_command(repo, bot_settings, args))
            current = repo.get_survey(survey.id)
            self.assertEqual(current.status, "approved")
            self.assertIsNotNone(current.group_sent_at)

    def test_username_cannot_rebind_existing_telegram_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = BotRepository(settings(tmp_path).database_path)
            configure_runtime(repo, tmp_path)
            repo.upsert_user(
                username="admin_user",
                display_name="Admin",
                role="backend",
                access_level="admin",
                telegram_id=100,
            )
            # Attacker tries to claim the allowlisted username from another Telegram id.
            rebound = repo.authorize_user_from_start("admin_user", 999, "Attacker")
            self.assertIsNone(rebound)
            self.assertEqual(repo.get_user_by_username("admin_user")["telegram_id"], 100)

            # Telegram id already bound to another username cannot steal a second allowlist row.
            repo.upsert_user(
                username="other_user",
                display_name="Other",
                role="frontend",
                access_level="member",
            )
            stolen = repo.authorize_user_from_start("other_user", 100, "Hijack")
            self.assertIsNone(stolen)
            self.assertIsNone(repo.get_user_by_username("other_user")["telegram_id"])

    def test_daily_reminder_marks_sent_only_after_successful_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            repo.set_telegram_destination(group_chat_id=-100123, topic_id=456)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            with connect_db(bot_settings.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_entries (
                        year, month, work_date, weekday, holiday, main, backup
                    ) VALUES (2026, 8, '2026-08-01', 'saturday', '', 'ali_user', 'sara_user')
                    """
                )
            repo.set_active_schedule_source(2026, 8)
            now = datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo("Asia/Tehran"))

            original_send = fake.send_message

            def boom(*args, **kwargs):
                raise TelegramApiError("send failed")

            fake.send_message = boom  # type: ignore[method-assign]
            with self.assertRaises(TelegramApiError):
                bot.send_daily_reminder(now)
            self.assertFalse(repo.daily_was_sent("2026-08-01"))

            fake.send_message = original_send  # type: ignore[method-assign]
            bot.send_daily_reminder(now)
            self.assertTrue(repo.daily_was_sent("2026-08-01"))
            bot.send_daily_reminder(now)
            reminder_messages = [
                message
                for message in fake.messages
                if message["chat_id"] == -100123 and "Good morning" in message["text"]
            ]
            self.assertEqual(len(reminder_messages), 1)

    def test_debug_and_production_share_revision_autocomplete_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path, revision_collect_for="")
            fake = FakeTelegram()
            roster = [
                Member("Ali", 1, "ali_user", "backend", True, access_level="admin"),
                Member("Sara", 2, "sara_user", "frontend", True),
            ]
            bot = AdhocTelegramBot(bot_settings, roster, repo, fake)
            debug = seed_collecting_survey(
                repo,
                kind=SURVEY_KIND_DEBUG,
                participants=schedule_members(roster),
            )
            for telegram_id, name in ((1, "Ali"), (2, "Sara")):
                bot.persist_member_response(
                    debug,
                    AvailabilityResponse(telegram_id, name, [], [], True),
                )
            bot.create_preview(2026, 8, survey_id=debug.id)
            bot.start_revision(repo.get_survey(debug.id), [1])
            self.assertEqual(repo.get_survey(debug.id).status, "revision_requested")
            bot.maybe_create_preview(date(2026, 8, 1))
            self.assertEqual(repo.get_survey(debug.id).status, "revision_requested")
            bot.persist_member_response(
                repo.get_survey(debug.id),
                AvailabilityResponse(1, "Ali", [], [], True),
            )
            bot.maybe_create_preview(date(2026, 8, 1))
            self.assertEqual(repo.get_survey(debug.id).status, "pending_admin_review")

    def test_direct_preview_and_publish_now_default_to_debug_kind(self) -> None:
        direct = parse_args(["--direct-preview"])
        self.assertIsNone(direct.survey_kind)
        self.assertEqual(resolve_cli_survey_kind(direct), SURVEY_KIND_DEBUG)

        publish = parse_args(["--publish-now"])
        self.assertIsNone(publish.survey_kind)
        self.assertEqual(resolve_cli_survey_kind(publish), SURVEY_KIND_DEBUG)

        explicit = parse_args(["--direct-preview", "--survey-kind", "production"])
        self.assertEqual(explicit.survey_kind, SURVEY_KIND_PRODUCTION)
        self.assertEqual(resolve_cli_survey_kind(explicit), SURVEY_KIND_PRODUCTION)

        create_only = parse_args(["--create-survey"])
        self.assertIsNone(create_only.survey_kind)
        self.assertEqual(resolve_cli_survey_kind(create_only), SURVEY_KIND_PRODUCTION)

    def test_create_local_survey_uses_debug_default_for_direct_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            for username, name, access in (
                ("ali_user", "Ali", "admin"),
                ("sara_user", "Sara", "member"),
            ):
                repo.upsert_user(
                    username=username,
                    display_name=name,
                    role="backend",
                    access_level=access,
                    telegram_id={"ali_user": 1, "sara_user": 2}[username],
                )
            args = self._runtime_cli_args(
                direct_preview=True,
                target_month="2026-08",
                replace_active_survey=True,
            )
            survey = create_local_survey(repo, args)
            self.assertEqual(survey.kind, SURVEY_KIND_DEBUG)

            prod_args = self._runtime_cli_args(
                direct_preview=True,
                survey_kind=SURVEY_KIND_PRODUCTION,
                target_month="2026-08",
                replace_active_survey=True,
            )
            production = create_local_survey(repo, prod_args)
            self.assertEqual(production.kind, SURVEY_KIND_PRODUCTION)

    def test_set_survey_closes_at_updates_runtime_deadline_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            fake = FakeTelegram()
            bot = AdhocTelegramBot(bot_settings, members(), repo, fake)
            survey = seed_collecting_survey(
                repo,
                collect_until="2026-07-20T12:00:00+03:30",
            )
            now = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Asia/Tehran"))
            self.assertFalse(bot.collection_deadline_reached(now, survey=repo.get_survey(survey.id)))

            args = self._runtime_cli_args(
                survey_id=survey.id,
                set_survey_closes_at="2026-07-15 09:00",
            )
            self.assertTrue(handle_local_db_command(repo, bot_settings, args))
            updated = repo.get_survey(survey.id)
            self.assertTrue(bot.collection_deadline_reached(now, survey=updated))
            member = members()[0]
            state = bot.survey_state_for(updated)
            self.assertEqual(state["collect_until"], updated.closes_at)
            self.assertFalse(bot.member_can_edit_survey(member, updated, state, now=now))

    def test_survey_state_uses_survey_row_not_stale_phase_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            bot = AdhocTelegramBot(bot_settings, members(), repo, FakeTelegram())
            survey = seed_collecting_survey(repo)
            bot.create_preview(2026, 8, survey_id=survey.id)
            bot.start_revision(repo.get_survey(survey.id), [1])
            survey = repo.get_survey(survey.id)
            # Corrupt allowlist blob metadata; status/deadline still come from survey row.
            repo.set_state(
                f"active_survey:{SURVEY_KIND_PRODUCTION}",
                {
                    "id": survey.id,
                    "kind": SURVEY_KIND_PRODUCTION,
                    "allowed_member_ids": [1],
                },
            )
            state = bot.survey_state_for(repo.get_survey(survey.id))
            self.assertEqual(state["phase"], "revision_requested")
            self.assertEqual(state["collect_until"], survey.closes_at)
            self.assertEqual(state["allowed_member_ids"], [1])
            self.assertTrue(
                bot.member_can_edit_survey(members()[0], survey, state)
            )
            self.assertFalse(
                bot.member_can_edit_survey(members()[1], survey, state)
            )

    def test_set_survey_status_only_allows_guarded_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bot_settings = settings(tmp_path)
            repo = BotRepository(bot_settings.database_path)
            configure_runtime(repo, tmp_path)
            survey = seed_collecting_survey(repo)

            with self.assertRaises(SystemExit):
                parse_args(["--survey-id", survey.id, "--set-survey-status", "approved"])
            with self.assertRaises(SystemExit):
                parse_args(["--survey-id", survey.id, "--set-survey-status", "publishing"])
            with self.assertRaises(SystemExit):
                parse_args(
                    ["--survey-id", survey.id, "--set-survey-status", "pending_admin_review"]
                )
            with self.assertRaises(SystemExit):
                parse_args(
                    ["--survey-id", survey.id, "--set-survey-status", "revision_requested"]
                )

            # collecting cannot jump into admin-review states even if forced via Namespace.
            for bad_status in (
                "pending_admin_review",
                "revision_requested",
                "scheduled",
                "blocked",
                "approved",
                "publishing",
            ):
                with self.assertRaises(SystemExit) as ctx:
                    handle_local_db_command(
                        repo,
                        bot_settings,
                        self._runtime_cli_args(
                            survey_id=survey.id,
                            set_survey_status=bad_status,
                        ),
                    )
                self.assertIn("Cannot set survey", str(ctx.exception))
                self.assertEqual(repo.get_survey(survey.id).status, "collecting")

            cancel_args = self._runtime_cli_args(
                survey_id=survey.id,
                set_survey_status="canceled",
            )
            self.assertTrue(handle_local_db_command(repo, bot_settings, cancel_args))
            self.assertEqual(repo.get_survey(survey.id).status, "canceled")

            scheduled = seed_collecting_survey(
                repo,
                status="scheduled",
                survey_id="S-gregorian-2026-08-sched",
            )
            open_args = self._runtime_cli_args(
                survey_id=scheduled.id,
                set_survey_status="collecting",
            )
            self.assertTrue(handle_local_db_command(repo, bot_settings, open_args))
            self.assertEqual(repo.get_survey(scheduled.id).status, "collecting")


if __name__ == "__main__":
    unittest.main()
