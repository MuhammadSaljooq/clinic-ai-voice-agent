"""Inbound SMS handling. Opt-out is a legal requirement, so it gets the most tests."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from clinic_agent.config import load_config
from clinic_agent.db.seed import seed_from_config
from clinic_agent.messaging.inbound import handle_inbound, normalise

REPO = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
PATIENT = "+15550100"


@pytest.fixture
async def clinic_db(pool):
    cfg = load_config(REPO / "config.yaml")
    await seed_from_config(pool, cfg)
    return pool, cfg


async def add_appointment(pool, *, hours=20, phone=PATIENT):
    patient_id = await pool.fetchval(
        "INSERT INTO patients (name, phone) VALUES ('Ada', $1)"
        " ON CONFLICT (phone) DO UPDATE SET name='Ada' RETURNING id",
        phone,
    )
    starts = NOW + timedelta(hours=hours)
    return await pool.fetchval(
        "INSERT INTO appointments (provider_id, appointment_type_id, patient_id,"
        " starts_at, ends_at, blocked_range) VALUES (1,1,$1,$2,$3,tstzrange($2,$3))"
        " RETURNING id",
        patient_id, starts, starts + timedelta(minutes=15),
    )


async def opted_out(pool, phone=PATIENT):
    return await pool.fetchval("SELECT sms_opted_out FROM patients WHERE phone=$1", phone)


# --- normalisation -------------------------------------------------------------


@pytest.mark.parametrize("raw", ["STOP", "stop", " Stop ", "STOP.", "stop!", "Stop\n"])
def test_opt_out_is_recognised_however_it_is_typed(raw):
    assert normalise(raw) == "STOP"


# --- opt out / in --------------------------------------------------------------


@pytest.mark.parametrize("word", ["STOP", "stop", "UNSUBSCRIBE", "END", "QUIT", "STOPALL"])
async def test_every_standard_optout_keyword_works(clinic_db, word):
    pool, cfg = clinic_db
    result = await handle_inbound(pool, cfg, from_number=PATIENT, text=word, now=NOW)
    assert result.action == "opted_out"
    assert await opted_out(pool) is True
    assert "START" in result.reply


async def test_cancel_the_word_opts_out_but_explains_how_to_cancel_a_booking(clinic_db):
    """Carriers require CANCEL to opt out, but a patient means their appointment.
    Honour compliance, then remove the ambiguity in the reply."""
    pool, cfg = clinic_db
    result = await handle_inbound(pool, cfg, from_number=PATIENT, text="cancel", now=NOW)
    assert result.action == "opted_out"
    assert await opted_out(pool) is True
    assert "reply C" in result.reply


async def test_start_resumes_messages(clinic_db):
    pool, cfg = clinic_db
    await handle_inbound(pool, cfg, from_number=PATIENT, text="STOP", now=NOW)
    result = await handle_inbound(pool, cfg, from_number=PATIENT, text="START", now=NOW)
    assert result.action == "opted_in"
    assert await opted_out(pool) is False


async def test_optout_from_an_unknown_number_is_still_honoured(clinic_db):
    """Otherwise a later booking under that number would text someone who said stop."""
    pool, cfg = clinic_db
    await handle_inbound(pool, cfg, from_number="+15559999", text="STOP", now=NOW)
    assert await opted_out(pool, "+15559999") is True


# --- cancelling by reply -------------------------------------------------------


async def test_c_cancels_the_soonest_appointment(clinic_db):
    pool, cfg = clinic_db
    soon = await add_appointment(pool, hours=20)
    later = await add_appointment(pool, hours=48)

    result = await handle_inbound(pool, cfg, from_number=PATIENT, text="C", now=NOW)

    assert result.action == "cancelled"
    assert await pool.fetchval("SELECT status::text FROM appointments WHERE id=$1", soon) == "cancelled"
    assert await pool.fetchval("SELECT status::text FROM appointments WHERE id=$1", later) == "booked", (
        "one letter should not wipe out every future appointment"
    )


async def test_c_with_nothing_booked_replies_helpfully(clinic_db):
    pool, cfg = clinic_db
    result = await handle_inbound(pool, cfg, from_number=PATIENT, text="C", now=NOW)
    assert result.action == "no_appointment"
    assert cfg.clinic.phone_display in result.reply


async def test_c_does_not_opt_the_patient_out(clinic_db):
    pool, cfg = clinic_db
    await add_appointment(pool)
    await handle_inbound(pool, cfg, from_number=PATIENT, text="C", now=NOW)
    assert await opted_out(pool) is False


# --- everything else -----------------------------------------------------------


async def test_help_explains_the_options(clinic_db):
    pool, cfg = clinic_db
    result = await handle_inbound(pool, cfg, from_number=PATIENT, text="HELP", now=NOW)
    assert result.action == "help"
    assert "STOP" in result.reply


async def test_an_unrecognised_message_changes_nothing(clinic_db):
    pool, cfg = clinic_db
    appointment = await add_appointment(pool)
    result = await handle_inbound(
        pool, cfg, from_number=PATIENT, text="can I move this to next week?", now=NOW
    )
    assert result.action == "unrecognised"
    assert await opted_out(pool) is False
    assert await pool.fetchval("SELECT status::text FROM appointments WHERE id=$1", appointment) == "booked"


async def test_a_message_with_no_sender_is_ignored(clinic_db):
    pool, cfg = clinic_db
    result = await handle_inbound(pool, cfg, from_number="", text="STOP", now=NOW)
    assert result.action == "ignored"
    assert result.reply is None
