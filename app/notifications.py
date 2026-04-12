from __future__ import annotations

from typing import Any

import httpx


class NotificationError(RuntimeError):
    pass


class NoopNotifier:
    async def aclose(self) -> None:
        return

    async def send_task_prompt(self, task: dict[str, Any], checkin_url: str) -> None:
        return

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        return

    async def send_message(self, text: str) -> None:
        return


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=20.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_task_prompt(self, task: dict[str, Any], checkin_url: str) -> None:
        text = (
            f"Current task: {task['name']}\n"
            f"Status: {task['status']['status']}\n"
            f"Open in ClickUp: {task['url']}\n"
            f"Quick check-in: {checkin_url}"
        )
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Continue Slice", "callback_data": f"continue:{task['id']}"},
                        {"text": "Complete Task", "callback_data": f"complete:{task['id']}"},
                    ],
                    [
                        {"text": "Switch Task", "callback_data": f"switch:{task['id']}"},
                        {"text": "Take Break", "callback_data": f"break:{task['id']}"},
                    ],
                    [
                        {"text": "Blocked", "callback_data": f"blocked:{task['id']}"},
                    ],
                    [
                        {"text": "Pulse Check-in", "url": checkin_url},
                    ],
                ]
            },
        }
        await self._post("/sendMessage", payload)

    async def answer_callback(self, callback_query_id: str, text: str) -> None:
        await self._post(
            "/answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    async def send_message(self, text: str) -> None:
        await self._post("/sendMessage", {"chat_id": self._chat_id, "text": text})

    async def _post(self, path: str, payload: dict[str, Any]) -> None:
        try:
            response = await self._client.post(path, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise NotificationError("Notifier request timed out.") from exc
        except httpx.HTTPStatusError as exc:
            raise NotificationError(f"Notifier request failed with status {exc.response.status_code}.") from exc
        except httpx.HTTPError as exc:
            raise NotificationError("Notifier request failed.") from exc
