"""Config validation. A typo must fail at startup, not mid-call with a patient waiting."""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from clinic_agent.config import ClinicConfig, load_config

REPO = pathlib.Path(__file__).resolve().parents[1]

BASE = {
    "clinic": {
        "name": "Test Clinic",
        "timezone": "America/New_York",
        "human_transfer_number": "+15551234567",
        "phone_display": "(555) 123-4567",
    },
    "providers": [
        {"id": 1, "name": "Dr. A", "availability": [{"weekday": "mon", "start": "09:00", "end": "17:00"}]}
    ],
    "appointment_types": [
        {"id": 1, "name": "Follow-up", "duration_min": 15, "providers": [1]}
    ],
}


def cfg(**overrides):
    merged = {**BASE, **overrides}
    return ClinicConfig.model_validate(merged)


def test_the_shipped_config_file_is_valid():
    """The config we actually ship must load, or the app cannot start."""
    loaded = load_config(REPO / "config.yaml")
    assert loaded.clinic.name
    assert loaded.providers
    assert loaded.appointment_types


def test_valid_config_projects_into_domain_objects():
    c = cfg()
    assert [p.name for p in c.domain_providers()] == ["Dr. A"]
    assert len(c.domain_rules()) == 1
    assert c.domain_appointment_type(1).duration_min == 15


def test_unknown_timezone_is_rejected():
    with pytest.raises(ValidationError, match="unknown timezone"):
        cfg(clinic={**BASE["clinic"], "timezone": "Mars/Olympus_Mons"})


def test_appointment_type_referencing_an_unknown_provider_is_rejected():
    with pytest.raises(ValidationError, match="unknown provider ids"):
        cfg(appointment_types=[{"id": 1, "name": "X", "duration_min": 15, "providers": [1, 99]}])


def test_overlapping_availability_for_one_provider_is_rejected():
    with pytest.raises(ValidationError, match="overlapping availability"):
        cfg(providers=[{
            "id": 1, "name": "Dr. A",
            "availability": [
                {"weekday": "mon", "start": "09:00", "end": "13:00"},
                {"weekday": "mon", "start": "12:00", "end": "17:00"},
            ],
        }])


def test_touching_availability_windows_are_allowed():
    """09:00-13:00 then 13:00-17:00 is a lunch-free split day, not an overlap."""
    c = cfg(providers=[{
        "id": 1, "name": "Dr. A",
        "availability": [
            {"weekday": "mon", "start": "09:00", "end": "13:00"},
            {"weekday": "mon", "start": "13:00", "end": "17:00"},
        ],
    }])
    assert len(c.domain_rules()) == 2


def test_same_hours_on_different_days_are_not_an_overlap():
    c = cfg(providers=[{
        "id": 1, "name": "Dr. A",
        "availability": [
            {"weekday": "mon", "start": "09:00", "end": "17:00"},
            {"weekday": "tue", "start": "09:00", "end": "17:00"},
        ],
    }])
    assert len(c.domain_rules()) == 2


def test_unknown_weekday_is_rejected():
    with pytest.raises(ValidationError, match="unknown weekday"):
        cfg(providers=[{
            "id": 1, "name": "Dr. A",
            "availability": [{"weekday": "funday", "start": "09:00", "end": "17:00"}],
        }])


def test_backwards_availability_window_is_rejected():
    with pytest.raises(ValidationError, match="must be before"):
        cfg(providers=[{
            "id": 1, "name": "Dr. A",
            "availability": [{"weekday": "mon", "start": "17:00", "end": "09:00"}],
        }])


def test_duplicate_provider_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate provider ids"):
        cfg(providers=[
            {"id": 1, "name": "Dr. A", "availability": []},
            {"id": 1, "name": "Dr. B", "availability": []},
        ])


def test_zero_duration_appointment_type_is_rejected():
    with pytest.raises(ValidationError):
        cfg(appointment_types=[{"id": 1, "name": "X", "duration_min": 0, "providers": [1]}])


def test_appointment_type_with_no_providers_is_rejected():
    with pytest.raises(ValidationError):
        cfg(appointment_types=[{"id": 1, "name": "X", "duration_min": 15, "providers": []}])
