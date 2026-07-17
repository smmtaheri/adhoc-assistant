import io
import json
import unittest
import urllib.error
from unittest import mock

from adhoc_assistant.telegram_bot.telegram import TelegramApiError, TelegramClient


class FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class TelegramClientTests(unittest.TestCase):
    def test_request_retries_after_url_error(self) -> None:
        client = TelegramClient("token")
        client.max_retries = 3

        responses = [
            urllib.error.URLError("temporary outage"),
            FakeHttpResponse({"ok": True, "result": {"id": 1}}),
        ]

        def fake_urlopen(*args, **kwargs):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with (
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen) as urlopen_mock,
            mock.patch("time.sleep") as sleep_mock,
        ):
            payload = client.request("getMe", {})

        self.assertEqual(payload["result"]["id"], 1)
        self.assertEqual(urlopen_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1)

    def test_send_message_does_not_retry_ambiguous_network_errors(self) -> None:
        client = TelegramClient("token")
        client.max_retries = 3

        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("timed out"),
            ) as urlopen_mock,
            mock.patch("time.sleep") as sleep_mock,
        ):
            with self.assertRaises(TelegramApiError):
                client.request("sendMessage", {"chat_id": 1, "text": "hi"})

        self.assertEqual(urlopen_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_request_retries_after_retry_after_error(self) -> None:
        client = TelegramClient("token")
        error = urllib.error.HTTPError(
            url="https://api.telegram.org/bottoken/sendMessage",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests: retry later",
                        "parameters": {"retry_after": 2},
                    }
                ).encode("utf-8")
            ),
        )

        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=[error, FakeHttpResponse({"ok": True, "result": {"ok": True}})],
            ) as urlopen_mock,
            mock.patch("time.sleep") as sleep_mock,
        ):
            payload = client.request("sendMessage", {"chat_id": 1, "text": "hi"})

        self.assertTrue(payload["result"]["ok"])
        self.assertEqual(urlopen_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    def test_ignorable_error_marks_exception(self) -> None:
        client = TelegramClient("token")

        with mock.patch(
            "urllib.request.urlopen",
            return_value=FakeHttpResponse(
                {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: query is too old and response timeout expired or query ID is invalid",
                }
            ),
        ):
            with self.assertRaises(TelegramApiError) as context:
                client.request("answerCallbackQuery", {"callback_query_id": "1"})

        self.assertTrue(context.exception.ignorable)


if __name__ == "__main__":
    unittest.main()
