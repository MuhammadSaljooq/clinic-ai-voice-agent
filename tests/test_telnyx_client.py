"""Telnyx REST client, driven by httpx.MockTransport. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from clinic_agent.telephony.telnyx_client import TelnyxClient, TelnyxError


def recording_client():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"result": "ok"}})

    return TelnyxClient("key-abc", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))), seen


async def test_answer_opens_a_bidirectional_l16_stream():
    """These four parameters are the entire audio negotiation; if any is wrong the
    bridge receives nothing or receives an unplayable codec."""
    client, seen = recording_client()
    await client.answer_with_stream("ccid-1", stream_url="wss://example.test/telnyx/stream")

    request = seen[0]
    assert request.url.path == "/v2/calls/ccid-1/actions/answer"
    body = json.loads(request.content)
    assert body == {
        "stream_url": "wss://example.test/telnyx/stream",
        "stream_track": "inbound_track",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "L16",
    }


async def test_requests_are_authenticated_as_bearer():
    client, seen = recording_client()
    await client.hangup("ccid-1")
    assert seen[0].headers["authorization"] == "Bearer key-abc"


async def test_transfer_sends_the_destination():
    client, seen = recording_client()
    await client.transfer("ccid-1", to="+15551234567")
    assert seen[0].url.path == "/v2/calls/ccid-1/actions/transfer"
    assert json.loads(seen[0].content) == {"to": "+15551234567"}


async def test_hangup_posts_to_the_hangup_action():
    client, seen = recording_client()
    await client.hangup("ccid-7")
    assert seen[0].url.path == "/v2/calls/ccid-7/actions/hangup"


async def test_speak_is_available_as_a_fallback_when_gemini_cannot_start():
    client, seen = recording_client()
    await client.speak("ccid-1", text="Sorry, we're having trouble.")
    assert seen[0].url.path == "/v2/calls/ccid-1/actions/speak"
    assert json.loads(seen[0].content)["payload"].startswith("Sorry")


async def test_send_sms_posts_a_message():
    client, seen = recording_client()
    await client.send_sms(to="+15550100", from_="+15550222", text="Reminder")
    assert seen[0].url.path == "/v2/messages"
    assert json.loads(seen[0].content) == {
        "to": "+15550100", "from": "+15550222", "text": "Reminder",
    }


async def test_an_error_response_raises_with_the_status_and_body():
    """The message needs to say what Telnyx actually complained about."""
    def handler(_request):
        return httpx.Response(422, text='{"errors":[{"detail":"stream_url is invalid"}]}')

    client = TelnyxClient("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(TelnyxError, match="422"):
        await client.answer_with_stream("ccid-1", stream_url="not-a-url")


async def test_an_empty_success_body_is_tolerated():
    def handler(_request):
        return httpx.Response(200, content=b"")

    client = TelnyxClient("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.hangup("ccid-1") == {}
