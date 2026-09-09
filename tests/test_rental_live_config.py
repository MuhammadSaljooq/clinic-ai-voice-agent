"""Rental prompt + tool declarations."""

from __future__ import annotations

import pathlib

from google.genai import types

from clinic_agent.ai.rental_live_config import (
    build_rental_live_config,
    build_rental_system_instruction,
    build_rental_tools,
)
from clinic_agent.rental_config import load_rental_config

REPO = pathlib.Path(__file__).resolve().parents[1]
CFG = load_rental_config(REPO / "trailer_config.yaml")


def test_prompt_has_the_rental_persona_and_guardrails():
    text = build_rental_system_instruction(CFG).lower()
    assert "trailer" in text
    assert "never invent" in text
    assert "driver's license" in text
    assert "transfer" in text or "put them through" in text
    assert "6x12 utility" in text            # trailer types listed
    assert "45/day" in text or "$45" in text  # rate quoted from config


def test_tools_declare_the_rental_actions_with_a_type_enum():
    decls = {d.name: d for t in build_rental_tools(CFG) for d in t.function_declarations}
    assert set(decls) == {
        "find_available_trailers", "book_rental", "lookup_rental",
        "cancel_rental", "answer_faq", "transfer_to_human",
    }
    enum = decls["find_available_trailers"].parameters.properties["trailer_type"].enum
    assert set(enum) == {"6x12 Utility", "7x14 Enclosed Cargo"}
    assert set(decls["book_rental"].parameters.required) == {"option", "first_name", "last_name", "phone"}


def test_live_config_sets_voice_and_audio_output():
    cfg = build_rental_live_config(CFG, voice="Puck")
    assert cfg.response_modalities == [types.Modality.AUDIO]
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Puck"
    assert cfg.tools
