"""Interactive .env filler.

Prompts for the values that must come from you, validates each one, and writes them
into .env. Secrets are read with getpass so they are never echoed to the screen and
never land in shell history.

Run:  .venv/bin/python scripts/set_env.py
"""

from __future__ import annotations

import base64
import binascii
import getpass
import pathlib
import re
import sys

ENV = pathlib.Path(__file__).resolve().parents[1] / ".env"


def check_telnyx_api_key(value: str) -> str | None:
    if not value.startswith("KEY"):
        return "Telnyx API keys start with 'KEY'. Did you paste the Public Key by mistake?"
    if len(value) < 30:
        return f"looks too short ({len(value)} chars) for a Telnyx API key"
    return None


def check_telnyx_public_key(value: str) -> str | None:
    if value.startswith("KEY"):
        return "That's the API Key, not the Public Key. The Public Key is short base64."
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return "not valid base64 — copy the Public Key exactly as shown"
    if len(raw) != 32:
        return f"Ed25519 public keys decode to 32 bytes, this one is {len(raw)}"
    return None


def check_gemini_key(value: str) -> str | None:
    if len(value) < 20:
        return f"looks too short ({len(value)} chars) for a Gemini API key"
    return None


def check_number(value: str) -> str | None:
    if not re.fullmatch(r"\+\d{8,15}", value):
        return "must be E.164: a leading + then digits only, e.g. +15551234567"
    return None


FIELDS = [
    ("GEMINI_API_KEY", "Gemini API key (aistudio.google.com/apikey)", True, check_gemini_key),
    ("TELNYX_API_KEY", "Telnyx API key (starts with KEY)", True, check_telnyx_api_key),
    ("TELNYX_PUBLIC_KEY", "Telnyx Public Key (short base64)", False, check_telnyx_public_key),
    ("TELNYX_NUMBER", "Your Telnyx number, E.164 e.g. +15551234567", False, check_number),
]


def read_env() -> list[str]:
    if not ENV.exists():
        sys.exit(f"{ENV} not found -- run from the project root")
    return ENV.read_text().splitlines(keepends=True)


def current(lines: list[str], key: str) -> str:
    for line in lines:
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def set_value(lines: list[str], key: str, value: str) -> list[str]:
    out, seen = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}\n")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}\n")
    return out


def main() -> None:
    lines = read_env()
    print(f"Filling {ENV}\nPress Enter to keep an existing value. Ctrl+C to stop.\n")

    for key, label, secret, validate in FIELDS:
        existing = current(lines, key)
        status = f" [currently set, {len(existing)} chars]" if existing else ""
        while True:
            prompt = f"{key} -- {label}{status}\n> "
            value = (getpass.getpass(prompt) if secret else input(prompt)).strip()

            if not value:
                if existing:
                    print("  kept existing\n")
                    break
                print("  required, please paste it\n")
                continue

            problem = validate(value)
            if problem:
                # Fail loudly here rather than at 2am on a real call.
                print(f"  rejected: {problem}\n")
                continue

            lines = set_value(lines, key, value)
            print(f"  set ({len(value)} chars)\n")
            break

    ENV.write_text("".join(lines))

    print("Done. Status:")
    for key, *_ in FIELDS:
        value = current(lines, key)
        print(f"  {key:<20} {'set' if value else 'EMPTY'}")
    print("\nNothing was printed to your screen or shell history.")


if __name__ == "__main__":
    main()
