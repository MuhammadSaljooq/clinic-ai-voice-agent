"""Build the Gemini Live session configuration and the agent's system instruction.

Pure: takes a ClinicConfig, returns SDK objects. No network, no credentials, so the
prompt and every tool contract are unit-testable.
"""

from __future__ import annotations

from google.genai import types

from clinic_agent.config import ClinicConfig

# Pinned deliberately. Affective dialog (reading the caller's tone) and
# NON_BLOCKING function calling both exist ONLY on this native-audio model --
# gemini-3.1-flash-live-preview documents "remove any configuration for these
# features". See spec 2.1 for the fallback plan if this model is retired.
NATIVE_AUDIO_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
FALLBACK_MODEL = "gemini-3.1-flash-live-preview"

DEFAULT_VOICE = "Kore"

# Compression keeps a call alive past the 15-minute uncompressed audio cap.
COMPRESSION_TRIGGER_TOKENS = 25_600
COMPRESSION_TARGET_TOKENS = 8_000

# Phone lines carry background noise; a little patience beats cutting callers off
# mid-sentence. Tunable per deployment.
END_OF_SPEECH_SILENCE_MS = 600
PREFIX_PADDING_MS = 120

# Reads are NON_BLOCKING so the line never goes silent while we look something up.
READ_TOOLS = ("find_slots", "lookup_appointment", "answer_faq")

# Writes are BLOCKING on purpose. A non-blocking write would let the model say
# "you're all booked" while the transaction is still in flight -- and it may yet come
# back SlotTaken. Since the scheduler is local Postgres (about a millisecond), there is
# no dead-air cost to waiting. transfer_to_human blocks because there is nothing
# sensible to say afterwards.
WRITE_TOOLS = (
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "transfer_to_human",
)


def build_system_instruction(cfg: ClinicConfig) -> str:
    provider_lines = []
    for provider in cfg.providers:
        hours = ", ".join(
            f"{w.weekday.capitalize()} {w.start:%H:%M}-{w.end:%H:%M}" for w in provider.availability
        )
        provider_lines.append(f"  - {provider.name}: {hours or 'no regular hours'}")

    type_lines = [
        f"  - {t.name}: {t.duration_min} minutes"
        + (f" ({t.description})" if t.description else "")
        for t in cfg.appointment_types
    ]

    faq_lines = [f"  Q: {entry.q}\n  A: {entry.a}" for entry in cfg.faq]

    return f"""You are the phone assistant for {cfg.clinic.name}.

HOW TO OPEN THE CALL
Your first sentence must greet the caller and state plainly that you are an AI
assistant. This is a legal requirement and must happen within the first few seconds,
before anything else. Say it warmly and naturally, not as a disclaimer. For example:
"Thanks for calling {cfg.clinic.name} -- I'm an AI assistant, and I can book, move or
cancel an appointment for you. What can I do for you today?"
If the caller asks whether you are a real person, a bot, or an AI, tell them the
truth immediately and without hedging.

HARD LIMITS -- THESE ARE NOT NEGOTIABLE
- Never give medical advice. Never interpret symptoms, test results, medications or
  imaging. Do not speculate about what might be wrong with someone.
- Never claim or imply that you are a nurse, doctor, or any kind of clinician, and
  never imply you hold medical credentials. You are an AI scheduling assistant.
- If the caller describes a symptom or asks a clinical question, acknowledge them
  kindly, say it needs a member of clinical staff, and use transfer_to_human.
- If anything suggests an emergency -- chest pain, difficulty breathing, severe
  bleeding, thoughts of self-harm -- say immediately: "Please hang up and call 911
  right away." Do not attempt to book anything.
- Never invent or guess availability, prices, hours, providers, or policies. If you
  do not know, use answer_faq, or transfer to a human. Do not make up an answer.
- Only ever offer appointment times that find_slots returned to you.

HOW TO SPEAK
- You are on a phone call. Keep turns to one or two sentences.
- Use contractions and everyday words. Sound like a warm, competent receptionist.
- Ask one question at a time, and never read a long list aloud. Offer at most three
  options, then let the caller choose.
- Before you look anything up, say something brief first, like "let me check that for
  you" -- silence on a phone line feels like the call dropped.
- Read times back the way people say them: "Monday the 14th at 9 in the morning".
- Confirm the details back to the caller before you book, and again once it is done.

CLINIC DETAILS
Name: {cfg.clinic.name}
Timezone: {cfg.clinic.timezone}
Phone: {cfg.clinic.phone_display}

Providers and their regular hours:
{chr(10).join(provider_lines)}

Appointment types:
{chr(10).join(type_lines)}

QUESTIONS YOU CAN ANSWER
{chr(10).join(faq_lines) if faq_lines else "  (none configured)"}

BOOKING FLOW
1. Work out what kind of appointment they need, and whether they have a preferred
   provider or time.
2. Call find_slots. It returns numbered options.
3. Read the options out naturally and let them pick one.
4. Collect their full name. Their phone number is already known from caller ID, so
   only ask if you need a different callback number.
5. Confirm the choice back to them, then call book_appointment with the option number.
6. Tell them it is confirmed and mention they will get a text reminder the day before.

If a booking comes back as unavailable, apologise briefly, call find_slots again, and
offer the new options. Do not tell the caller an appointment is booked until the tool
confirms it.
"""


def _string(description: str, *, enum: list[str] | None = None) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=description, enum=enum)


def build_tools(cfg: ClinicConfig) -> list[types.Tool]:
    """Declare the agent's tools.

    Appointment types and provider names are declared as enums drawn from config, so
    the model physically cannot request a service the clinic does not offer or a
    provider who does not exist.
    """
    type_names = [t.name for t in cfg.appointment_types]
    provider_names = [p.name for p in cfg.providers]

    declarations = [
        types.FunctionDeclaration(
            name="find_slots",
            behavior=types.Behavior.NON_BLOCKING,
            description=(
                "Find available appointment times. Returns numbered options to read "
                "out to the caller. Always call this before offering any time."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "appointment_type": _string(
                        "The kind of appointment requested.", enum=type_names
                    ),
                    "provider_name": _string(
                        "Preferred provider, if the caller named one.", enum=provider_names
                    ),
                    "date_preference": _string(
                        "What the caller said about timing, in their own words, e.g. "
                        "'next Tuesday morning' or 'as soon as possible'."
                    ),
                },
                required=["appointment_type"],
            ),
        ),
        types.FunctionDeclaration(
            name="lookup_appointment",
            behavior=types.Behavior.NON_BLOCKING,
            description=(
                "Find the caller's existing upcoming appointments. Uses their caller ID "
                "by default. Call this before rescheduling or cancelling."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "patient_name": _string("The caller's full name, if they gave it."),
                    "phone": _string("A different phone number to search, if they gave one."),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="answer_faq",
            behavior=types.Behavior.NON_BLOCKING,
            description=(
                "Answer a question about the clinic -- location, parking, hours, what to "
                "bring. Use this rather than answering from memory."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"question": _string("The caller's question.")},
                required=["question"],
            ),
        ),
        types.FunctionDeclaration(
            name="book_appointment",
            behavior=types.Behavior.BLOCKING,
            description=(
                "Book one of the numbered options that find_slots returned. Do not tell "
                "the caller it is booked until this returns successfully."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "option": types.Schema(
                        type=types.Type.INTEGER,
                        description="The option number the caller chose, as returned by find_slots.",
                    ),
                    "patient_name": _string("The caller's full name."),
                    "callback_phone": _string(
                        "Only if the caller wants a different number from the one they called from."
                    ),
                },
                required=["option", "patient_name"],
            ),
        ),
        types.FunctionDeclaration(
            name="reschedule_appointment",
            behavior=types.Behavior.BLOCKING,
            description="Move an existing appointment to one of the numbered options from find_slots.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "appointment_id": types.Schema(
                        type=types.Type.INTEGER,
                        description="From lookup_appointment.",
                    ),
                    "option": types.Schema(
                        type=types.Type.INTEGER, description="The new option number chosen."
                    ),
                },
                required=["appointment_id", "option"],
            ),
        ),
        types.FunctionDeclaration(
            name="cancel_appointment",
            behavior=types.Behavior.BLOCKING,
            description="Cancel an existing appointment. Confirm with the caller first.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "appointment_id": types.Schema(
                        type=types.Type.INTEGER, description="From lookup_appointment."
                    )
                },
                required=["appointment_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="transfer_to_human",
            behavior=types.Behavior.BLOCKING,
            description=(
                "Transfer the call to clinic staff. Use for anything clinical, anything "
                "you cannot do, or whenever the caller asks for a person."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"reason": _string("Why the call is being transferred.")},
                required=["reason"],
            ),
        ),
    ]
    return [types.Tool(function_declarations=declarations)]


def build_live_config(
    cfg: ClinicConfig,
    *,
    resumption_handle: str | None = None,
    voice: str = DEFAULT_VOICE,
    enable_affective_dialog: bool = True,
) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=build_system_instruction(cfg),
        tools=build_tools(cfg),
        enable_affective_dialog=enable_affective_dialog,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        # Transcribe both directions so calls are reviewable in the dashboard.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            automatic_activity_detection=types.AutomaticActivityDetection(
                silence_duration_ms=END_OF_SPEECH_SILENCE_MS,
                prefix_padding_ms=PREFIX_PADDING_MS,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
            ),
        ),
        # Unbounded session length; without this, audio sessions stop at 15 minutes.
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=COMPRESSION_TRIGGER_TOKENS,
            sliding_window=types.SlidingWindow(target_tokens=COMPRESSION_TARGET_TOKENS),
        ),
        # Configured even with no handle: resumption updates only stream if asked for.
        session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
    )
