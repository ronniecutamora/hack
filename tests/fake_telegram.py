"""A fake Telegram Bot API behind python-telegram-bot's own request seam: no network, and every
call is recorded (method name and parameters)."""

import json
from typing import Any

from telegram.request import BaseRequest, RequestData

BOT_USER = {"id": 7000001, "is_bot": True, "first_name": "Ulat test", "username": "UlatTestBot"}


class FakeTelegram(BaseRequest):
    def __init__(self, *, webhook_url: str = "", get_me_status: int = 200) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.webhook_url = webhook_url
        self.get_me_status = get_me_status
        self._message_id = 100

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 1.0

    def sent_texts(self) -> list[str]:
        return [params["text"] for name, params in self.calls if name == "sendMessage"]

    def _result(self, name: str, params: dict[str, Any]) -> Any:
        if name == "getMe":
            return BOT_USER
        if name == "sendMessage":
            self._message_id += 1
            return {
                "message_id": self._message_id,
                "date": 1_790_000_000,
                "chat": {"id": params["chat_id"], "type": "private"},
                "text": params["text"],
            }
        if name == "getWebhookInfo":
            return {
                "url": self.webhook_url,
                "has_custom_certificate": False,
                "pending_update_count": 0,
            }
        if name == "setWebhook":
            self.webhook_url = params["url"]
            return True
        if name == "deleteWebhook":
            self.webhook_url = ""
            return True
        raise AssertionError(f"unexpected Bot API call {name}")

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe" and self.get_me_status != 200:
            body = {"ok": False, "error_code": self.get_me_status, "description": "Unauthorized"}
            return self.get_me_status, json.dumps(body).encode()
        return 200, json.dumps({"ok": True, "result": self._result(name, params)}).encode()


def text_update(text: str, *, update_id: int = 1, user_id: int = 4242) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_790_000_000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Reporter"},
            "text": text,
        },
    }
