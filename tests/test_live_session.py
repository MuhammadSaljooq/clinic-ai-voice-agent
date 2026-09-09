"""Voice turn-taking tuning: the lever on reply latency."""

from __future__ import annotations

import pathlib

from google.genai import types

from clinic_agent.ai.live_config import (
    DEFAULT_END_OF_SPEECH_SILENCE_MS,
    VadTuning,
    build_live_config,
)
from clinic_agent.ai.live_session import vad_from_env
from clinic_agent.config import load_config

REPO = pathlib.Path(__file__).resolve().parents[1]
CFG = load_config(REPO / "config.yaml")


def _vad(cfg):
    return cfg.realtime_input_config.automatic_activity_detection


def test_defaults_favour_a_snappy_reply():
    """The point of the change: the out-of-the-box turn-taking is responsive."""
    vad = VadTuning()
    assert vad.silence_ms == DEFAULT_END_OF_SPEECH_SILENCE_MS <= 450
    assert vad.end_sensitivity == types.EndSensitivity.END_SENSITIVITY_HIGH


def test_build_live_config_applies_the_tuning():
    vad = VadTuning(silence_ms=250, prefix_padding_ms=60,
                    end_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH)
    detection = _vad(build_live_config(CFG, vad=vad))
    assert detection.silence_duration_ms == 250
    assert detection.prefix_padding_ms == 60
    assert detection.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_HIGH


def test_env_overrides_let_a_noisy_clinic_dial_patience_back_up():
    vad = vad_from_env({"VAD_SILENCE_MS": "700", "VAD_END_SENSITIVITY": "low"})
    assert vad.silence_ms == 700
    assert vad.end_sensitivity == types.EndSensitivity.END_SENSITIVITY_LOW


def test_env_falls_back_to_defaults_and_ignores_garbage():
    vad = vad_from_env({"VAD_SILENCE_MS": "not-a-number"})
    assert vad.silence_ms == DEFAULT_END_OF_SPEECH_SILENCE_MS  # bad value ignored, not fatal
    # An unset environment yields the snappy defaults.
    empty = vad_from_env({})
    assert empty.silence_ms == DEFAULT_END_OF_SPEECH_SILENCE_MS


def test_affective_dialog_can_be_traded_for_latency():
    off = build_live_config(CFG, enable_affective_dialog=False)
    assert off.enable_affective_dialog is False


def test_barge_in_is_on_by_default_but_can_be_disabled():
    on = build_live_config(CFG)
    assert on.realtime_input_config.activity_handling == types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
    off = build_live_config(CFG, vad=VadTuning(interruptible=False))
    assert off.realtime_input_config.activity_handling == types.ActivityHandling.NO_INTERRUPTION


def test_agent_interruptible_env_flag():
    assert vad_from_env({"AGENT_INTERRUPTIBLE": "false"}).interruptible is False
    assert vad_from_env({}).interruptible is True
