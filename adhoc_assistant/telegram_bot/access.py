"""Allowlist-only Telegram identity binding for real bot users.

Debug local-only / synthetic participants never call these helpers — they are
survey roster entries only and do not authenticate through Telegram DMs.
"""

from __future__ import annotations

from adhoc_assistant.telegram_bot.messages import BotMessages
from adhoc_assistant.telegram_bot.repository import BotRepository, normalize_username
from adhoc_assistant.telegram_bot.settings import Member


def telegram_display_name(user: dict | None) -> str:
    if not user:
        return ""
    first_name = str(user.get("first_name", "")).strip()
    last_name = str(user.get("last_name", "")).strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if full_name:
        return full_name
    return normalize_username(str(user.get("username", "")))


def access_denied_message(
    repo: BotRepository,
    messages: BotMessages,
    *,
    telegram_id: int,
    username: str = "",
) -> str:
    """Pick unauthorized vs inactive messaging for a denied Telegram user."""
    existing = repo.get_user_by_telegram_id(int(telegram_id))
    if existing is not None and not existing["active"]:
        return messages.not_active_member

    username = normalize_username(username)
    if username:
        by_username = repo.get_user_by_username(username)
        if by_username is not None and not by_username["active"]:
            return messages.not_active_member

    return messages.unauthorized


def resolve_allowlisted_member(
    repo: BotRepository,
    *,
    telegram_id: int,
    username: str = "",
    display_name: str = "",
) -> Member | None:
    """Resolve an active allowlisted member for a real Telegram user.

    Rules:
    - telegram_id already bound to an active bot_users row => allow (mark seen)
    - otherwise username must exist, be active, and pass safe first-bind rules
    - missing / inactive / unsafe rebind => deny (None)
    """
    telegram_id = int(telegram_id)
    if telegram_id <= 0:
        return None

    existing = repo.get_user_by_telegram_id(telegram_id)
    if existing is not None:
        if not existing["active"]:
            return None
        repo.mark_user_seen(telegram_id, display_name)
        refreshed = repo.get_user_by_telegram_id(telegram_id) or existing
        return repo.user_to_member(refreshed)

    username = normalize_username(username)
    if not username:
        return None

    authorized = repo.authorize_user_from_start(username, telegram_id, display_name)
    if authorized is None:
        return None
    return repo.user_to_member(authorized)
