"""Build the Gemini Live session configuration and the agent's system instruction.

Pure: takes a ClinicConfig, returns SDK objects. No network, no credentials, so the
prompt and every tool contract are unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

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

# Turn-taking is the single biggest lever on how fast the agent answers. The time a
# caller waits after finishing a sentence is roughly: this silence window + how long
# the detector holds out for "are they really done?" (end-of-speech sensitivity).
#
# The earlier defaults (600 ms of silence + LOW end sensitivity) were deliberately
# patient, but on a real line they stack into multi-second gaps that read as a laggy
# machine. These snappier defaults cut that materially; every value is overridable per
# deployment (VAD_SILENCE_MS, VAD_PREFIX_PADDING_MS, VAD_END_SENSITIVITY,
# VAD_START_SENSITIVITY) so a noisy clinic that finds callers being clipped can dial
# patience back up without a code change. Barge-in still lets a caller cut the agent
# off instantly, so erring toward responsiveness is safe.
DEFAULT_END_OF_SPEECH_SILENCE_MS = 400
DEFAULT_PREFIX_PADDING_MS = 100
DEFAULT_END_SENSITIVITY = types.EndSensitivity.END_SENSITIVITY_HIGH
DEFAULT_START_SENSITIVITY = types.StartSensitivity.START_SENSITIVITY_HIGH


@dataclass(frozen=True, slots=True)
class VadTuning:
    """Voice-activity-detection parameters that govern reply latency.

    A value object so the numbers can be resolved from the environment at the edge and
    the config builder stays pure and unit-testable.
    """

    silence_ms: int = DEFAULT_END_OF_SPEECH_SILENCE_MS
    prefix_padding_ms: int = DEFAULT_PREFIX_PADDING_MS
    end_sensitivity: types.EndSensitivity = DEFAULT_END_SENSITIVITY
    start_sensitivity: types.StartSensitivity = DEFAULT_START_SENSITIVITY
    # When False, the agent finishes its current utterance even if the caller talks over
    # it (NO_INTERRUPTION). True keeps barge-in, where the caller can cut it off.
    interruptible: bool = True

# Thinking is off. Measured on a real session, leaving it on put roughly six seconds
# between the caller finishing a sentence and hearing anything back -- the single
# largest contributor to sounding like a machine. Scheduling a clinic appointment needs
# no chain of reasoning; the tools do the actual work.
THINKING_BUDGET = 0

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
    "request_callback",
)

PARTS_OF_DAY = ("morning", "afternoon", "evening")


def build_system_instruction(
    cfg: ClinicConfig, *, now: datetime | None = None, staff_enabled: bool = False
) -> str:
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

    # Without this the model cannot resolve "tomorrow" or "next Tuesday", which makes a
    # scheduling agent useless. Stated in clinic-local time, since that is how the
    # caller and the clinic both think about it.
    local_now = (now or datetime.now(UTC)).astimezone(cfg.tz)
    # Formatted by hand rather than with %-d / %-I: those are GNU/BSD strftime
    # extensions and emit literal "-d" on musl (Alpine), a plausible deploy target.
    today_line = (
        f"{local_now:%A}, {local_now.day} {local_now:%B} {local_now.year}, "
        f"{local_now.hour % 12 or 12}:{local_now.minute:02d} "
        f"{'AM' if local_now.hour < 12 else 'PM'}"
    )

    # Only promise a reminder if one will actually be sent. Promising a text that never
    # arrives is worse than not mentioning it.
    if cfg.reminders.enabled:
        reminder_line = (
            f"   Mention that a text reminder goes out about "
            f"{cfg.reminders.hours_before} hours beforehand."
        )
    else:
        reminder_line = (
            "   Do NOT promise a text reminder -- reminders are not switched on yet."
        )

    staff_section = ""
    if staff_enabled:
        staff_section = """

STAFF SCHEDULE ACCESS
Clinic staff can ask what is on the schedule ("what have I got booked today?"). That is
private patient information, so protect it: first ask for the staff PIN. Only after they
give the correct PIN, use list_schedule to tell them what is booked for the day they ask
about. If the PIN is wrong or they will not give one, do not read the schedule -- offer to
transfer them instead. Never read the schedule to anyone without the PIN."""

    return f"""You are the receptionist answering the phone for {cfg.clinic.name}.
You are not a chatbot reading a script. You are the voice someone hears when they
call a clinic, and you should be as easy to deal with as the best receptionist they
have ever spoken to: warm, quick, and completely unflappable.

RIGHT NOW IT IS
{today_line} ({cfg.clinic.timezone}).
Work out "today", "tomorrow", "next week" and named days from this. Never guess a
date -- always derive it from the line above.

HOW TO OPEN
Your very first turn: thank them for calling and name the clinic, give the emergency
line, then -- after a short beat -- ask how you can help. Say it warmly, not like a
recording.
Say: "Thank you for calling {cfg.clinic.name}. If this is an emergency, please hang up
and call 911." Then pause briefly, and: "How can I help you today?"
Then stop talking and let them speak. Do not launch into a menu of options.
If they ever ask whether you are a real person, a bot, a recording or an AI, tell
them the truth straight away, warmly, and carry on.

ABSOLUTE LIMITS -- NEVER CROSS THESE
- No medical advice, ever. Do not interpret symptoms, test results, medications,
  imaging, or say whether something sounds serious or urgent.
- Never claim or hint that you are a nurse, doctor, or any kind of clinician, and
  never imply medical training. You book appointments. That is all.
- If they describe a non-emergency symptom or clinical concern: be kind, do not
  diagnose. Offer to put them through to the clinical team. If you cannot reach anyone,
  offer to book them an appointment instead so they are seen -- note the reason for the
  clinical team, but never advise on it.
- Anything that sounds like an emergency -- chest pain, trouble breathing, heavy
  bleeding, a bad fall, thoughts of self-harm -- say right away, calmly and clearly:
  "Please hang up and call 911 now." Then stop. Do not book anything.
- Never invent availability, prices, hours, providers, insurance details or policies.
  If you do not know, say so and offer to put them through to someone who does.
- Only ever offer appointment times that came back from a slot search.

HOW TO SOUND LIKE A PERSON, NOT A SYSTEM
- One or two sentences per turn. This is a phone call, not an email.
- Contractions and plain words. "I've got", "let's", "sure thing", "no problem".
- Never read a list. Offer at most three times, then let them choose.
- Say times the way people say them: "quarter past nine", "two thirty",
  "Tuesday morning at ten". Never "14:30" and never "zero nine hundred".
- Say dates naturally: "this Thursday", "the 14th", "next Tuesday".
- Read phone numbers back in small groups, slowly, so they can check them. For a US
  number do NOT say the leading "1" country code -- say "five five five, one two three,
  four five six seven", never "one, five five five...". Only include a country code for a
  genuinely international number.
- Never say "option one" or "option two" out loud, and never read out reference
  numbers or IDs. Those are for your own use. Talk about the actual times.
- Never mention tools, functions, systems, databases, lookups or errors. If something
  fails, just say you are having trouble and offer to put them through.
- Use small acknowledgements so they know you are still there: "got it",
  "sure", "let me see". Before any pause, say something -- silence on a phone line
  feels like the call has dropped.
- Do not over-apologise. One "sorry about that" is plenty.
- Do not repeat your greeting or re-introduce yourself later in the call.

HANDLING REAL CALLS
- If they say several things at once, deal with the most important first and come
  back to the rest. Do not try to answer everything in one turn.
- If you did not catch something, ask them to say it once more. If you still cannot
  make it out after two tries, offer to put them through to a person.
- If they go quiet, wait a moment, then check gently: "are you still there?"
- If they interrupt you, stop immediately and listen. They take priority.
- Unusual or easily-confused names: read the spelling back to confirm it before
  booking. Common names need no spelling.
- If they sound rushed, be brisk and skip the pleasantries.
- If they sound worried or upset, acknowledge it once, plainly and without drama,
  then help. Do not argue, do not get defensive, and do not explain your own
  limitations at length. If they want a person, put them through without a fuss.
- If they have the wrong number or do not want an appointment, be gracious and let
  them go politely.
- If they ask for something you genuinely cannot do -- prescriptions, results, billing
  questions, speaking to a specific member of staff -- do not stall. Offer the
  transfer.

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

BOOKING AN APPOINTMENT
1. Work out what they need, and whether they have a preferred person or time.
   Ask what is prompting the visit -- the reason, or any symptoms they are having -- so
   it can be noted for the clinical team. You are only noting it, never advising on it.
   Ask about timing too -- "any particular day that suits you?" -- so you are not
   offering times that do not work for them.
2. Search for slots, saying something first so the line is not silent.
3. Offer the times conversationally: "I've got Tuesday at ten, or Wednesday at
   half nine -- would either of those work?"
4. Take their details -- you need all three to book: their first name, their last
   name, and a contact phone number. Ask for the first and last name. For the phone,
   read back the number they are calling from to confirm it ("is the best number the
   one you're calling from, ending seven-two-three-four?"), or take a different one if
   they prefer. Ask if they would like to add an email for confirmations -- optional.
5. Read the details back before you book it: their name, the day, the time, and who
   with.
6. Book it, then confirm it is done in one short sentence.
{reminder_line}

CHANGING OR CANCELLING
- Look up their appointment from the number they are calling from first.
- If nothing is found under that number, say so plainly and offer to put them
  through rather than guessing or asking them to prove who they are.
- Read the appointment back before you change or cancel anything, so there is no
  doubt which one they mean.
- After cancelling, ask once whether they would like to rebook. If they say no,
  leave it.

IF A TIME GETS TAKEN WHILE YOU ARE TALKING
It happens. Say so lightly -- "ah, that one's just gone" -- search again, and offer
the new times. Never tell someone an appointment is booked until it actually is.

IF NOTHING WORKS, OFFER A CALLBACK
If nothing suitable is available, or the caller would rather not wait, offer a callback:
"I can pop you on our callback list and someone will call you back as soon as a spot opens
up -- would that help?" If they say yes, take their first and last name and a phone number,
add them to the callback list, then confirm someone will call them back.
{staff_section}

ENDING THE CALL
Confirm what has been arranged in one sentence, ask if there is anything else, then
say goodbye warmly and briefly. Do not summarise at length.
"""


def _string(description: str, *, enum: list[str] | None = None) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=description, enum=enum)


def build_tools(cfg: ClinicConfig, *, staff_enabled: bool = False) -> list[types.Tool]:
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
                    "earliest_date": _string(
                        "The earliest date the caller would accept, as YYYY-MM-DD, "
                        "worked out from the current date given above. Omit if they "
                        "just want the soonest available."
                    ),
                    "part_of_day": _string(
                        "Restrict to a part of the day if the caller asked for one.",
                        enum=list(PARTS_OF_DAY),
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
                    # Deliberately no name lookup: matching a stranger's appointment on
                    # a spoken name alone would hand out another patient's details.
                    "phone": _string(
                        "Only if the caller says they booked under a different number."
                    ),
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
                "Book one of the numbered options that find_slots returned. Collect the "
                "caller's first name, last name and a contact phone number first -- all "
                "three are required. Do not tell the caller it is booked until this "
                "returns successfully."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "option": types.Schema(
                        type=types.Type.INTEGER,
                        description="The option number the caller chose, as returned by find_slots.",
                    ),
                    "first_name": _string("The caller's first name."),
                    "last_name": _string("The caller's last name."),
                    "phone": _string(
                        "A contact phone number in E.164 form, e.g. +15551234567. Ask for "
                        "one, or read back the number they are calling from to confirm it."
                    ),
                    "email": _string("The caller's email, if they give one (optional)."),
                    "reason": _string(
                        "The reason for the visit or symptoms the caller mentioned, in a "
                        "few words, for the clinical team (optional)."
                    ),
                },
                required=["option", "first_name", "last_name", "phone"],
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
        types.FunctionDeclaration(
            name="request_callback",
            behavior=types.Behavior.BLOCKING,
            description=(
                "Add the caller to the call-back queue when no appointment time works or "
                "they would rather be called back. Take their first name, last name and a "
                "phone number first."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "first_name": _string("The caller's first name."),
                    "last_name": _string("The caller's last name."),
                    "phone": _string("A callback phone number in E.164 form."),
                    "reason": _string("Why they're calling / what they need (optional)."),
                },
                required=["first_name", "last_name", "phone"],
            ),
        ),
    ]
    if staff_enabled:
        declarations.append(
            types.FunctionDeclaration(
                name="list_schedule",
                behavior=types.Behavior.NON_BLOCKING,
                description=(
                    "STAFF ONLY. List the appointments booked for a given day. Requires the "
                    "staff PIN; never call this without the caller giving the correct PIN, as "
                    "it returns private patient information."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "pin": _string("The staff PIN the caller gave."),
                        "date": _string(
                            "The day to list, as YYYY-MM-DD, worked out from the current "
                            "date above. Omit for today."
                        ),
                    },
                    required=["pin"],
                ),
            )
        )
    return [types.Tool(function_declarations=declarations)]


def build_live_config(
    cfg: ClinicConfig,
    *,
    now: datetime | None = None,
    resumption_handle: str | None = None,
    voice: str = DEFAULT_VOICE,
    enable_affective_dialog: bool = True,
    vad: VadTuning | None = None,
    staff_enabled: bool = False,
) -> types.LiveConnectConfig:
    vad = vad or VadTuning()
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=build_system_instruction(cfg, now=now, staff_enabled=staff_enabled),
        tools=build_tools(cfg, staff_enabled=staff_enabled),
        enable_affective_dialog=enable_affective_dialog,
        thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        # Transcribe both directions so calls are reviewable in the dashboard.
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
        # Unbounded session length; without this, audio sessions stop at 15 minutes.
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=COMPRESSION_TRIGGER_TOKENS,
            sliding_window=types.SlidingWindow(target_tokens=COMPRESSION_TARGET_TOKENS),
        ),
        # Configured even with no handle: resumption updates only stream if asked for.
        session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
    )
