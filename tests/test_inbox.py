"""The operator inbox: listing threads, replying, opt-out, and webhook persistence."""

from __future__ import annotations

import base64
import json
import pathlib

import pytest
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from clinic_agent.app import AppDeps, create_app
from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config
from clinic_agent.messaging import store
from clinic_agent.telephony.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER

REPO = pathlib.Path(__file__).resolve().parents[1]
PASSWORD = "inbox-test-pw"
FORM = {"content-type": "application/x-www-form-urlencoded"}


class FakeTelnyx:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_sms(self, *, to, from_, text):
        self.sent.append({"to": to, "from": from_, "text": text})
        return {"data": {"id": f"m{len(self.sent)}"}}

    async def aclose(self):
        pass


def _signed(key, body: bytes):
    import time

    ts = str(int(time.time()))
    sig = key.sign(f"{ts}|".encode() + body).signature
    return {
        SIGNATURE_HEADER: base64.b64encode(sig).decode(),
        TIMESTAMP_HEADER: ts,
        "content-type": "application/json",
    }


@pytest.fixture
async def inbox(pool):
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    key = SigningKey.generate()
    telnyx = FakeTelnyx()
    deps = AppDeps(
        cfg=cfg,
        telnyx=telnyx,
        public_key_b64=base64.b64encode(bytes(key.verify_key)).decode(),
        stream_url="wss://x/telnyx/stream",
        connect_gemini=lambda h: None,
        pool=pool,
        sms_from_number="+15550222",
        dashboard_password=PASSWORD,
    )
    app = create_app(deps)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/login", content=f"password={PASSWORD}", headers=FORM)
        yield client, pool, cfg, telnyx, key


# --- listing + reply -----------------------------------------------------------


async def test_a_recorded_message_shows_up_in_the_inbox(inbox):
    client, pool, _cfg, _t, _k = inbox
    await store.record_inbound(pool, phone="+15550100", body="are you open Saturday?")
    body = (await client.get("/dashboard/inbox")).text
    assert "+15550100" in body
    assert "are you open Saturday?" in body


async def test_replying_sends_via_telnyx_and_is_recorded(inbox):
    client, pool, _cfg, telnyx, _k = inbox
    await store.record_inbound(pool, phone="+15550100", body="hello")

    r = await client.post(
        "/dashboard/inbox/send", content="to=%2B15550100&text=We+open+at+9", headers=FORM
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("sent=sent")

    assert telnyx.sent == [{"to": "+15550100", "from": "+15550222", "text": "We open at 9"}]
    thread = await store.fetch_thread(pool, phone="+15550100")
    assert thread.messages[-1].direction == "outbound"
    assert thread.messages[-1].body == "We open at 9"
    assert thread.messages[-1].status == "sent"


async def test_a_reply_to_an_opted_out_number_is_blocked(inbox):
    client, pool, _cfg, telnyx, _k = inbox
    await pool.execute(
        "INSERT INTO patients (name, phone, sms_opted_out) VALUES ('Ada','+15550100',TRUE)"
    )
    await store.record_inbound(pool, phone="+15550100", body="stop")

    r = await client.post(
        "/dashboard/inbox/send", content="to=%2B15550100&text=hi", headers=FORM
    )
    assert r.headers["location"].endswith("sent=blocked")
    assert telnyx.sent == [], "must not text a number that opted out"
    thread = await store.fetch_thread(pool, phone="+15550100")
    assert thread.messages[-1].status == "blocked"


async def test_an_empty_reply_is_rejected(inbox):
    client, _pool, _cfg, telnyx, _k = inbox
    r = await client.post("/dashboard/inbox/send", content="to=%2B15550100&text=", headers=FORM)
    assert r.headers["location"].endswith("sent=empty")
    assert telnyx.sent == []


# --- webhook persistence -------------------------------------------------------


async def test_inbound_webhook_persists_the_message_and_reply(inbox):
    """A patient texting HELP is recorded, and the auto-reply is recorded too."""
    client, pool, _cfg, _t, key = inbox
    body = json.dumps({
        "data": {
            "event_type": "message.received",
            "payload": {"from": {"phone_number": "+15550100"}, "text": "HELP"},
        }
    }).encode()
    r = await client.post("/telnyx/messaging", content=body, headers=_signed(key, body))
    assert r.status_code == 200

    thread = await store.fetch_thread(pool, phone="+15550100")
    kinds = [(m.direction, m.kind) for m in thread.messages]
    assert ("inbound", "sms") in kinds
    assert ("outbound", "auto_reply") in kinds


async def test_delivery_receipt_marks_the_message_delivered(inbox):
    client, pool, _cfg, _t, key = inbox
    await store.record_outbound(
        pool, phone="+15550100", body="on its way", status="sent", provider_message_id="tx-9"
    )
    body = json.dumps({
        "data": {
            "event_type": "message.finalized",
            "payload": {"id": "tx-9", "to": [{"phone_number": "+15550100", "status": "delivered"}]},
        }
    }).encode()
    r = await client.post("/telnyx/messaging", content=body, headers=_signed(key, body))
    assert r.status_code == 200
    thread = await store.fetch_thread(pool, phone="+15550100")
    assert thread.messages[-1].status == "delivered"
