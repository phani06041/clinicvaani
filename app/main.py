"""ClinicVaani server.

Browser mic (16 kHz PCM) --ws--> this server --Gemini Live API--> native audio (24 kHz PCM) --ws--> browser
Tool calls from Gemini are executed against the clinic back-office (SQLite) and every step is streamed to the
front-desk console over /ws/ops so judges can see understanding -> retrieval -> action -> human approval.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import clinic, prompts
from . import tools as toolmod

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

USE_VERTEX = os.getenv("USE_VERTEX", "0") == "1"
API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ("vertex" if USE_VERTEX else None)


def make_client():
    """AI Studio key by default; set USE_VERTEX=1 (+ GOOGLE_CLOUD_PROJECT) to run on Vertex AI / Google Cloud."""
    from google import genai

    if USE_VERTEX:
        return genai.Client(vertexai=True, project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                            location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    from google.genai import types

    # The Live API is served on v1beta; newer SDKs default to v1, which 404s on the Live websocket.
    return genai.Client(api_key=API_KEY,
                        http_options=types.HttpOptions(api_version=os.getenv("GEMINI_API_VERSION", "v1beta")))
MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
# Tried in order if the configured model is not available to this key (404 / not found).
CANDIDATE_MODELS = list(dict.fromkeys([MODEL, "gemini-3.8-live", "gemini-3.1-flash-live-preview",
                                       "gemini-2.5-flash-native-audio-preview-12-2025",
                                       "gemini-2.5-flash-native-audio-preview-09-2025"]))
_working_model: str | None = None


def _is_model_missing(e: Exception) -> bool:
    t = f"{type(e).__name__} {e}".lower()
    return "404" in t or "not found" in t or "not supported" in t or "1008" in t


async def open_live(client, stack: contextlib.AsyncExitStack):
    """Connect to the first Live model this key can use; remember it for later calls."""
    global _working_model
    order = ([_working_model] if _working_model else []) + [m for m in CANDIDATE_MODELS if m != _working_model]
    failed = []
    for m in order:
        try:
            session = await stack.enter_async_context(client.aio.live.connect(model=m, config=live_config()))
        except Exception as e:  # noqa: BLE001
            if not _is_model_missing(e):
                raise
            failed.append(m)
            log.warning("Live model %s not available to this key: %s", m, e)
            continue
        if m != MODEL:
            log.warning("Using %s because %s is not available. Set GEMINI_LIVE_MODEL=%s in .env", m, MODEL, m)
        _working_model = m
        return session, m
    raise RuntimeError("None of these Live models are available to your key: " + ", ".join(failed) +
                       ". Run `python scripts/list_live_models.py` and put one of its names in GEMINI_LIVE_MODEL in .env.")
VOICE = os.getenv("GEMINI_VOICE", "Kore")
log = logging.getLogger("clinicvaani")

# ------------------------------------------------------------------ staff sign-in
DEFAULT_PIN = "2580"
STAFF_PIN = os.getenv("STAFF_PIN") or DEFAULT_PIN
_SECRET = (os.getenv("STAFF_SECRET") or secrets.token_hex(32)).encode()
COOKIE = "cv_staff"
SESSION_HOURS = 12


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:40]


def make_staff_token(name: str) -> str:
    payload = base64.urlsafe_b64encode(name.encode()).decode() + "." + str(int(time.time()) + SESSION_HOURS * 3600)
    return payload + "." + _sign(payload)


def staff_from_token(token: str | None) -> str | None:
    try:
        b64, exp, sig = (token or "").split(".")
        if not hmac.compare_digest(sig, _sign(b64 + "." + exp)) or int(exp) < time.time():
            return None
        return base64.urlsafe_b64decode(b64.encode()).decode()
    except Exception:  # noqa: BLE001
        return None


def require_staff(request: Request) -> str:
    name = staff_from_token(request.cookies.get(COOKIE))
    if not name:
        raise HTTPException(401, "Staff sign-in required")
    return name
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")


# ------------------------------------------------------------------ ops hub

class Hub:
    """Fan-out of live events to every open front-desk console."""

    def __init__(self) -> None:
        self.subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def publish(self, event: dict) -> None:
        event.setdefault("ts", time.time())
        for q in list(self.subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)


hub = Hub()


def script_of(text: str) -> str:
    """Rough language tag from the transcript's script (Latin is reported as en/romanised)."""
    counts: dict[str, int] = {}
    for ch in text:
        o = ord(ch)
        lang = ("Telugu" if 0x0C00 <= o <= 0x0C7F else "Hindi" if 0x0900 <= o <= 0x097F else
                "Tamil" if 0x0B80 <= o <= 0x0BFF else "Kannada" if 0x0C80 <= o <= 0x0CFF else
                "Latin" if ch.isascii() and ch.isalpha() else None)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    native = {k: v for k, v in counts.items() if k != "Latin" and v >= 2}
    if native:
        top = max(native, key=native.get)
        return f"{top}+English" if counts.get("Latin", 0) > 3 else top
    return "Latin" if counts else ""


class Call:
    def __init__(self, ws: WebSocket) -> None:
        self.id = "CALL-" + uuid.uuid4().hex[:5].upper()
        self.ws = ws
        self.session = None
        self.started = time.time()
        self.started_iso = clinic.now().isoformat()
        self.languages: set[str] = set()
        self.turns = 0
        self.tool_calls = 0
        self.interruptions = 0
        self.latencies: list[int] = []
        self.outcomes: set[str] = set()
        self.caller_buf = ""
        self.last_caller_words = ""   # rolling tail of what the caller said last, to anchor reply language
        self._caller_turn_open = False

    async def send(self, event: dict) -> None:
        with contextlib.suppress(Exception):
            await self.ws.send_text(json.dumps(event, ensure_ascii=False, default=str))

    async def emit(self, event: dict) -> None:
        event = {**event, "call_id": self.id}
        hub.publish(dict(event))
        await self.send(event)

    async def inject(self, text: str) -> None:
        """Push a clinic-system message into the live conversation (human-in-the-loop updates)."""
        if self.session is not None:
            await self.session.send_realtime_input(text=text)
            await self.emit({"type": "system_injected", "text": text})

    def summary(self) -> dict:
        return {"id": self.id, "started_at": self.started_iso, "ended_at": clinic.now().isoformat(),
                "duration_s": round(time.time() - self.started, 1), "languages": sorted(self.languages),
                "turns": self.turns, "tool_calls": self.tool_calls, "interruptions": self.interruptions,
                "latencies": self.latencies, "outcomes": sorted(self.outcomes)}


CALLS: dict[str, Call] = {}


# ------------------------------------------------------------------ Gemini

def live_config():
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=prompts.system_instruction())]),
        tools=[types.Tool(function_declarations=toolmod.DECLARATIONS)],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
    )


OUTCOME_OF = {"book_appointment": "booking", "cancel_appointment": "cancellation", "escalate_to_human": "handoff",
              "create_preauth_request": "preauth", "check_insurance_coverage": "insurance_answer",
              "search_clinic_knowledge": "info_answer"}


async def handle_tool_call(call: Call, tool_call) -> None:
    from google.genai import types

    responses = []
    changed = False
    for fc in tool_call.function_calls or []:
        args = dict(fc.args or {})
        await call.emit({"type": "tool_call", "id": fc.id, "name": fc.name, "args": args})
        t0 = time.perf_counter()
        try:
            result = await asyncio.to_thread(toolmod.dispatch, fc.name, args, call.id)
        except Exception as e:  # never let a tool crash the call
            log.exception("tool %s failed", fc.name)
            result = {"ok": False, "error": "INTERNAL_ERROR", "message": str(e)}
        ms = round((time.perf_counter() - t0) * 1000, 1)
        sms = result.pop("_deliver_sms", None) if isinstance(result, dict) else None
        if sms:  # only the caller's phone sees the SMS text (verification codes never reach Gemini or staff)
            await call.send({"type": "sms", "to": sms["to"][:2] + "XXXXXX" + sms["to"][-2:], "text": sms["text"]})
        call.tool_calls += 1
        if result.get("ok") and fc.name in OUTCOME_OF:
            call.outcomes.add(OUTCOME_OF[fc.name])
        changed |= fc.name in toolmod.STATE_CHANGING
        await call.emit({"type": "tool_result", "id": fc.id, "name": fc.name, "result": result, "ms": ms})
        responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response={
            "result": result,
            "reply_language": f'Answer in the SAME language and mix as the caller\'s last words: "{call.last_caller_words.strip()}"',
        }))
    await call.session.send_tool_response(function_responses=responses)
    if changed:
        hub.publish({"type": "state_changed"})


async def transcript(call: Call, role: str, text: str, typed: bool = False) -> None:
    if role == "caller":
        call.last_caller_words = ((call.last_caller_words + text) if call._caller_turn_open else text)[-160:]
        call._caller_turn_open = True
        lang = script_of(text)
        if lang:
            call.languages.add("English/romanised" if lang == "Latin" else lang)
    if role == "agent":
        call._caller_turn_open = False
    await call.emit({"type": "transcript", "role": role, "delta": text, "typed": typed})


async def from_gemini(call: Call) -> None:
    session = call.session
    empty_rounds = 0
    while True:
        got = 0
        async for msg in session.receive():
            got += 1
            sc = msg.server_content
            if sc is not None:
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            await call.ws.send_bytes(part.inline_data.data)
                if sc.input_transcription and sc.input_transcription.text:
                    await transcript(call, "caller", sc.input_transcription.text)
                if sc.output_transcription and sc.output_transcription.text:
                    await transcript(call, "agent", sc.output_transcription.text)
                if sc.interrupted:
                    call.interruptions += 1
                    await call.emit({"type": "interrupted"})
                if sc.turn_complete:
                    call.turns += 1
                    await call.emit({"type": "turn_complete"})
            if msg.tool_call:
                await handle_tool_call(call, msg.tool_call)
            if msg.tool_call_cancellation:
                await call.emit({"type": "tool_cancelled", "ids": msg.tool_call_cancellation.ids})
            if msg.go_away:
                await call.emit({"type": "notice", "message": "Gemini session is about to time out; start a new call."})
        empty_rounds = empty_rounds + 1 if got == 0 else 0
        if empty_rounds >= 3:  # connection closed
            return


async def from_browser(call: Call) -> None:
    from google.genai import types

    while True:
        m = await call.ws.receive()
        if m["type"] == "websocket.disconnect":
            return
        if m.get("bytes"):
            await call.session.send_realtime_input(audio=types.Blob(data=m["bytes"], mime_type="audio/pcm;rate=16000"))
        elif m.get("text"):
            d = json.loads(m["text"])
            kind = d.get("type")
            if kind == "text" and d.get("text"):
                await transcript(call, "caller", d["text"], typed=True)
                await call.session.send_realtime_input(text=d["text"])
            elif kind == "latency" and isinstance(d.get("ms"), (int, float)):
                ms = int(d["ms"])
                if 0 < ms < 15000:
                    call.latencies.append(ms)
                    hub.publish({"type": "latency", "ms": ms, "call_id": call.id})
            elif kind == "mic_paused":
                await call.session.send_realtime_input(audio_stream_end=True)
            elif kind == "hangup":
                return


# ------------------------------------------------------------------ app

@asynccontextmanager
async def lifespan(_app: FastAPI):
    clinic.init_db(reset=os.getenv("RESET_DB_ON_START", "0") == "1")
    if not API_KEY:
        log.warning("GEMINI_API_KEY is not set: the console works, but calls will fail. Put it in .env")
    if STAFF_PIN == DEFAULT_PIN:
        log.warning("Staff console PIN is the default (%s). Set STAFF_PIN in .env before sharing the server.", DEFAULT_PIN)
    yield


app = FastAPI(title="ClinicVaani", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    """Demo view: patient phone and staff console side by side."""
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/patient")
def patient():
    """What a caller sees: the call screen only, no clinic data."""
    return FileResponse(ROOT / "static" / "patient.html")


@app.get("/staff")
def staff():
    """Front-desk console: live call monitor, approvals, appointments, metrics."""
    return FileResponse(ROOT / "static" / "staff.html")


@app.get("/api/config")
def config():
    return {"model": _working_model or MODEL, "voice": VOICE, "has_key": bool(API_KEY),
            "backend": "Vertex AI" if USE_VERTEX else "Gemini API (AI Studio)"}


class Login(BaseModel):
    name: str
    pin: str


@app.post("/api/staff/login")
async def staff_login(body: Login, response: Response):
    name = body.name.strip()[:40]
    if not name or not hmac.compare_digest(body.pin.strip(), STAFF_PIN):
        await asyncio.sleep(0.8)  # slow down guessing
        raise HTTPException(401, "Wrong PIN")
    response.set_cookie(COOKIE, make_staff_token(name), httponly=True, samesite="lax", max_age=SESSION_HOURS * 3600)
    return {"ok": True, "name": name}


@app.post("/api/staff/logout")
def staff_logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/staff/me")
def staff_me(name: str = Depends(require_staff)):
    return {"name": name}


@app.get("/api/state")
def state(_staff: str = Depends(require_staff)):
    s = clinic.dashboard_state()
    s["active_calls"] = list(CALLS)
    return s


class Decision(BaseModel):
    decision: str
    note: str = ""


def _conflict(row: dict, what: str) -> HTTPException:
    who = row.get("staff_name") or "another staff member"
    return HTTPException(409, f"{what} is already {row['status'].replace('_', ' ')} by {who}.")


@app.post("/api/preauths/{pa_id}/decision")
async def preauth_decision(pa_id: str, body: Decision, staff: str = Depends(require_staff)):
    if body.decision not in ("approved", "rejected", "need_more_info"):
        raise HTTPException(400, "decision must be approved, rejected or need_more_info")
    res = clinic.decide_preauth(pa_id, body.decision, body.note, staff)
    if not res:
        raise HTTPException(404, "unknown pre-auth")
    if res["conflict"]:
        raise _conflict(res["row"], pa_id)
    row = res["row"]
    hub.publish({"type": "state_changed"})
    call = CALLS.get(row.get("call_id") or "")
    if call:
        await call.inject(
            f"[STAFF UPDATE] {staff} reviewed pre-authorization {pa_id} for {row['patient_name']} "
            f"({row['procedure'].replace('_', ' ')}, {row['insurer']} {row['plan']}): {body.decision.replace('_', ' ').upper()}. "
            f"Staff note: {body.note or 'none'}. Tell the caller now, in the same language as their last words "
            f"(\"{call.last_caller_words.strip()}\"), and explain the next step.")
    return {"ok": True, "preauth": row, "relayed_to_live_call": bool(call)}


@app.post("/api/handoffs/{ho_id}/status")
async def handoff_status(ho_id: str, body: Decision, staff: str = Depends(require_staff)):
    if body.decision not in ("taken", "resolved"):
        raise HTTPException(400, "decision must be taken or resolved")
    res = clinic.update_handoff(ho_id, body.decision, staff, body.note)
    if not res:
        raise HTTPException(404, "unknown hand-off")
    if res["conflict"]:
        raise _conflict(res["row"], ho_id)
    row = res["row"]
    hub.publish({"type": "state_changed"})
    call = CALLS.get(row.get("call_id") or "")
    if call and body.decision == "taken":
        await call.inject(
            f"[STAFF UPDATE] {staff} from the front desk is taking over hand-off {ho_id}. "
            f"{('Note: ' + body.note + '. ') if body.note else ''}"
            "Tell the caller, in their language, that a staff member is joining now, then stop speaking.")
    return {"ok": True, "handoff": row, "relayed_to_live_call": bool(call)}


@app.post("/api/reset")
def reset(_staff: str = Depends(require_staff)):
    clinic.init_db(reset=True)
    hub.publish({"type": "state_changed"})
    return {"ok": True}


@app.websocket("/ws/ops")
async def ws_ops(ws: WebSocket):
    if not staff_from_token(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return
    await ws.accept()
    q = hub.subscribe()
    try:
        while True:
            ev = await q.get()
            await ws.send_text(json.dumps(ev, ensure_ascii=False, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(q)


@app.websocket("/ws/call")
async def ws_call(ws: WebSocket):
    await ws.accept()
    call = Call(ws)
    if not API_KEY:
        await call.send({"type": "error", "message": "GEMINI_API_KEY is not set on the server (.env)."})
        await ws.close()
        return
    CALLS[call.id] = call
    hub.publish({"type": "state_changed"})
    try:
        client = make_client()
        async with contextlib.AsyncExitStack() as stack:
            session, model_used = await open_live(client, stack)
            call.session = session
            await call.emit({"type": "call_started", "model": model_used})
            await session.send_realtime_input(text=prompts.CALL_CONNECTED)
            tasks = {asyncio.create_task(from_browser(call)), asyncio.create_task(from_gemini(call))}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                    raise t.exception()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("call %s failed", call.id)
        await call.send({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        CALLS.pop(call.id, None)
        call.session = None
        summary = call.summary()
        clinic.save_call(summary)
        hub.publish({"type": "call_ended", "call_id": call.id, "summary": summary})
        hub.publish({"type": "state_changed"})
        with contextlib.suppress(Exception):
            await ws.close()
