# Voice Bridge Implementation Plan (Plan 2 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bridge a live Telnyx phone call to a Gemini Live native-audio session, with correct audio conversion, instant barge-in, and survival across the ~10-minute WebSocket reset.

**Architecture:** Everything protocol-shaped is a **pure function** over bytes and dicts -- audio conversion, Telnyx frame codec, webhook signature, Live config building. Only `bridge/call_session.py` is stateful, and it is driven in tests by a fake Telnyx socket and a fake Gemini session, so the whole bridge is testable with no network and no credentials.

**Tech Stack:** Python 3.12, numpy + scipy (resampling), websockets, FastAPI, PyNaCl (Ed25519), google-genai 2.22.0.

---

## Verified facts driving this plan

All confirmed against vendor docs or the installed SDK, not assumed.

| Fact | Source | Consequence |
|---|---|---|
| **L16 is big-endian** (network byte order) | RFC 3551 | **Byte swap required in both directions.** Gemini PCM16 is little-endian. Getting this wrong produces loud static, not silence -- easy to misdiagnose as a codec problem |
| Telnyx L16 is 16 kHz; Gemini input is 16 kHz | Telnyx docs / Gemini docs | **No resampling inbound.** Byte swap only |
| Gemini output is 24 kHz | Gemini docs | `resample_poly(up=2, down=3)` -> 16 kHz outbound |
| Telnyx accepts `{"event":"clear"}` | Telnyx docs | Barge-in can flush already-buffered playback |
| Telnyx chunks must be >= 20 ms | Telnyx docs | Frame at exactly 20 ms = 320 samples = 640 bytes |
| Stream params: `stream_url`, `stream_track` (`inbound_track`/`outbound_track`/`both_tracks`), `stream_bidirectional_mode: "rtp"`, `stream_bidirectional_codec: "L16"` | Telnyx docs | Can be set on `answer`, saving a round-trip |
| Webhook signing: Ed25519, headers `telnyx-signature-ed25519` + `telnyx-timestamp`, message is `{timestamp}\|{raw_body}`, 5-min replay window | Telnyx docs | PyNaCl, already a dependency |
| SDK 2.22.0 has `enable_affective_dialog`, `session_resumption`, `context_window_compression`, `proactivity`, `realtime_input_config`, `input/output_audio_transcription`, `Behavior.NON_BLOCKING` | **Introspected installed package** | Every feature the spec relies on exists |
| `LiveServerMessage` carries `server_content`, `tool_call`, `session_resumption_update`, `go_away`, `usage_metadata` | Introspected | `go_away.time_left` allows reconnecting *before* being cut off |
| `LiveServerContent.interrupted` | Introspected | The barge-in trigger |

---

## File structure

| File | Responsibility | Pure? |
|---|---|---|
| `audio/codec.py` | L16(BE) <-> PCM16(LE), 24k->16k, 20 ms framing | **Yes** |
| `telephony/stream_protocol.py` | Telnyx WS frame encode/decode | **Yes** |
| `telephony/signature.py` | Ed25519 webhook verification | **Yes** |
| `telephony/telnyx_client.py` | Call Control REST: answer, transfer, hangup | No (httpx) |
| `ai/provider.py` | AI Studio <-> Vertex seam | No |
| `ai/live_config.py` | Build `LiveConnectConfig` | **Yes** |
| `ai/live_session.py` | Connect, resume, reconnect on GoAway | No |
| `bridge/call_session.py` | Orchestrate both sockets, barge-in, tools | No |
| `app.py` | FastAPI webhook + WS endpoint | No |
| `tests/fakes/` | Fake Telnyx socket, fake Gemini session | - |

---

### Task 1: Audio codec (highest risk, do first)

**Files:** Create `src/clinic_agent/audio/codec.py`, `tests/test_audio_codec.py`

- [ ] **Step 1: Write the failing tests**

Cases: byte swap is its own inverse; a known big-endian sample decodes to the
expected int; 24k->16k halves-and-a-bit the sample count at the 2/3 ratio;
a 24 kHz sine survives resampling with its frequency intact (guards against a
resampler that silently changes pitch); framing yields exactly 640-byte frames;
a trailing partial frame is padded, not dropped; empty input yields no frames.

- [ ] **Step 2: Run, confirm failure**

Run: `.venv/bin/pytest tests/test_audio_codec.py -q` -> `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`telnyx_to_gemini(raw)`: interpret as `>i2`, view as `<i2`, return bytes.
`gemini_to_telnyx(raw)`: interpret `<i2`, `resample_poly(up=2, down=3)`, clip, to `>i2`.
`frame_20ms(raw)`: split into 640-byte frames, zero-pad the tail.

- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 2: Telnyx stream protocol

**Files:** Create `src/clinic_agent/telephony/stream_protocol.py`, `tests/test_stream_protocol.py`

- [ ] **Step 1: Tests** -- decode `connected`/`start`/`media`/`stop`/`dtmf`; unknown
      event type is surfaced, not crashed on; `start` exposes `call_control_id` and
      `media_format`; encode `media` produces base64 payload; encode `clear`
      produces exactly `{"event":"clear"}`; malformed JSON raises a typed error.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement** with a small frozen dataclass per inbound event.
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 3: Webhook signature verification

**Files:** Create `src/clinic_agent/telephony/signature.py`, `tests/test_signature.py`

- [ ] **Step 1: Tests** -- a signature generated with a test keypair verifies;
      tampered body rejected; tampered timestamp rejected; a timestamp older than
      the tolerance rejected (**replay protection**); wrong key rejected;
      malformed base64 rejected.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement** with PyNaCl `VerifyKey`, message `f"{ts}|{body}"`.
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 4: Live API config builder

**Files:** Create `src/clinic_agent/ai/live_config.py`, `src/clinic_agent/ai/provider.py`, `tests/test_live_config.py`

- [ ] **Step 1: Tests** -- config sets `AUDIO` modality, `enable_affective_dialog`,
      context compression, session resumption, both transcriptions; the system
      instruction contains the AI disclosure and the no-medical-advice guardrail;
      **read** tools are `NON_BLOCKING` and **write** tools are
      `BLOCKING`; a resumption handle is threaded through when supplied; provider
      seam returns Vertex config when `AI_PROVIDER=vertex`.

**Revision to this plan:** it originally said *every* tool should be
`NON_BLOCKING`. That is wrong for writes. A non-blocking write lets the model keep
talking -- and say "you're all booked" -- while the transaction is still in flight
and may yet come back `SlotTaken`. Reads stay `NON_BLOCKING` so the line never goes
silent. There is also no dead-air cost to blocking on a write here: choosing
Postgres over the Google Calendar API in Plan 1 dropped tool latency from
200-800 ms to about a millisecond, which removes most of the original motivation
for non-blocking writes in the first place.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 5: Call session bridge

**Files:** Create `src/clinic_agent/bridge/call_session.py`, `tests/fakes/__init__.py`, `tests/test_call_session.py`

- [ ] **Step 1: Build the fakes** -- `FakeTelnyxSocket` records sent frames and
      replays a scripted inbound sequence; `FakeGeminiSession` emits scripted
      `LiveServerMessage`s including `interrupted`, `tool_call`,
      `session_resumption_update`, and `go_away`.
- [ ] **Step 2: Tests**
  - inbound audio reaches Gemini byte-swapped and unresampled
  - Gemini audio reaches Telnyx as 640-byte base64 frames
  - `interrupted` -> queue flushed **and** `{"event":"clear"}` sent
  - `tool_call` -> handler invoked, `send_tool_response` called with matching id
  - a failing tool returns a structured error rather than killing the call
  - `session_resumption_update` handle is stored
  - `go_away` triggers reconnect using the stored handle, and audio continues
  - `stop` event ends the session cleanly
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

### Task 6: FastAPI app

**Files:** Create `src/clinic_agent/app.py`, `tests/test_app.py`

- [ ] **Step 1: Tests** -- unsigned webhook rejected 401; signed `call.initiated`
      triggers answer-with-streaming; health endpoint returns ok.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, confirm pass. Commit.**

---

## Definition of done

- [ ] Full suite green, lint clean
- [ ] Byte-order handled and tested in both directions
- [ ] Barge-in emits `clear` **and** flushes the local queue
- [ ] Reconnect-on-`go_away` proven to preserve the session handle
- [ ] No credentials required to run any test

## Explicitly deferred

Real call verification (needs Telnyx + Gemini keys), the agent tool handlers
wired to the scheduler (Plan 3), calendar mirror and reminders (Plan 4).
