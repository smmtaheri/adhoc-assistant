import json
import logging
import mimetypes
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path


logger = logging.getLogger(__name__)


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        ignorable: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ignorable = ignorable


class TelegramClient:
    # Ambiguous network/5xx retries can duplicate these side effects.
    NON_IDEMPOTENT_METHODS = frozenset(
        {
            "sendMessage",
            "sendPhoto",
            "sendDocument",
            "forwardMessage",
            "copyMessage",
            "pinChatMessage",
        }
    )

    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.request_timeout_seconds = 10
        self.extra_read_timeout_seconds = 5
        self.max_retries = 3
        self.retry_backoff_seconds = 1

    def _allows_ambiguous_retry(self, method: str) -> bool:
        return method not in self.NON_IDEMPOTENT_METHODS

    def request(self, method: str, payload: dict | None = None) -> dict:
        data = urllib.parse.urlencode(payload or {}).encode()
        return self._request_json(
            method=method,
            request_or_url=f"{self.base_url}/{method}",
            data=data,
            timeout=self.request_timeout_seconds,
        )

    def multipart_request(
        self,
        method: str,
        payload: dict,
        file_field: str,
        file_path: Path,
    ) -> dict:
        boundary = "----adhoc-assistant-boundary"
        body = bytearray()

        for key, value in payload.items():
            if value is None:
                continue
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            body.extend(str(value).encode())
            body.extend(b"\r\n")

        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())

        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return self._request_json(
            method=method,
            request_or_url=request,
            data=None,
            timeout=self.request_timeout_seconds,
        )

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        started = time.monotonic()
        logger.debug("telegram.getUpdates start offset=%s timeout=%s", offset, timeout)
        payload = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            payload["offset"] = offset
        data = urllib.parse.urlencode(payload).encode()
        response = self._request_json(
            method="getUpdates",
            request_or_url=f"{self.base_url}/getUpdates",
            data=data,
            timeout=timeout + self.extra_read_timeout_seconds,
        )
        updates = response["result"]
        logger.debug(
            "telegram.getUpdates done offset=%s timeout=%s updates=%s elapsed_ms=%.1f",
            offset,
            timeout,
            len(updates),
            (time.monotonic() - started) * 1000,
        )
        return updates

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        message_thread_id: int | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self.request("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self.request("editMessageText", payload)

    def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict[str, str | int] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self.request("editMessageReplyMarkup", payload)

    def send_document(
        self,
        chat_id: int,
        document_path: Path,
        caption: str = "",
        reply_markup: dict | None = None,
        message_thread_id: int | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "caption": caption}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self.multipart_request("sendDocument", payload, "document", document_path)

    def send_photo(
        self,
        chat_id: int,
        photo_path: Path,
        caption: str = "",
        reply_markup: dict | None = None,
        message_thread_id: int | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "caption": caption}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self.multipart_request("sendPhoto", payload, "photo", photo_path)

    def pin_chat_message(
        self,
        chat_id: int,
        message_id: int,
        disable_notification: bool = True,
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": disable_notification,
        }
        return self.request("pinChatMessage", payload)

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> dict:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        return self.request("answerCallbackQuery", payload)

    def _request_json(
        self,
        *,
        method: str,
        request_or_url: urllib.request.Request | str,
        data: bytes | None,
        timeout: int,
    ) -> dict:
        last_error: TelegramApiError | None = None
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request_or_url, data=data, timeout=timeout) as response:
                    payload = json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                error = self._http_error(method, exc)
                if error.ignorable:
                    raise error from exc
                if (
                    error.retryable
                    and attempt < self.max_retries
                    and self._should_retry(method, error)
                ):
                    self._sleep_for_retry(error, attempt)
                    last_error = error
                    continue
                logger.debug(
                    "telegram.%s failed attempt=%s elapsed_ms=%.1f error=%s",
                    method,
                    attempt,
                    (time.monotonic() - started) * 1000,
                    error,
                )
                raise error from exc
            except urllib.error.URLError as exc:
                error = TelegramApiError(
                    f"Telegram network error for {method}: {exc}",
                    retryable=True,
                )
                if (
                    attempt < self.max_retries
                    and self._allows_ambiguous_retry(method)
                ):
                    self._sleep_for_retry(error, attempt)
                    last_error = error
                    continue
                logger.debug(
                    "telegram.%s failed attempt=%s elapsed_ms=%.1f error=%s",
                    method,
                    attempt,
                    (time.monotonic() - started) * 1000,
                    error,
                )
                raise error from exc

            error = self._api_error(method, payload)
            if error is None:
                logger.debug(
                    "telegram.%s ok attempt=%s elapsed_ms=%.1f",
                    method,
                    attempt,
                    (time.monotonic() - started) * 1000,
                )
                return payload
            if error.ignorable:
                logger.debug(
                    "telegram.%s ignorable_error attempt=%s elapsed_ms=%.1f error=%s",
                    method,
                    attempt,
                    (time.monotonic() - started) * 1000,
                    error,
                )
                raise error
            if (
                error.retryable
                and attempt < self.max_retries
                and self._should_retry(method, error)
            ):
                self._sleep_for_retry(error, attempt)
                last_error = error
                continue
            logger.debug(
                "telegram.%s failed attempt=%s elapsed_ms=%.1f error=%s",
                method,
                attempt,
                (time.monotonic() - started) * 1000,
                error,
            )
            raise error

        if last_error is not None:
            raise last_error
        raise TelegramApiError(f"Telegram request for {method} failed without a response.")

    def _should_retry(self, method: str, error: TelegramApiError) -> bool:
        """Retry 429 always; skip ambiguous failures for non-idempotent sends."""
        text = str(error).lower()
        if "retry_after=" in text or "too many requests" in text or " 429 " in text:
            return True
        return self._allows_ambiguous_retry(method)

    def _http_error(self, method: str, exc: urllib.error.HTTPError) -> TelegramApiError:
        body = exc.read().decode(errors="replace")
        parsed = self._parse_json(body)
        description = self._extract_description(parsed) or body
        retry_after = self._extract_retry_after(parsed)
        message = f"Telegram HTTP error for {method}: {exc.code} {description}"
        if retry_after is not None:
            message = f"{message} retry_after={retry_after}"
        return TelegramApiError(
            message,
            retryable=exc.code == 429 or 500 <= exc.code < 600,
            ignorable=self._is_ignorable_description(description),
        )

    def _api_error(self, method: str, payload: dict) -> TelegramApiError | None:
        if payload.get("ok"):
            return None

        description = self._extract_description(payload) or str(payload)
        error_code = int(payload.get("error_code", 0) or 0)
        retry_after = self._extract_retry_after(payload)
        message = f"Telegram API error for {method}: {description}"
        if retry_after is not None:
            message = f"{message} retry_after={retry_after}"
        return TelegramApiError(
            message,
            retryable=error_code == 429 or 500 <= error_code < 600,
            ignorable=self._is_ignorable_description(description),
        )

    def _sleep_for_retry(self, error: TelegramApiError, attempt: int) -> None:
        retry_after = self._extract_retry_after_from_error(error)
        if retry_after is not None:
            time.sleep(retry_after)
            return
        time.sleep(self.retry_backoff_seconds * attempt)

    def _extract_retry_after_from_error(self, error: TelegramApiError) -> int | None:
        marker = "retry_after="
        text = str(error)
        if marker not in text:
            return None
        raw = text.split(marker, maxsplit=1)[1].split()[0]
        try:
            return int(raw)
        except ValueError:
            return None

    def _extract_retry_after(self, payload: dict | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            return None
        retry_after = parameters.get("retry_after")
        if retry_after in (None, ""):
            return None
        try:
            return int(retry_after)
        except (TypeError, ValueError):
            return None

    def _extract_description(self, payload: dict | None) -> str:
        if not isinstance(payload, dict):
            return ""
        description = payload.get("description", "")
        return str(description)

    def _parse_json(self, raw_value: str) -> dict | None:
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _is_ignorable_description(self, description: str) -> bool:
        normalized = description.lower()
        return "message is not modified" in normalized or "query is too old" in normalized
