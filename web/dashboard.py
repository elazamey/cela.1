"""لوحة تحكم Celia Repo Agent — FastAPI + SQLite + JWT + WebSocket.

Dashboard server for Celia Repo Agent.

Features
  * REST API (``/api/...``) backed by the shared SQLite recorder.
  * JWT auth: ``POST /api/auth`` with ``{"token": <CELIA_DASH_TOKEN>}`` returns
    a signed access token; protected endpoints require ``Authorization:
    Bearer <jwt>`` (or ``?token=<jwt>`` for the WebSocket).
  * WebSocket ``/ws/events`` streams new events live (server-side events from
    the demo generator are broadcast in-process; events written by a separate
    ``agent.py`` process are picked up by the UI polling).
  * ``static/index.html`` single-page Tailwind UI served at ``/``.

Run (from the repository root):

    uvicorn web.dashboard:app --host 0.0.0.0 --port 8000

Security notes
  * ``CELIA_DASH_TOKEN`` configures the shared secret; when it is *not* set the
    server starts in DEMO MODE (auth is bypassed) and seeds sample data so the
    UI is explorable — always set a real token outside local demos.
"""

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from web.recorder import Recorder, demo_seed, now_iso

logger = logging.getLogger("celia-dashboard")

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"

DEMO_TOKEN = "demo"  # مستخدم فقط عند غياب CELIA_DASH_TOKEN.

_secret = os.environ.get("CELIA_DASH_TOKEN", "").strip()
DEMO_MODE = not _secret
if DEMO_MODE:
    logger.warning(
        "⚠️ CELIA_DASH_TOKEN غير مضبوط — تعمل اللوحة في DEMO MODE (بدون تحقق)."
    )


def _jwt_secret() -> str:
    return _secret or DEMO_TOKEN


# --------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------- #
def create_access_token() -> str:
    payload = {
        "sub": "dashboard",
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def verify_access_token(token: str) -> bool:
    if not token:
        return DEMO_MODE
    if DEMO_MODE:
        return token == DEMO_TOKEN or _looks_like_jwt(token)
    try:
        jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
        return True
    except jwt.PyJWTError:
        return False


def _looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2


# --------------------------------------------------------------------- #
# Recorder singleton
# --------------------------------------------------------------------- #
_recorder = Recorder()


class ConnectionManager:
    """يبث الأحداث الجديدة لكل عملاء WebSocket المتصلين."""

    def __init__(self) -> None:
        self.active: List[WebSocket] = []
        self._lock = threading.Lock()
        self._last_id = 0

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        with self._lock:
            self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            sockets = list(self.active)
        dead = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# --------------------------------------------------------------------- #
# App / schemas
# --------------------------------------------------------------------- #
app = FastAPI(title="Celia Repo Agent Dashboard", version="2.0.0")

bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    token: str


class LoginResponse(BaseModel):
    access_token: str
    demo_mode: bool
    token_type: str = "bearer"


class DemoActivityRequest(BaseModel):
    run_id: Optional[int] = None
    events: int = 12


def _current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> str:
    token = credentials.credentials if credentials else ""
    if verify_access_token(token):
        return "dashboard"
    raise HTTPException(status_code=401, detail="توكن غير صالح أو منتهي")


# --------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------- #
@app.post("/api/auth", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    if _secret and body.token != _secret:
        raise HTTPException(status_code=401, detail="توكن اللوحة غير صحيح.")
    if not _secret and body.token != DEMO_TOKEN:
        raise HTTPException(status_code=401, detail="استخدم التوكن: demo")
    return LoginResponse(access_token=create_access_token(), demo_mode=DEMO_MODE)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "demo_mode": DEMO_MODE, "time": now_iso()}


# --------------------------------------------------------------------- #
# Runs & events
# --------------------------------------------------------------------- #
@app.get("/api/runs")
def list_runs(
    limit: int = Query(25, ge=1, le=200), _: str = Depends(_current_user)
) -> Dict[str, Any]:
    runs = _recorder.get_runs(limit=limit)
    return {"runs": runs, "stats": _recorder.stats()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, _: str = Depends(_current_user)) -> Dict[str, Any]:
    run = _recorder.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="التشغيل غير موجود")
    return {"run": run}


@app.get("/api/events")
def list_events(
    run_id: Optional[int] = None,
    after_id: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    _: str = Depends(_current_user),
) -> Dict[str, Any]:
    events = _recorder.get_events(run_id=run_id, after_id=after_id, limit=limit)
    return {"events": events, "after_id": after_id}


@app.get("/api/events/latest")
def latest_event_id(_: str = Depends(_current_user)) -> Dict[str, int]:
    events = _recorder.get_events(run_id=None, after_id=0, limit=1)
    return {"latest_id": events[-1]["id"] if events else 0}


# --------------------------------------------------------------------- #
# Demo generator (dev/preview aid)
# --------------------------------------------------------------------- #
@app.post("/api/demo/activity")
async def generate_demo_activity(
    body: DemoActivityRequest, _: str = Depends(_current_user)
) -> Dict[str, Any]:
    """يولّد أحداثاً تجريبية (محاكاة وكيل يعمل) ويبثها حياً عبر WebSocket."""
    events = body.events if 0 < body.events <= 200 else 12
    run_id = body.run_id
    messages = [
        ("🚀 بدء تشغيل وكيل فحص المستودعات...", "info"),
        ("🔍 جلب المستودعات المملوكة للمستخدم", "info"),
        ("✅ لا توجد نواقص ملفات/CI ظاهرة", "success"),
        ("🛡️ رصد 2 ثغرة npm في package.json", "warning"),
        ("🔧 تحديث حتمي: lodash==4.17.21 (dependencies)", "info"),
        ("🎉 تم فتح PR الإصلاح الأمني npm: #12", "success"),
        ("🩺 رصد تشغيل CI فاشل في .github/workflows/ci.yml", "warning"),
        ("🤖 تشخيص Gemini: نسخة setup-node قديمة في الخطوة install", "info"),
        ("🎉 تم فتح PR إصلاح CI: #13", "success"),
        ("📝 تم نشر اقتراح حل على المشكلة #7", "success"),
        ("❌ فشل معالجة مستودع آخر بسبب GitHubException", "error"),
        ("🏁 انتهت عملية الفحص والإصلاح.", "success"),
    ]
    if run_id is None:
        row = _recorder.get_runs(limit=1)
        run_id = row[0]["id"] if row else _recorder.start_run(mode="demo")
    for _ in range(events):
        text, level = random.choice(messages)
        event_id = _recorder.add_event(run_id, level=level, message=text)
        await manager.broadcast(
            {"type": "event", "event": {"id": event_id, "run_id": run_id, "ts": now_iso(), "level": level, "message": text}}
        )
        time.sleep(0.12)
    return {"run_id": run_id, "events_generated": events}


# --------------------------------------------------------------------- #
# WebSocket live events
# --------------------------------------------------------------------- #
@app.websocket("/ws/events")
async def ws_events(ws: WebSocket, token: str = Query("")) -> None:
    if not verify_access_token(token):
        await ws.close(code=4401, reason="unauthorized")
        return
    await manager.connect(ws)
    try:
        while True:
            # أي رسالة من العميل = طلب ping للحفاظ على الاتصال.
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:  # noqa: BLE001
        manager.disconnect(ws)


# --------------------------------------------------------------------- #
# Static UI
# --------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.on_event("startup")
def _startup() -> None:
    demo_seed(_recorder)
    logger.info("قاعدة البيانات: %s", _recorder.db_path)


def main() -> None:
    import uvicorn

    host = os.environ.get("CELIA_DASH_HOST", "0.0.0.0")
    port = int(os.environ.get("CELIA_DASH_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
