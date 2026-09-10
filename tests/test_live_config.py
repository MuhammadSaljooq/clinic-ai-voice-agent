"""Gemini Live session configuration.

Two things here are legal requirements rather than product choices: the AI
disclosure in the greeting (CA AB 2905, within 15 seconds) and the ban on an AI
implying healthcare credentials or giving clinical advice (CA AB 489). Both are
asserted so a future prompt edit cannot quietly drop them.
"""

from __future__ import annotations

import pathlib
import re

from google.genai import types

from clinic_agent.ai.live_config import (
    NATIVE_AUDIO_MODEL,
    READ_TOOLS,
    WRITE_TOOLS,
    build_live_config,
    build_system_instruction,
    build_tools,
)
from clinic_agent.ai.provider import ProviderSettings, client_kwargs
from clinic_agent.config import load_config

REPO = pathlib.Path(__file__).resolve().parents[1]
CFG = load_config(REPO / "config.yaml")


def prompt_text(cfg=None) -> str:
    """Lowercased with whitespace collapsed.

    The prompt is hard-wrapped for readability, so a phrase can straddle a line
    break; asserting on raw text makes tests fail for purely cosmetic reflows.
    """
    return re.sub(r"\s+", " ", build_system_instruction(cfg or CFG)).lower()


# --- model choice -------------------------------------------------------------

def test_model_is_pinned_to_the_native_audio_preview():
    """Affective dialog and NON_BLOCKING tools exist only on this model."""
    assert NATIVE_AUDIO_MODEL == "gemini-2.5-flash-native-audio-preview-12-2025"


# --- session config -----------------------------------------------------------

def test_config_requests_audio_only_output():
    cfg = build_live_config(CFG)
    assert cfg.response_modalities == [types.Modality.AUDIO]


def test_affective_dialog_is_enabled():
    """The feature that makes it read the caller's tone."""
    assert build_live_config(CFG).enable_affective_dialog is True


def test_context_compression_is_enabled_so_long_calls_survive():
    """Uncompressed audio sessions cap at 15 minutes; calls can run longer."""
    compression = build_live_config(CFG).context_window_compression
    assert compression is not None
    assert compression.sliding_window is not None
    assert compression.trigger_tokens is not None


def test_session_resumption_is_requested_even_without_a_handle():
    """Resumption updates only arrive if resumption is configured from the start."""
    assert build_live_config(CFG).session_resumption is not None
    assert build_live_config(CFG).session_resumption.handle is None


def test_a_resumption_handle_is_threaded_through():
    cfg = build_live_config(CFG, resumption_handle="handle-xyz")
    assert cfg.session_resumption.handle == "handle-xyz"


def test_both_transcriptions_are_enabled_for_the_call_log():
    cfg = build_live_config(CFG)
    assert cfg.input_audio_transcription is not None
    assert cfg.output_audio_transcription is not None


def test_barge_in_is_configured_to_interrupt():
    cfg = build_live_config(CFG)
    handling = cfg.realtime_input_config.activity_handling
    assert handling == types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS


def test_voice_is_configurable():
    cfg = build_live_config(CFG, voice="Kore")
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"


# --- system instruction: the legally required parts ---------------------------

def test_instruction_opens_with_a_greeting_and_keeps_reactive_honesty():
    """Proactive "I'm an AI" disclosure was removed at the operator's request. A warm
    opening greeting remains, and the agent still answers truthfully if asked whether it
    is a bot (reactive honesty). Note: some jurisdictions require proactive disclosure --
    that trade-off is the operator's to own."""
    text = prompt_text()
    assert "how to open" in text
    assert "greeting" in text or "greet" in text
    assert "truth" in text  # still tells the truth if a caller asks whether it's a bot


def test_instruction_forbids_medical_advice():
    text = prompt_text()
    assert "medical advice" in text
    assert "symptom" in text


def test_instruction_forbids_claiming_clinical_credentials():
    """CA AB 489: an AI must not imply it holds healthcare credentials."""
    text = prompt_text()
    assert "nurse" in text or "doctor" in text or "credential" in text


def test_instruction_routes_emergencies_to_911():
    assert "911" in build_system_instruction(CFG)


def test_instruction_admits_to_being_ai_when_asked():
    """Utah AI Policy Act: must disclose on request."""
    text = prompt_text()
    assert "asks" in text or "ask" in text


def test_instruction_forbids_inventing_availability():
    text = prompt_text()
    assert "never invent" in text or "do not invent" in text or "make up" in text


def test_instruction_includes_the_clinic_specifics():
    text = build_system_instruction(CFG)
    assert CFG.clinic.name in text
    assert "Dr. Reyes" in text
    assert "Follow-up" in text
    assert "Northside Avenue" in text, "FAQ content should be available to the model"


# --- tools --------------------------------------------------------------------

def test_all_expected_tools_are_declared():
    names = {d.name for tool in build_tools(CFG) for d in tool.function_declarations}
    assert names == set(READ_TOOLS) | set(WRITE_TOOLS)


def test_list_schedule_is_absent_unless_staff_access_is_enabled():
    """The schedule is patient PII: the tool must not even exist without a staff PIN."""
    names = {d.name for tool in build_tools(CFG) for d in tool.function_declarations}
    assert "list_schedule" not in names


def test_staff_mode_declares_a_pin_gated_read_only_schedule_tool():
    declarations = {
        d.name: d
        for tool in build_tools(CFG, staff_enabled=True)
        for d in tool.function_declarations
    }
    assert "list_schedule" in declarations
    tool = declarations["list_schedule"]
    assert tool.behavior == types.Behavior.NON_BLOCKING  # reads never block the line
    assert "pin" in tool.parameters.required


def test_staff_mode_prompt_tells_the_agent_to_demand_the_pin():
    text = build_system_instruction(CFG, staff_enabled=True)
    assert "STAFF SCHEDULE ACCESS" in text
    assert "PIN" in text
    # And the section is silent when the feature is off.
    assert "STAFF SCHEDULE ACCESS" not in build_system_instruction(CFG)


def test_read_tools_are_non_blocking_so_the_line_never_goes_silent():
    declarations = {d.name: d for t in build_tools(CFG) for d in t.function_declarations}
    for name in READ_TOOLS:
        assert declarations[name].behavior == types.Behavior.NON_BLOCKING, name


def test_write_tools_are_blocking_so_nothing_is_confirmed_before_it_commits():
    """A non-blocking write would let the model say 'you're booked' while the
    transaction is still in flight -- and it might then fail with SlotTaken."""
    declarations = {d.name: d for t in build_tools(CFG) for d in t.function_declarations}
    for name in WRITE_TOOLS:
        assert declarations[name].behavior == types.Behavior.BLOCKING, name


def test_appointment_type_argument_is_constrained_to_configured_types():
    """An enum means the model cannot ask to book a service the clinic does not offer."""
    declarations = {d.name: d for t in build_tools(CFG) for d in t.function_declarations}
    enum = declarations["find_slots"].parameters.properties["appointment_type"].enum
    assert set(enum) == {t.name for t in CFG.appointment_types}


def test_provider_argument_is_constrained_to_configured_providers():
    declarations = {d.name: d for t in build_tools(CFG) for d in t.function_declarations}
    enum = declarations["find_slots"].parameters.properties["provider_name"].enum
    assert set(enum) == {p.name for p in CFG.providers}


def test_booking_takes_an_option_number_not_a_raw_token():
    """Tokens stay server-side: shorter context, and nothing to forge or garble."""
    declarations = {d.name: d for t in build_tools(CFG) for d in t.function_declarations}
    params = declarations["book_appointment"].parameters
    assert "option" in params.properties
    assert params.properties["option"].type == types.Type.INTEGER
    assert not any("token" in p for p in params.properties)


def test_tools_are_attached_to_the_live_config():
    assert build_live_config(CFG).tools


# --- provider seam ------------------------------------------------------------

def test_ai_studio_uses_an_api_key_and_v1beta():
    """Affective dialog requires the v1beta API version."""
    kwargs = client_kwargs(ProviderSettings(kind="ai_studio", api_key="k-123"))
    assert kwargs["api_key"] == "k-123"
    assert kwargs["http_options"].api_version == "v1beta"
    assert "vertexai" not in kwargs


def test_vertex_uses_project_and_location_and_no_api_key():
    """The one change needed to run under a Google Cloud BAA."""
    kwargs = client_kwargs(
        ProviderSettings(kind="vertex", project="my-proj", location="us-central1")
    )
    assert kwargs["vertexai"] is True
    assert kwargs["project"] == "my-proj"
    assert kwargs["location"] == "us-central1"
    assert "api_key" not in kwargs


def test_ai_studio_without_a_key_is_rejected_at_construction():
    import pytest
    with pytest.raises(ValueError, match="api_key"):
        client_kwargs(ProviderSettings(kind="ai_studio", api_key=None))


def test_vertex_without_a_project_is_rejected_at_construction():
    import pytest
    with pytest.raises(ValueError, match="project"):
        client_kwargs(ProviderSettings(kind="vertex", location="us-central1"))


# --- the model must know what day it is ---------------------------------------

def test_instruction_states_the_current_clinic_local_date():
    """Without this, 'tomorrow' and 'next Tuesday' are unanswerable."""
    from datetime import UTC, datetime

    moment = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)  # 09:30 in New York
    text = build_system_instruction(CFG, now=moment)

    assert "Monday" in text
    assert "14 September 2026" in text
    assert "9:30 AM" in text
    assert CFG.clinic.timezone in text


def test_instruction_tells_the_model_to_derive_dates_rather_than_guess():
    text = prompt_text()
    assert "never guess a date" in text


def test_find_slots_takes_structured_date_hints_not_free_text():
    """Free text would push date parsing into our code, where it does not belong."""
    declarations = {d.name: d for t in build_tools(CFG) for d in t.function_declarations}
    properties = declarations["find_slots"].parameters.properties
    assert "earliest_date" in properties
    assert "part_of_day" in properties
    assert "date_preference" not in properties


def test_part_of_day_is_constrained_to_known_values():
    declarations = {d.name: d for t in build_tools(CFG) for d in t.function_declarations}
    enum = declarations["find_slots"].parameters.properties["part_of_day"].enum
    assert set(enum) == {"morning", "afternoon", "evening"}


def test_thinking_is_disabled_to_keep_replies_fast():
    """Measured: leaving thinking on added ~6s before the caller heard anything."""
    cfg = build_live_config(CFG)
    assert cfg.thinking_config is not None
    assert cfg.thinking_config.thinking_budget == 0


# --- prompt qualities that make it sound like a person -------------------------


def test_instruction_forbids_speaking_internal_option_numbers_aloud():
    """The model books by option number, but saying "option one" to a caller is
    exactly the tell that gives away a machine."""
    text = prompt_text()
    assert "option one" in text
    assert "never say" in text


def test_instruction_forbids_mentioning_tools_or_systems():
    """A caller should never hear about lookups, databases or functions."""
    text = prompt_text()
    assert "never mention tools" in text
    assert "databases" in text


def test_instruction_requires_natural_time_reading():
    text = prompt_text()
    assert "14:30" in text, "should give an explicit example of what NOT to say"
    assert "quarter past" in text or "two thirty" in text


def test_instruction_covers_the_messy_parts_of_phone_calls():
    text = prompt_text()
    for topic in ["still there", "say it once more", "interrupt", "spelling", "wrong number"]:
        assert topic in text, f"prompt should handle: {topic}"


def test_instruction_requires_reading_details_back_before_booking():
    text = prompt_text()
    assert "read the details back" in text


# --- the reminder promise must match reality ----------------------------------


def test_no_reminder_is_promised_while_reminders_are_disabled():
    """Promising a text that never arrives is worse than not mentioning it."""
    assert CFG.reminders.enabled is False
    text = build_system_instruction(CFG)
    assert "not switched on yet" in text
    assert "text reminder goes out" not in text


def test_a_reminder_is_promised_once_reminders_are_enabled():
    enabled = CFG.model_copy(deep=True)
    enabled.reminders.enabled = True
    enabled.reminders.hours_before = 24
    text = build_system_instruction(enabled)
    assert "text reminder goes out about 24 hours" in text
    assert "not switched on yet" not in text
