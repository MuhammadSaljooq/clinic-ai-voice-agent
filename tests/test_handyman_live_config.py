"""Handyman prompt + tools: the right tools, the never-quote rule, native-audio stripping."""

from __future__ import annotations

import pathlib

from google.genai import types

from clinic_agent.ai.handyman_live_config import (
    build_handyman_live_config,
    build_handyman_system_instruction,
    build_handyman_tools,
)
from clinic_agent.handyman_config import load_handyman_config

REPO = pathlib.Path(__file__).resolve().parents[1]
CFG = load_handyman_config(REPO / "handyman_config.yaml")

EXPECTED_TOOLS = {
    "answer_question", "request_appointment", "capture_lead", "take_message",
    "transfer_to_human",
}


def test_all_expected_tools_are_declared():
    names = {d.name for t in build_handyman_tools(CFG) for d in t.function_declarations}
    assert names == EXPECTED_TOOLS


def test_the_prompt_grounds_the_agent_in_the_business():
    text = build_handyman_system_instruction(CFG)
    assert CFG.business.name in text
    assert CFG.business.owner_name in text
    assert "Knoxville" in text


def test_the_prompt_forbids_quoting_a_price():
    """The defining rule of this agent: a handyman quotes after seeing the job."""
    text = build_handyman_system_instruction(CFG)
    assert "NEVER QUOTE A PRICE" in text
    assert "free estimate" in text.lower()


def test_the_prompt_tells_the_agent_requests_are_not_confirmed_bookings():
    text = build_handyman_system_instruction(CFG)
    assert "REQUEST" in text
    assert "booked" in text.lower()  # ...specifically, "never say it's booked"


def test_native_audio_tools_are_non_blocking_reads_and_blocking_writes():
    decls = {d.name: d for t in build_handyman_tools(CFG) for d in t.function_declarations}
    assert decls["answer_question"].behavior == types.Behavior.NON_BLOCKING
    assert decls["request_appointment"].behavior == types.Behavior.BLOCKING


def test_half_cascade_config_strips_native_only_features():
    tools = build_handyman_tools(CFG, native_audio=False)
    assert all(d.behavior is None for t in tools for d in t.function_declarations)
    cfg = build_handyman_live_config(CFG, native_audio=False)
    assert cfg.enable_affective_dialog is None
    assert cfg.thinking_config is None


def test_request_appointment_requires_name_phone_and_email():
    decls = {d.name: d for t in build_handyman_tools(CFG) for d in t.function_declarations}
    assert set(decls["request_appointment"].parameters.required) == {"name", "phone", "email"}
    assert "email" in decls["request_appointment"].parameters.properties
