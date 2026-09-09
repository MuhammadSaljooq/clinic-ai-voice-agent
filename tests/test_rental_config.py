"""Trailer-rental config loads and validates strictly."""

from __future__ import annotations

import pathlib
from decimal import Decimal

import pytest

from clinic_agent.rental_config import RentalConfig, load_rental_config

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_the_example_config_loads():
    cfg = load_rental_config(REPO / "trailer_config.yaml")
    assert cfg.business.name == "Ridgeline Trailer Rentals"
    assert len(cfg.trailer_types) == 2
    t = cfg.type_by_name("6x12 Utility")
    assert t is not None
    assert t.daily_rate == Decimal("45.00")
    assert t.units == 3
    assert str(cfg.tz) == "America/New_York"


def _base() -> dict:
    return {
        "business": {"name": "R", "timezone": "America/New_York",
                     "human_transfer_number": "+1", "phone_display": "x"},
        "trailer_types": [{"id": 1, "name": "A", "daily_rate": "10", "units": 1}],
    }


def test_rejects_duplicate_type_ids():
    raw = _base()
    raw["trailer_types"] = [
        {"id": 1, "name": "A", "daily_rate": "10", "units": 1},
        {"id": 1, "name": "B", "daily_rate": "10", "units": 1},
    ]
    with pytest.raises(ValueError, match="duplicate trailer type ids"):
        RentalConfig.model_validate(raw)


def test_rejects_non_positive_rate_and_zero_units():
    raw = _base()
    raw["trailer_types"] = [{"id": 1, "name": "A", "daily_rate": "0", "units": 1}]
    with pytest.raises(ValueError):
        RentalConfig.model_validate(raw)
    raw["trailer_types"] = [{"id": 1, "name": "A", "daily_rate": "10", "units": 0}]
    with pytest.raises(ValueError):
        RentalConfig.model_validate(raw)


def test_rejects_min_days_greater_than_max():
    raw = _base()
    raw["rental"] = {"min_days": 5, "max_days": 3}
    with pytest.raises(ValueError, match="min_days"):
        RentalConfig.model_validate(raw)


def test_rejects_unknown_timezone():
    raw = _base()
    raw["business"]["timezone"] = "Mars/Phobos"
    with pytest.raises(ValueError, match="unknown timezone"):
        RentalConfig.model_validate(raw)
