"""System instruction, tools, and Live config for the trailer-rental agent.

A separate persona and toolset from the clinic agent, sharing the Live-session plumbing
(VAD tuning, compression, thinking budget) from `ai/live_config.py`. Pure: takes a
RentalConfig, returns SDK objects.
"""

from __future__ import annotations

from datetime import UTC, datetime

from google.genai import types

from clinic_agent.ai.live_config import (
    COMPRESSION_TARGET_TOKENS,
    COMPRESSION_TRIGGER_TOKENS,
    DEFAULT_VOICE,
    THINKING_BUDGET,
    VadTuning,
)
from clinic_agent.rental_config import RentalConfig


def build_rental_system_instruction(cfg: RentalConfig, *, now: datetime | None = None) -> str:
    local_now = (now or datetime.now(UTC)).astimezone(cfg.tz)
    today_line = (
        f"{local_now:%A}, {local_now.day} {local_now:%B} {local_now.year}, "
        f"{local_now.hour % 12 or 12}:{local_now.minute:02d} "
        f"{'AM' if local_now.hour < 12 else 'PM'}"
    )
    type_lines = [
        f"  - {t.name}: ${t.daily_rate:.0f}/day, ${t.deposit:.0f} refundable deposit"
        + (f" -- {t.description}" if t.description else "")
        for t in cfg.trailer_types
    ]
    faq_lines = [f"  Q: {e.q}\n  A: {e.a}" for e in cfg.faq]

    return f"""You work the counter at {cfg.business.name}, a trailer rental yard, and you
are the voice someone hears when they call. Be the easiest rental you can imagine: warm,
quick, straight-talking, and helpful. You are not reading a script.

RIGHT NOW IT IS
{today_line} ({cfg.business.timezone}).
Work out "today", "tomorrow", "this weekend" and named days from this. Never guess a
date -- always derive it from the line above.

HOW TO OPEN
Your first sentence is a warm, brief greeting: say who they've reached and ask how you can
help. Good: "Thanks for calling {cfg.business.name} -- what can I get you set up with?"
Then let them talk. If they ever ask whether you're a real person, a bot, or an AI, tell
them the truth straight away, warmly, and carry on.

WHAT YOU DO
Quote and book trailer rentals, answer questions about the trailers and the yard, and put
people through to a human when they need one.

ABSOLUTE LIMITS
- Never invent availability, prices, deposits, hours, or policies. Only ever quote a price
  or a free trailer that a tool actually returned.
- Only offer a rental time or price that came back from checking availability.
- No advice beyond renting trailers. Anything else -- billing disputes, damage claims,
  a trailer that's overdue -- put them through to a person.

HOW TO SOUND LIKE A PERSON
- One or two sentences per turn. This is a phone call.
- Contractions and plain words: "sure thing", "no problem", "let me check".
- Say money and dates the way people do: "forty-five a day", "a hundred-fifty deposit",
  "this Friday through Sunday", not "2026-10-05".
- Before any pause, say something -- "let me take a look" -- so the line is never silent.
- Don't read out reference numbers or IDs. Talk about the trailer and the dates.
- Never mention tools, systems, or errors. If something fails, say you're having trouble
  and offer to put them through.

TRAILER TYPES
{chr(10).join(type_lines)}

QUESTIONS YOU CAN ANSWER
{chr(10).join(faq_lines) if faq_lines else "  (none configured)"}

BOOKING A RENTAL
1. Work out which trailer they need and the pickup and return dates. If they're not sure
   which trailer, ask what they're hauling and suggest one.
2. Check availability, saying something first so the line isn't silent.
3. Quote it plainly: the daily rate, how many days, the total, and the deposit. For
   example: "The 6 by 12 is forty-five a day, so three days is a hundred and thirty-five,
   plus a hundred-fifty deposit you get back."
4. Take their details -- you need all three to book: first name, last name, and a contact
   phone number. Ask for the first and last name; for the phone, read back the number
   they're calling from to confirm it, or take a different one. Offer to add an email for
   the receipt -- optional.
5. Read it back before you book: the trailer, the dates, and the total.
6. Book it, then confirm in one short sentence. Remind them to bring a valid driver's
   license and a vehicle with the right hitch and ball to pick it up.

CHANGING OR CANCELLING
- Look their rental up from the number they're calling from first.
- If nothing's found under that number, say so plainly and offer to put them through
  rather than guessing.
- Read the rental back before you cancel it.

IF A TRAILER GETS TAKEN WHILE YOU'RE TALKING
It happens. Say so lightly -- "ah, that one just went" -- check again, and offer what's
left or another type.

ENDING THE CALL
Confirm what's arranged in one sentence, ask if there's anything else, then say goodbye
warmly and briefly.
"""


def _string(description: str, *, enum: list[str] | None = None) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=description, enum=enum)


def build_rental_tools(cfg: RentalConfig, *, native_audio: bool = True) -> list[types.Tool]:
    type_names = [t.name for t in cfg.trailer_types]
    declarations = [
        types.FunctionDeclaration(
            name="find_available_trailers",
            behavior=types.Behavior.NON_BLOCKING,
            description=(
                "Check which trailers are free for a pickup and return date, with the "
                "total price. Returns numbered options to read out. Always call this "
                "before quoting a price or offering a trailer."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "trailer_type": _string(
                        "The trailer the caller wants, if they named one.", enum=type_names
                    ),
                    "pickup_date": _string("Pickup date as YYYY-MM-DD, from the date above."),
                    "return_date": _string("Return date as YYYY-MM-DD, from the date above."),
                },
                required=["pickup_date", "return_date"],
            ),
        ),
        types.FunctionDeclaration(
            name="book_rental",
            behavior=types.Behavior.BLOCKING,
            description=(
                "Book one of the numbered options from find_available_trailers. Collect the "
                "caller's first name, last name and a contact phone number first -- all "
                "three are required. Do not say it's booked until this returns successfully."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "option": types.Schema(
                        type=types.Type.INTEGER,
                        description="The option number the caller chose.",
                    ),
                    "first_name": _string("The caller's first name."),
                    "last_name": _string("The caller's last name."),
                    "phone": _string(
                        "A contact phone number in E.164 form, e.g. +15551234567."
                    ),
                    "email": _string("The caller's email, if they give one (optional)."),
                },
                required=["option", "first_name", "last_name", "phone"],
            ),
        ),
        types.FunctionDeclaration(
            name="lookup_rental",
            behavior=types.Behavior.NON_BLOCKING,
            description=(
                "Find the caller's existing upcoming rentals, by their caller ID by "
                "default. Call this before cancelling."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "phone": _string("Only if they booked under a different number.")
                },
            ),
        ),
        types.FunctionDeclaration(
            name="cancel_rental",
            behavior=types.Behavior.BLOCKING,
            description="Cancel an existing rental. Confirm with the caller first.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "rental_id": types.Schema(
                        type=types.Type.INTEGER, description="From lookup_rental."
                    )
                },
                required=["rental_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="answer_faq",
            behavior=types.Behavior.NON_BLOCKING,
            description=(
                "Answer a question about the yard or the trailers -- hours, what to bring, "
                "insurance, hitch size. Use this rather than answering from memory."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"question": _string("The caller's question.")},
                required=["question"],
            ),
        ),
        types.FunctionDeclaration(
            name="transfer_to_human",
            behavior=types.Behavior.BLOCKING,
            description=(
                "Transfer the call to staff. Use for anything you cannot do or whenever "
                "the caller asks for a person."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"reason": _string("Why the call is being transferred.")},
                required=["reason"],
            ),
        ),
    ]
    if not native_audio:
        # The half-cascade model rejects function-calling behaviour config; unset it.
        for declaration in declarations:
            declaration.behavior = None
    return [types.Tool(function_declarations=declarations)]


def build_rental_live_config(
    cfg: RentalConfig,
    *,
    now: datetime | None = None,
    resumption_handle: str | None = None,
    voice: str = DEFAULT_VOICE,
    enable_affective_dialog: bool = True,
    vad: VadTuning | None = None,
    native_audio: bool = True,
) -> types.LiveConnectConfig:
    vad = vad or VadTuning()
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=build_rental_system_instruction(cfg, now=now),
        tools=build_rental_tools(cfg, native_audio=native_audio),
        enable_affective_dialog=(enable_affective_dialog if native_audio else None),
        thinking_config=(
            types.ThinkingConfig(thinking_budget=THINKING_BUDGET) if native_audio else None
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            activity_handling=(
                types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
                if vad.interruptible
                else types.ActivityHandling.NO_INTERRUPTION
            ),
            automatic_activity_detection=types.AutomaticActivityDetection(
                silence_duration_ms=vad.silence_ms,
                prefix_padding_ms=vad.prefix_padding_ms,
                end_of_speech_sensitivity=vad.end_sensitivity,
                start_of_speech_sensitivity=vad.start_sensitivity,
            ),
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=COMPRESSION_TRIGGER_TOKENS,
            sliding_window=types.SlidingWindow(target_tokens=COMPRESSION_TARGET_TOKENS),
        ),
        session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
    )
