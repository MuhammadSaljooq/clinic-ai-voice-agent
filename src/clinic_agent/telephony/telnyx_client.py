"""Telnyx Call Control and Messaging REST client.

Thin on purpose: we issue a handful of commands and want full control over the exact
request bodies, particularly the streaming parameters on `answer`, which is where the
bidirectional audio session is negotiated.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.telnyx.com"

STREAM_TRACK_INBOUND = "inbound_track"
BIDIRECTIONAL_MODE_RTP = "rtp"
CODEC_L16 = "L16"


class TelnyxError(Exception):
    """A Telnyx API call failed."""


class TelnyxClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._base_url}{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise TelnyxError(f"{path} failed with {response.status_code}: {response.text}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    async def answer_with_stream(
        self,
        call_control_id: str,
        *,
        stream_url: str,
        codec: str = CODEC_L16,
    ) -> dict[str, Any]:
        """Answer the call and open bidirectional audio in one command.

        Starting the stream on `answer` rather than as a follow-up `streaming_start`
        saves a round-trip, which matters: it is dead air at the very start of the call,
        before the caller has heard anything at all.
        """
        return await self._post(
            f"/v2/calls/{call_control_id}/actions/answer",
            {
                "stream_url": stream_url,
                "stream_track": STREAM_TRACK_INBOUND,
                "stream_bidirectional_mode": BIDIRECTIONAL_MODE_RTP,
                "stream_bidirectional_codec": codec,
            },
        )

    async def transfer(self, call_control_id: str, *, to: str) -> dict[str, Any]:
        return await self._post(f"/v2/calls/{call_control_id}/actions/transfer", {"to": to})

    async def hangup(self, call_control_id: str) -> dict[str, Any]:
        return await self._post(f"/v2/calls/{call_control_id}/actions/hangup", {})

    async def speak(self, call_control_id: str, *, text: str, voice: str = "female") -> dict[str, Any]:
        """Fallback speech via Telnyx TTS, for when the Gemini session cannot start."""
        return await self._post(
            f"/v2/calls/{call_control_id}/actions/speak",
            {"payload": text, "voice": voice, "language": "en-US"},
        )

    async def send_sms(self, *, to: str, from_: str, text: str) -> dict[str, Any]:
        return await self._post("/v2/messages", {"to": to, "from": from_, "text": text})
