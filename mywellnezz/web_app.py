"""
myWellnezz Web Service
FastAPI application providing a web interface for gym class booking.
Run with: uvicorn web_app:app --host 0.0.0.0 --port 8080
Or via entry point: mywellnezz-web
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional, List

from loguru import logger

# Ensure mywellnezz package modules are importable when running directly
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from models.config import Config, read_config, write_config, add_user, remove_user
from models.facility import my_facilities
from models.mywellnezz import MyWellnezz
from models.usercontext import UserContext
from modules.math_util import write_obfuscation

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
mw = MyWellnezz()
_ws_clients: List[WebSocket] = []
_bg_task: Optional[asyncio.Task] = None
_broadcaster_task: Optional[asyncio.Task] = None

_templates = Jinja2Templates(directory=os.path.join(_pkg_dir, "templates"))


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_task, _broadcaster_task
    logger.add("mywellness.log", rotation="10 MB", retention="7 days")
    _broadcaster_task = asyncio.create_task(_broadcaster())
    c = read_config()
    if c.user_choice is not None and c.facility_choice is not None:
        _bg_task = asyncio.create_task(_bg_loop())
    yield
    mw.run = False
    for task in (_bg_task, _broadcaster_task):
        if task and not task.done():
            task.cancel()


app = FastAPI(title="myWellnezz", version="1.2.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def _bg_loop():
    """
    Main background loop: keeps the event list up-to-date and manages
    booking tasks.  Reads config fresh from disk on every iteration so
    that changes made through the web UI are picked up automatically.
    """
    while mw.run:
        try:
            c = read_config()
            if c.user_choice is None or c.facility_choice is None:
                await asyncio.sleep(5)
                continue
            user = c.get_user()
            # Hydrate facilities list when absent (first run or after reload)
            if not user.facilities:
                user.facilities = await my_facilities(user) or []
                if user.facilities:
                    c.users[c.user_choice] = user
                    write_config(c)
            if not user.facilities:
                await asyncio.sleep(10)
                continue
            facility = await c.get_facility()
            mw.set_event_task(user, facility, c)
        except Exception as exc:
            logger.error(f"BG loop error: {exc}")
        await asyncio.sleep(5)


async def _broadcaster():
    """Push live event snapshots to all connected WebSocket clients."""
    while True:
        try:
            if _ws_clients:
                events = await mw.get_events()
                payload = {
                    "type": "events_update",
                    "events": [_event_to_dict(e) for e in events.values()],
                }
                dead: List[WebSocket] = []
                for ws in list(_ws_clients):
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    _ws_clients.remove(ws)
        except Exception as exc:
            logger.error(f"Broadcaster error: {exc}")
        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _event_to_dict(e) -> dict:
    return {
        "uid": e.uid,
        "name": e.name or "",
        "room": e.room or "",
        "assigned_to": e.assigned_to or "",
        "start": e.start.isoformat() if e.start else None,
        "end": e.end.isoformat() if e.end else None,
        "booking_opens_on": e.booking_opens_on.isoformat() if e.booking_opens_on else None,
        "status": e.status or "Unknown",
        "available_places": e.available_places or 0,
        "max_participants": e.max_participants or 0,
        "is_participant": bool(e.is_participant),
        "is_in_waiting_list": bool(e.is_in_waiting_list),
        "can_book": bool(e.can_book),
        "booking_available": bool(e.booking_available),
    }


async def _ensure_bg_running():
    """Start background loop if not already running."""
    global _bg_task
    if _bg_task is None or _bg_task.done():
        _bg_task = asyncio.create_task(_bg_loop())


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: str
    password: str


class SelectRequest(BaseModel):
    index: int


class AutoBookRequest(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Routes – UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _templates.TemplateResponse("index.html", {"request": request})


# ---------------------------------------------------------------------------
# Routes – API
# ---------------------------------------------------------------------------
@app.get("/api/status")
async def get_status():
    c = read_config()
    current_user = None
    current_facility = None
    if c.user_choice is not None and c.users:
        u = c.get_user()
        current_user = {
            "email": u.usr,
            "name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
        }
        if c.facility_choice is not None and u.facilities and c.facility_choice < len(u.facilities):
            f = u.facilities[c.facility_choice]
            current_facility = {"name": f.name, "address": f.address or ""}
    return {
        "configured": c.user_choice is not None and c.facility_choice is not None,
        "users_count": len(c.users),
        "user_choice": c.user_choice,
        "facility_choice": c.facility_choice,
        "auto_book": c.auto_book,
        "current_user": current_user,
        "current_facility": current_facility,
    }


@app.get("/api/users")
async def get_users():
    c = read_config()
    return {
        "users": [
            {
                "index": i,
                "email": u.usr,
                "name": f"{u.first_name or ''} {u.last_name or ''}".strip(),
            }
            for i, u in enumerate(c.users)
        ]
    }


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    temp = UserContext()
    temp.usr = req.email
    temp.pwd = write_obfuscation(req.email, req.password)
    logged, user = await temp.login_app()
    if not logged:
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    user.pwd = temp.pwd
    c = add_user(user)
    # Auto-select when it is the only account
    if len(c.users) == 1:
        c.user_choice = 0
        write_config(c)
    return {
        "success": True,
        "index": len(c.users) - 1,
        "user": {
            "email": user.usr,
            "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        },
    }


@app.delete("/api/users/{index}")
async def delete_user(index: int):
    c = remove_user(index)
    return {"success": True, "users_count": len(c.users)}


@app.post("/api/users/select")
async def select_user(req: SelectRequest):
    c = read_config()
    if req.index >= len(c.users):
        raise HTTPException(status_code=400, detail="Index utilisateur invalide")
    c.user_choice = req.index
    c.facility_choice = None  # reset facility when switching accounts
    write_config(c)
    return {"success": True}


@app.get("/api/facilities")
async def get_facilities():
    c = read_config()
    if c.user_choice is None or not c.users:
        raise HTTPException(status_code=400, detail="Aucun utilisateur sélectionné")
    user = c.get_user()
    if not user.token:
        ok, user = await user.refresh()
        if not ok:
            raise HTTPException(status_code=401, detail="Authentification échouée")
    facilities = await my_facilities(user)
    if not facilities:
        raise HTTPException(status_code=404, detail="Aucune salle trouvée")
    user.facilities = facilities
    c.users[c.user_choice] = user
    write_config(c)
    return {
        "facilities": [
            {"index": i, "name": f.name, "address": f.address or ""}
            for i, f in enumerate(facilities)
        ]
    }


@app.post("/api/facilities/select")
async def select_facility(req: SelectRequest):
    c = read_config()
    if c.user_choice is None:
        raise HTTPException(status_code=400, detail="Aucun utilisateur sélectionné")
    user = c.get_user()
    if not user.facilities:
        user.facilities = await my_facilities(user) or []
    if req.index >= len(user.facilities):
        raise HTTPException(status_code=400, detail="Index de salle invalide")
    c.facility_choice = req.index
    write_config(c)
    await _ensure_bg_running()
    return {"success": True, "facility": user.facilities[req.index].name}


@app.get("/api/events")
async def get_events_api():
    c = read_config()
    if c.user_choice is None or c.facility_choice is None:
        return {"events": [], "configured": False}
    events = await mw.get_events()
    if not events:
        try:
            user = c.get_user()
            if not user.facilities:
                user.facilities = await my_facilities(user) or []
            facility = await c.get_facility()
            events = await mw.set_events(user, facility)
        except Exception as exc:
            logger.error(f"Events fetch error: {exc}")
    return {"events": [_event_to_dict(e) for e in (events or {}).values()], "configured": True}


@app.post("/api/events/{uid}/toggle")
async def toggle_booking(uid: str):
    c = read_config()
    if c.user_choice is None or c.facility_choice is None:
        raise HTTPException(status_code=400, detail="Service non configuré")
    user = c.get_user()
    if not user.facilities:
        user.facilities = await my_facilities(user) or []
    facility = await c.get_facility()
    try:
        event = await mw.get_event(uid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Cours introuvable")
    await mw.set_book_task(user, facility, event)
    return {"success": True, "event": event.name, "status": event.status}


@app.post("/api/events/refresh")
async def refresh_events_api():
    c = read_config()
    if c.user_choice is None or c.facility_choice is None:
        raise HTTPException(status_code=400, detail="Service non configuré")
    user = c.get_user()
    if not user.facilities:
        user.facilities = await my_facilities(user) or []
    facility = await c.get_facility()
    events = await mw.set_events(user, facility)
    return {"events": [_event_to_dict(e) for e in (events or {}).values()]}


@app.post("/api/autobook")
async def set_autobook(req: AutoBookRequest):
    c = read_config()
    c.auto_book = req.enabled
    write_config(c)
    return {"auto_book": c.auto_book}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def start():
    import uvicorn

    host = os.environ.get("MYWELLNEZZ_HOST", "0.0.0.0")
    port = int(os.environ.get("MYWELLNEZZ_PORT", "8080"))
    logger.info(f"Starting myWellnezz web service on {host}:{port}")
    uvicorn.run(
        "mywellnezz.web_app:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    start()
