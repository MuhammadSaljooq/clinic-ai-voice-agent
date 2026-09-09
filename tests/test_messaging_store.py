"""The SMS message log that backs the inbox, against real Postgres."""

from __future__ import annotations

import pathlib

import pytest

from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config
from clinic_agent.messaging import store

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
async def db(pool):
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    return pool


async def test_inbound_and_outbound_land_in_one_thread(db):
    await store.record_inbound(db, phone="+15550100", body="hi there")
    await store.record_outbound(db, phone="+15550100", body="hello back", status="sent")

    thread = await store.fetch_thread(db, phone="+15550100")
    assert [m.direction for m in thread.messages] == ["inbound", "outbound"]
    assert thread.messages[0].body == "hi there"
    assert thread.messages[1].status == "sent"


async def test_threads_are_grouped_by_number_newest_first(db):
    await store.record_inbound(db, phone="+15550001", body="first person")
    await store.record_inbound(db, phone="+15550002", body="second person")
    await store.record_outbound(db, phone="+15550001", body="reply", status="sent")

    threads = await store.list_threads(db)
    # +15550001 got the most recent message, so it leads.
    assert threads[0].phone == "+15550001"
    assert threads[0].last_direction == "outbound"
    assert threads[0].total == 2
    assert {t.phone for t in threads} == {"+15550001", "+15550002"}


async def test_a_thread_knows_the_patient_name_and_optout(db):
    await db.execute(
        "INSERT INTO patients (name, phone, sms_opted_out) VALUES ('Ada', '+15550100', TRUE)"
    )
    await store.record_inbound(db, phone="+15550100", body="stop please")
    thread = await store.fetch_thread(db, phone="+15550100")
    assert thread.name == "Ada"
    assert thread.opted_out is True


async def test_delivery_receipts_update_the_matching_message(db):
    await store.record_outbound(
        db, phone="+15550100", body="on its way", status="sent", provider_message_id="tx-1"
    )
    matched = await store.mark_delivery(db, provider_message_id="tx-1", status="delivered")
    assert matched is True

    thread = await store.fetch_thread(db, phone="+15550100")
    assert thread.messages[0].status == "delivered"


async def test_a_receipt_for_an_unknown_message_is_ignored(db):
    assert await store.mark_delivery(db, provider_message_id="nope", status="delivered") is False


async def test_awaiting_reply_counts_only_threads_ending_inbound(db):
    await store.record_inbound(db, phone="+15550001", body="need help")          # awaiting
    await store.record_inbound(db, phone="+15550002", body="hi")
    await store.record_outbound(db, phone="+15550002", body="answered", status="sent")  # not
    assert await store.count_awaiting_reply(db) == 1
