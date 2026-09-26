"""Reigns engine — FastAPI app (FR-B1, FR-B2, FR-B9, FR-B10).

Run:  cd engine && uvicorn app.main:app --port 8765 --reload
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app import plugins
from app import voice
from app.learning import store
from app.ledger import Ledger
from app.models import INBOUND_TYPES, PAYLOAD_MODELS, Envelope, ErrorPayload, MessageNew
from app.session import Session, envelope

VERSION = "1.0"
ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "shared" / "fixtures" / "scenarios"


# ---------------------------------------------------------------------------- logging (FR-B10)
class JsonFormatter(logging.Formatter):
    _std = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": self.formatTime(record), "level": record.levelname,
                "logger": record.name, "msg": record.getMessage()}
        data.update({k: v for k, v in vars(record).items() if k not in self._std})
        return json.dumps(data, default=str)


def _setup_logging() -> None:
    log_dir = ROOT / "engine" / "logs"
    log_dir.mkdir(exist_ok=True)
    root = logging.getLogger("reigns")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(log_dir / "engine.log")
    fh.setFormatter(JsonFormatter())
    sh = logging.StreamHandler()
    sh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    root.addHandler(sh)


_setup_logging()
log = logging.getLogger("reigns.main")

# ---------------------------------------------------------------------------- app
ledger = Ledger()
debug_sessions: dict[str, Session] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    await ledger.open()
    # FR-L1: connect to Atlas in the background so a slow/unreachable cluster never delays startup
    startup_task = asyncio.create_task(store.startup())
    voice_task = asyncio.create_task(voice.warm_cache())  # FR-V2: pre-generate common lines
    log.info("engine up", extra={"detectors": plugins.available_detectors()})
    yield
    startup_task.cancel()
    voice_task.cancel()
    await ledger.close()


app = FastAPI(title="Reigns engine", version=VERSION, lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": VERSION}


@app.get("/debug/status")
async def debug_status() -> dict:
    """Which teammates' modules are plugged in right now."""
    return {"detectors": plugins.available_detectors(),
            "course_correct": plugins._optional_import("app.course_correct.api") is not None,
            "mongo": await store.ping(), "mongo_queued": store.queued(),
            "voice": voice.enabled()}


@app.post("/debug/voice")
async def debug_voice(text: str = "Heads up: a cited source could not be confirmed.") -> dict:
    """Say `text` with ElevenLabs and save it to engine/logs/voice_test.mp3 so you can play it:
    curl -X POST "localhost:8765/debug/voice?text=Hello"  then  afplay logs/voice_test.mp3"""
    if not voice.enabled():
        raise HTTPException(400, "voice is off: set ELEVENLABS_API_KEY and REIGNS_VOICE_ID")
    t0 = time.perf_counter()
    cached = voice._cache_path(text).exists()
    audio = await voice.synthesize(text)
    if not audio:
        raise HTTPException(502, "ElevenLabs call failed; see the engine log for the reason")
    out = ROOT / "engine" / "logs" / "voice_test.mp3"
    out.write_bytes(audio)
    return {"ok": True, "bytes": len(audio), "cached": cached,
            "ms": int((time.perf_counter() - t0) * 1000), "saved_to": str(out)}


def parse_inbound(raw: Any) -> tuple[Envelope, Any]:
    """Validate envelope + payload (FR-B2). Raises ValueError with a readable message."""
    try:
        env = Envelope.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"bad envelope: {exc.errors()[0]['msg']} at {exc.errors()[0]['loc']}")
    if env.type not in INBOUND_TYPES:
        raise ValueError(f"'{env.type}' is not a companion → engine message type")
    try:
        payload = PAYLOAD_MODELS[env.type].model_validate(env.payload)
    except ValidationError as exc:
        err = exc.errors()[0]
        raise ValueError(f"bad {env.type} payload: {err['msg']} at {list(err['loc'])}")
    return env, payload


# ---------------------------------------------------------------------------- WebSocket
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    send_lock = asyncio.Lock()
    session: Optional[Session] = None
    tasks: set[asyncio.Task] = set()

    async def send(env: Envelope) -> None:
        async with send_lock:
            await ws.send_text(env.model_dump_json())

    async def send_error(sid: str, code: str, message: str) -> None:
        await send(envelope("error", sid, ErrorPayload(code=code, message=message)))

    async def ticker() -> None:  # hysteresis / decay / recovered changes (§9)
        while True:
            await asyncio.sleep(0.5)
            if session:
                env = session.heat_envelope_if_changed()
                if env:
                    await send(env)

    async def process(env: Envelope, payload: Any) -> None:
        try:
            for out in await session.handle(env, payload):
                await send(out)
        except Exception as exc:  # §14 rule 7: engine sends an error, never dies
            log.exception("processing failed")
            await send_error(session.sid, "engine_error", f"{type(exc).__name__}: {exc}")

    tick_task = asyncio.create_task(ticker())
    try:
        while True:
            text = await ws.receive_text()
            try:
                env, payload = parse_inbound(json.loads(text))
            except (ValueError, json.JSONDecodeError) as exc:
                await send_error(session.sid if session else "unknown", "invalid_message", str(exc))
                continue
            if session is None:  # single session per connection (FR-B1)
                session = Session(env.session_id, ledger)
                session.voice_sink = send
            t = asyncio.create_task(process(env, payload))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    except WebSocketDisconnect:
        pass
    finally:
        tick_task.cancel()
        for t in tasks:
            t.cancel()


# ---------------------------------------------------------------------------- debug (FR-B9)
def _debug_session(session_id: str) -> Session:
    if session_id not in debug_sessions:
        debug_sessions[session_id] = Session(session_id, ledger)
    return debug_sessions[session_id]


@app.post("/debug/message")
async def debug_message(body: dict = Body(...), session_id: str = "debug") -> dict:
    """Same body as message.new (bare payload, or a full envelope). Returns everything the
    engine would have sent over the WebSocket. Reuse `?session_id=` to build a conversation."""
    if "payload" in body and "type" in body:
        try:
            env, payload = parse_inbound(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        session_id = env.session_id
    else:
        try:
            payload = MessageNew.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, str(exc.errors()[0]))
        env = envelope("message.new", session_id, payload)
    t0 = time.perf_counter()
    s = _debug_session(session_id)
    outputs = await s.handle(env, payload)
    return {
        "session_id": session_id,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "target_level": s.heat.target_level(s.clock()),
        "outputs": [json.loads(o.model_dump_json()) for o in outputs],
    }


@app.post("/debug/reset")
async def debug_reset(session_id: str = "debug") -> dict:
    debug_sessions.pop(session_id, None)
    return {"ok": True}


@app.get("/debug/scenarios")
async def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIOS.glob("*.json"))


@app.post("/debug/scenario/{name}")
async def run_scenario(name: str) -> dict:
    """Play every companion message of a /shared/fixtures scenario through a fresh session."""
    path = SCENARIOS / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, f"no scenario {name}; see GET /debug/scenarios")
    scenario = json.loads(path.read_text())
    sid = f"scenario-{name}-{int(time.time())}"
    s = _debug_session(sid)
    outputs = []
    t0 = time.perf_counter()
    for raw in scenario["companion_to_engine"]:
        raw = {**raw, "session_id": sid}
        if raw["type"] == "correction.inserted" and s.ctx.corrections:
            # fixture ids differ from runtime ids: insert the correction the engine just offered
            raw["payload"] = {"correction_id": s.ctx.corrections[-1].correction_id}
        env, payload = parse_inbound(raw)
        outputs += [json.loads(o.model_dump_json()) for o in await s.handle(env, payload)]
    return {"session_id": sid, "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "target_level": s.heat.target_level(s.clock()), "outputs": outputs}
