"""Handyman config: strict validation at load, and the shipped yaml is valid."""

from __future__ import annotations

import pathlib

import pytest

from clinic_agent.handyman_config import HandymanConfig, load_handyman_config

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_the_shipped_config_loads():
    cfg = load_handyman_config(REPO / "handyman_config.yaml")
    assert cfg.business.name
    assert cfg.business.owner_name
    assert cfg.services, "at least one service category is required"
    assert cfg.tz.key == cfg.business.timezone


def test_a_bad_timezone_is_rejected_at_load():
    with pytest.raises(ValueError):
        HandymanConfig.model_validate(
            {
                "business": {
                    "name": "X", "owner_name": "Y", "timezone": "Mars/Phobos",
                    "service_area": "Nowhere", "human_transfer_number": "+1",
                    "phone_display": "1",
                },
                "services": [{"name": "Repairs"}],
            }
        )


def test_at_least_one_service_is_required():
    with pytest.raises(ValueError):
        HandymanConfig.model_validate(
            {
                "business": {
                    "name": "X", "owner_name": "Y", "timezone": "America/New_York",
                    "service_area": "Knoxville", "human_transfer_number": "+1",
                    "phone_display": "1",
                },
                "services": [],
            }
        )
