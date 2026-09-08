"""Gemini Live session configuration.

Two things here are legal requirements rather than product choices: the AI
disclosure in the greeting (CA AB 2905, within 15 seconds) and the ban on an AI
implying healthcare credentials or giving clinical advice (CA AB 489). Both are
asserted so a future prompt edit cannot quietly drop them.
"""

from __future__ import annotations

import pathlib

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

def test_instruction_requires_ai_disclosure_in_the_greeting():
    text = build_system_instruction(CFG).lower()
    assert "ai" in text
    assert "first" in text or "greeting" in text
    assert "disclos" in text or "tell them" in text or "say" in text


def test_instruction_forbids_medical_advice():
    text = build_system_instruction(CFG).lower()
    assert "medical advice" in text
    assert "symptom" in text


def test_instruction_forbids_claiming_clinical_credentials():
    """CA AB 489: an AI must not imply it holds healthcare credentials."""
    text = build_system_instruction(CFG).lower()
    assert "nurse" in text or "doctor" in text or "credential" in text


def test_instruction_routes_emergencies_to_911():
    assert "911" in build_system_instruction(CFG)


def test_instruction_admits_to_being_ai_when_asked():
    """Utah AI Policy Act: must disclose on request."""
    text = build_system_instruction(CFG).lower()
    assert "asks" in text or "ask" in text


def test_instruction_forbids_inventing_availability():
    text = build_system_instruction(CFG).lower()
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
