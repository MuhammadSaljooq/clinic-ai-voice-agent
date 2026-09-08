"""The AI provider seam.

The one place that knows whether we are talking to Google AI Studio or Vertex AI.
Moving a clinic to a HIPAA-eligible footing is a change to this file and an
environment variable -- not a rewrite -- because both providers serve the same
models through the same Live API.

**AI Studio is not BAA-eligible.** It is for development against synthetic data.
Real patient traffic requires `AI_PROVIDER=vertex` and an executed Google Cloud BAA.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from google import genai
from google.genai import types

# Affective dialog is only exposed on the v1beta API version.
REQUIRED_API_VERSION = "v1beta"

ProviderKind = Literal["ai_studio", "vertex"]


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    kind: ProviderKind = "ai_studio"
    api_key: str | None = None
    project: str | None = None
    location: str | None = None

    @property
    def is_baa_eligible(self) -> bool:
        return self.kind == "vertex"


def client_kwargs(settings: ProviderSettings) -> dict[str, Any]:
    """Translate settings into genai.Client kwargs, failing loudly if incomplete.

    Kept separate from client construction so it is testable without credentials.
    """
    if settings.kind == "ai_studio":
        if not settings.api_key:
            raise ValueError("ai_studio provider requires api_key (set GEMINI_API_KEY)")
        return {
            "api_key": settings.api_key,
            "http_options": types.HttpOptions(api_version=REQUIRED_API_VERSION),
        }

    if settings.kind == "vertex":
        if not settings.project:
            raise ValueError("vertex provider requires project (set GOOGLE_CLOUD_PROJECT)")
        if not settings.location:
            raise ValueError("vertex provider requires location (set GOOGLE_CLOUD_LOCATION)")
        return {
            "vertexai": True,
            "project": settings.project,
            "location": settings.location,
            "http_options": types.HttpOptions(api_version=REQUIRED_API_VERSION),
        }

    raise ValueError(f"unknown provider kind {settings.kind!r}")


def settings_from_env(env: dict[str, str] | None = None) -> ProviderSettings:
    source = os.environ if env is None else env
    kind = source.get("AI_PROVIDER", "ai_studio").strip().lower()
    return ProviderSettings(
        kind="vertex" if kind == "vertex" else "ai_studio",
        api_key=source.get("GEMINI_API_KEY"),
        project=source.get("GOOGLE_CLOUD_PROJECT"),
        location=source.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )


def build_client(settings: ProviderSettings | None = None) -> genai.Client:
    return genai.Client(**client_kwargs(settings or settings_from_env()))
