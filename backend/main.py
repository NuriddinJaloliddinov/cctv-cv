"""FastAPI app: REST + WebRTC signaling + static frontend."""
import asyncio
import io
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from aiortc import RTCPeerConnection, RTCSessionDescription
from pydantic import BaseModel, Field

from . import db, discovery, capabilities, pipeline, enroll, telegram_bot, review, training, crossing, detection_settings, face_review, auth
from .config import FACE_DETECT_MODEL, FACE_EMBED_MODEL
from .discovery import fetch_rtsp_url
from .security import hash_password, verify_password, dummy_verify, token_digest
from .source import FrameSource
from .models import (
    CameraInfo, CameraCreate, CameraUpdate,
    Capability, DiscoveryReport, DiscoveryRequest, DiscoveryConnectRequest,
    DiscoveredDevice,
    StartRequest, StatusResponse, TelegramSettings,
)
from .webrtc_stream import FrameTrack


# ---------- Constants for enrollment ----------
PERSONS_DIR = Path("data/persons")
PERSONS_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}


class PersonUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None


class ProfilesRequest(BaseModel):
    onvif_user: str | None = None
    onvif_pass: str | None = None


# ---------- App lifecycle ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    telegram_bot.service.start()
    try:
        yield
    finally:
        telegram_bot.service.stop()


app = FastAPI(title="CCTV CV", lifespan=lifespan)

_peers: dict[str, set[RTCPeerConnection]] = {}
_candidate_assignment_lock = asyncio.Lock()


async def _close_peer(camera_id: str, pc: RTCPeerConnection) -> None:
    """Remove one viewer and close only its peer connection."""
    peers = _peers.get(camera_id)
    if peers is not None:
        peers.discard(pc)
        if not peers:
            _peers.pop(camera_id, None)
    try:
        await pc.close()
    except Exception:
        pass


async def _close_camera_peers(camera_id: str) -> None:
    """Close every viewer connected to a camera."""
    peers = _peers.pop(camera_id, None)
    if not peers:
        return
    for pc in list(peers):
        try:
            await pc.close()
        except Exception:
            pass


class ReviewLabel(BaseModel):
    label: str


class ReviewBox(BaseModel):
    box: list[int] | None


class ReviewCapture(BaseModel):
    enabled: bool
    scope: Literal["global", "exclusive"] = "global"
    max_per_hour: int | None = Field(default=None, ge=1, le=120)
    camera_ids: list[str] = Field(default_factory=list)


class DetectionSettingsUpdate(BaseModel):
    scope: Literal["global", "exclusive"]
    person: bool
    phone: bool
    camera_ids: list[str] = Field(default_factory=list)


class FaceReviewSettingsUpdate(BaseModel):
    scope: Literal["global", "exclusive"]
    enabled: bool
    per_hour: int = Field(ge=1, le=30)
    camera_ids: list[str] = Field(default_factory=list)


class CandidateAssignment(BaseModel):
    person_id: str | None = None
    name: str | None = None
    notes: str = ""


# ---------- Authentication models ----------

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=4, max_length=256)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=4, max_length=256)
    role: Literal["admin", "reader"] = "reader"
    active: bool = True


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=32)
    password: str | None = Field(default=None, min_length=4, max_length=256)
    role: Literal["admin", "reader"] | None = None
    active: bool | None = None


# ---------- Authentication middleware ----------

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SELF_POST_PATHS = {"/api/auth/logout", "/api/auth/password"}
_READER_POST_PREFIXES = ("/api/webrtc/offer/",)
_ADMIN_GET_PREFIXES = (
    "/api/users", "/api/auth/audit", "/api/discover", "/api/telegram",
    "/api/enrollment/review", "/api/training", "/api/detection",
)


def _same_origin(request: Request) -> bool:
    """Reject cross-site state-changing requests as a CSRF defence."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host") or ""
    return urlsplit(origin).netloc.lower() == host.lower()


def _reader_may_post(path: str) -> bool:
    return path in _SELF_POST_PATHS or any(
        path.startswith(prefix) for prefix in _READER_POST_PREFIXES)


def _admin_required(method: str, path: str) -> bool:
    if method in _SAFE_METHODS:
        return any(path == prefix or path.startswith(prefix + "/")
                   for prefix in _ADMIN_GET_PREFIXES)
    return not _reader_may_post(path)


def _with_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    # The flag exists only for isolated unit tests that exercise non-auth
    # endpoints; it is never set by the application itself.
    if getattr(app.state, "auth_disabled", False):
        return _with_security_headers(await call_next(request))

    path = request.url.path
    if path != "/api/auth/login" and not _same_origin(request):
        return JSONResponse({"detail": "Cross-site request blocked."},
                            status_code=403)

    token = request.cookies.get(auth.SESSION_COOKIE)
    user = auth.session_user(token) if token else None
    if path == "/api/auth/login":
        return _with_security_headers(await call_next(request))

    if path.startswith("/api/"):
        if user is None:
            return JSONResponse({"detail": "Sign in to continue."},
                                status_code=401)
        request.state.user = user
        if _admin_required(request.method, path) and not auth.is_admin(user):
            return JSONResponse(
                {"detail": "This action requires an administrator account."},
                status_code=403)
        return _with_security_headers(await call_next(request))

    # The dashboard shell must not be served before sign-in. Static assets
    # (CSS, JS, images) are harmless and are needed by the login page too.
    if path in ("/", "/index.html") and user is None:
        return RedirectResponse("/login.html", status_code=302)
    return _with_security_headers(await call_next(request))



# ---------- Authentication and accounts ----------

@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    username = body.username.strip()
    user = db.get_user_by_username(username)
    ip = auth.request_ip(request)
    if user is None:
        dummy_verify(body.password)
        db.record_auth_event(None, username, "login_failed",
                             "unknown account", ip)
        raise HTTPException(401, "Invalid username or password.")

    if user["locked"]:
        db.record_auth_event(user["id"], user["username"], "login_blocked",
                             "account locked", ip)
        raise HTTPException(
            423, "This account is locked after too many failed sign-in "
                 "attempts. Ask an administrator to unlock it.")
    if not user["active"]:
        db.record_auth_event(user["id"], user["username"], "login_disabled",
                             "account disabled", ip)
        raise HTTPException(403, "This account is disabled.")

    if not verify_password(body.password, user["password_hash"]):
        state = db.register_failed_login(user["id"])
        db.record_auth_event(
            user["id"], user["username"], "login_failed",
            f"attempt {state['attempts']} of {state['limit']}", ip)
        if state["locked"]:
            raise HTTPException(
                423, "This account has been locked after too many failed "
                     "sign-in attempts. Ask an administrator to unlock it.")
        remaining = max(0, state["limit"] - state["attempts"])
        noun = "attempt" if remaining == 1 else "attempts"
        raise HTTPException(
            401, "Invalid username or password. "
                 f"{remaining} {noun} left before the account is locked.")

    token, expires = auth.create_session(user)
    db.touch_login(user["id"])
    db.record_auth_event(user["id"], user["username"], "login", "", ip)
    fresh = db.get_user(user["id"])
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=max(
            60, int((expires - datetime.now(timezone.utc)).total_seconds())),
        httponly=True, samesite="strict", path="/",
        secure=auth.is_secure_request(
            request.url.scheme, request.headers.get("x-forwarded-proto")),
    )
    return {"user": auth.public_user(fresh)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    user = request.state.user
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        db.delete_auth_session(token_digest(token))
    db.record_auth_event(user["id"], user["username"], "logout", "",
                         auth.request_ip(request))
    response.delete_cookie(
        auth.SESSION_COOKIE, path="/", samesite="strict",
        secure=auth.is_secure_request(
            request.url.scheme, request.headers.get("x-forwarded-proto")))
    return {"ok": True}


@app.get("/api/auth/me")
def current_account(request: Request) -> dict:
    return {"user": auth.public_user(request.state.user)}


@app.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request) -> dict:
    user = db.get_user(request.state.user["id"])
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    if body.current_password == body.new_password:
        raise HTTPException(400, "Choose a different new password.")
    db.update_user(user["id"], {"password_hash": hash_password(
        body.new_password)})
    token = request.cookies.get(auth.SESSION_COOKIE)
    keep = token_digest(token) if token else None
    db.delete_user_sessions(user["id"], keep_token_hash=keep)
    db.record_auth_event(
        user["id"], user["username"], "password_changed",
        "changed own password", auth.request_ip(request))
    return {"ok": True}


@app.get("/api/users")
def list_users(request: Request) -> dict:
    return {"users": db.list_users(),
            "current_user_id": request.state.user["id"]}


@app.post("/api/users", status_code=201)
def create_user(body: UserCreate, request: Request) -> dict:
    try:
        username = auth.normalize_username(body.username)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if db.get_user_by_username(username):
        raise HTTPException(409, "That username is already in use.")
    user = db.create_user(
        username, hash_password(body.password), body.role, body.active,
        created_by=request.state.user["username"])
    db.record_auth_event(
        request.state.user["id"], request.state.user["username"],
        "user_created", f"created {username} ({body.role})",
        auth.request_ip(request))
    return auth.public_user(user)


@app.put("/api/users/{user_id}")
def update_user(user_id: str, body: UserUpdate, request: Request) -> dict:
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(404, "Account not found.")
    patch = body.model_dump(exclude_unset=True)
    current = request.state.user
    is_self = target["id"] == current["id"]

    if "username" in patch and patch["username"] is not None:
        try:
            patch["username"] = auth.normalize_username(patch["username"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        existing = db.get_user_by_username(patch["username"])
        if existing and existing["id"] != target["id"]:
            raise HTTPException(409, "That username is already in use.")

    if "role" in patch and patch["role"] is not None:
        if is_self and patch["role"] != target["role"]:
            raise HTTPException(400, "You cannot change your own role.")
        if target["role"] == "admin" and patch["role"] != "admin" and \
                db.count_active_admins(exclude_id=target["id"]) == 0:
            raise HTTPException(
                409, "At least one active administrator must remain.")

    if "active" in patch and patch["active"] is not None:
        if is_self and not patch["active"]:
            raise HTTPException(400, "You cannot disable your own account.")
        if target["role"] == "admin" and not patch["active"] and \
                db.count_active_admins(exclude_id=target["id"]) == 0:
            raise HTTPException(
                409, "At least one active administrator must remain.")

    if "password" in patch:
        password = patch.pop("password")
        if password:
            patch["password_hash"] = hash_password(password)
            patch["failed_attempts"] = 0
            patch["locked"] = False

    updated = db.update_user(target["id"], patch)
    if updated is None:
        raise HTTPException(404, "Account not found.")
    if patch.get("password_hash") or patch.get("active") is False:
        db.delete_user_sessions(target["id"])
    db.record_auth_event(
        current["id"], current["username"], "user_updated",
        f"updated {target['username']}: {', '.join(sorted(patch)) or 'nothing'}",
        auth.request_ip(request))
    return auth.public_user(updated)


@app.post("/api/users/{user_id}/unlock")
def unlock_user(user_id: str, request: Request) -> dict:
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(404, "Account not found.")
    updated = db.unlock_user(user_id)
    db.record_auth_event(
        request.state.user["id"], request.state.user["username"],
        "user_unlocked", f"unlocked {target['username']}",
        auth.request_ip(request))
    return auth.public_user(updated)


@app.get("/api/auth/audit")
def auth_audit(limit: int = 100) -> dict:
    return {"events": db.list_auth_audit(limit)}


# ---------- Discovery / capabilities ----------

@app.post("/api/discover", response_model=DiscoveryReport)
def discover(body: DiscoveryRequest | None = None) -> DiscoveryReport:
    """Find devices without submitting any credentials to them."""
    request = body or DiscoveryRequest()
    try:
        return discovery.discover_devices(request)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/discover", response_model=DiscoveryReport)
def discover_get(subnet: str | None = None,
                  deep: bool = False) -> DiscoveryReport:
    """Compatibility endpoint: anonymous discovery."""
    request = DiscoveryRequest(
        subnets=[subnet] if subnet else [],
        include_saved=True,
        deep=deep,
    )
    try:
        return discovery.discover_devices(request)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/discover/connect", response_model=DiscoveredDevice)
def connect_discovered(body: DiscoveryConnectRequest) -> DiscoveredDevice:
    """Check only the selected endpoint after the operator enters a login."""
    try:
        return discovery.connect_discovered_device(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/capabilities")
def caps() -> list[Capability]:
    return capabilities.probe_devices()


# ---------- Camera CRUD ----------

@app.get("/api/cameras", response_model=list[CameraInfo])
def list_cameras():
    return db.list_cameras()


@app.get("/api/cameras/{camera_id}", response_model=CameraInfo)
def get_camera(camera_id: str):
    cam = db.get_camera(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    return cam


@app.post("/api/cameras", response_model=CameraInfo, status_code=201)
def create_camera(body: CameraCreate):
    try:
        crossing.validate(body.role, body.tripwire, body.tripwire_direction)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if db.find_by_address(body.address):
        raise HTTPException(409, "A camera with this address already exists")
    return db.create_camera(body.model_dump())


@app.put("/api/cameras/{camera_id}", response_model=CameraInfo)
def update_camera(camera_id: str, body: CameraUpdate):
    patch = body.model_dump(exclude_unset=True)
    current = db.get_camera(camera_id)
    if current is None:
        raise HTTPException(404, "Camera not found")
    if "role" in patch and patch["role"] is None:
        patch.update(tripwire=None, tripwire_direction=None)
    if patch.get("role", current.role) is None:
        # A customer flag only makes sense on an entrance/exit camera.
        patch["count_customer"] = False
    try:
        crossing.validate(patch.get("role", current.role),
                          patch.get("tripwire", current.tripwire),
                          patch.get("tripwire_direction", current.tripwire_direction))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    required = {"name", "address", "source_type", "device", "fps"}
    for field in required & patch.keys():
        if not patch[field]:
            raise HTTPException(400, f"'{field}' cannot be empty")
    for field, low, high in (("phone_conf", 0.02, 0.95),
                             ("phone_pitch", 0.10, 0.95)):
        if field in patch and not low <= float(patch[field]) <= high:
            raise HTTPException(
                400, f"'{field}' must be between {low} and {high}")
    if "address" in patch:
        existing = db.find_by_address(patch["address"])
        if existing and existing.id != camera_id:
            raise HTTPException(409, "A camera with this address already exists")
    cam = db.update_camera(camera_id, patch)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if {"role", "tripwire", "tripwire_direction", "count_customer"} & patch.keys():
        running = pipeline.get_pipeline(camera_id)
        if running is not None:
            if {"role", "tripwire", "tripwire_direction"} & patch.keys():
                running.set_crossing(cam.role, cam.tripwire, cam.tripwire_direction)
            if "count_customer" in patch.keys():
                running.set_count_customer(cam.count_customer)
    return cam


@app.get("/api/cameras/{camera_id}/snapshot")
def camera_snapshot(camera_id: str):
    """Fresh saved-camera frame for drawing a line, with no stored preview."""
    cam = db.get_camera(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if not cam.rtsp_url:
        raise HTTPException(400, "Connect the camera and resolve its feed first")
    running = pipeline.get_pipeline(camera_id)
    frame = None
    if (running is not None and
            (running._source.status().get("capture_age_ms") or 999999) < 5000):
        _, frame = running._source.latest()
    if frame is None:
        source = FrameSource(cam.rtsp_url)
        source.start()
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                _, frame = source.latest()
                if frame is not None:
                    break
                time.sleep(.05)
        finally:
            source.stop()
    if frame is None:
        raise HTTPException(503, "Could not get a frame; check the camera feed")
    height, width = frame.shape[:2]
    if width > 1200:
        frame = cv2.resize(frame, (1200, int(height * 1200 / width)),
                           interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".jpg", frame,
                            [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise HTTPException(503, "Could not encode camera frame")
    return Response(content=data.tobytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.delete("/api/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    if not db.delete_camera(camera_id):
        raise HTTPException(404, "Camera not found")
    detection_settings.remove_camera(camera_id)
    face_review.remove_camera(camera_id)
    pipeline.stop_pipeline(camera_id)
    await _close_camera_peers(camera_id)
    return {"ok": True}


@app.post("/api/cameras/{camera_id}/rtsp")
def resolve_camera_rtsp(camera_id: str):
    cam = db.get_camera(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if cam.source_type != "onvif":
        raise HTTPException(400, "Only ONVIF cameras need URL resolution")
    try:
        url = fetch_rtsp_url(cam.address, cam.onvif_user or "", cam.onvif_pass or "")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    db.update_camera(camera_id, {"rtsp_url": url})
    return {"rtsp_url": url}


@app.post("/api/cameras/{camera_id}/profiles")
def camera_profiles(camera_id: str, body: ProfilesRequest | None = None) -> list[dict]:
    """List every media profile (NVR channel) and test its video feed.

    Uses credentials supplied by the user, or the ones already saved for
    the camera. Each profile is reported as accessible only when a frame
    can actually be decoded from its RTSP URI.
    """
    cam = db.get_camera(camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if cam.source_type != "onvif":
        raise HTTPException(400, "Only ONVIF cameras expose media profiles")

    user = body.onvif_user if body and body.onvif_user is not None else cam.onvif_user
    password = body.onvif_pass if body and body.onvif_pass is not None else cam.onvif_pass
    try:
        profiles = discovery.fetch_stream_profiles(cam.address, user or "", password or "")
    except RuntimeError as e:
        raise HTTPException(400, str(e))

    def verify(profile: dict) -> dict:
        if not profile.get("rtsp_url"):
            return {**profile, "accessible": False}
        return {**profile, **discovery.check_stream(profile["rtsp_url"])}

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(verify, profiles))


# ---------- Pipeline ----------

def _detection_payload(settings):
    return {**settings, "effective": {
        cam.id: detection_settings.effective(cam.id, settings)
        for cam in db.list_cameras()}}


@app.get("/api/detection/settings")
def get_detection_settings():
    return _detection_payload(detection_settings.policy())


@app.put("/api/detection/settings")
def update_detection_settings(body: DetectionSettingsUpdate):
    if body.scope == "exclusive":
        if not body.camera_ids or len(body.camera_ids) > 100:
            raise HTTPException(400, "Select between 1 and 100 saved cameras")
        for camera_id in body.camera_ids:
            if db.get_camera(camera_id) is None:
                raise HTTPException(404, f"Camera not found: {camera_id}")
    settings = detection_settings.save(body.scope, body.person, body.phone,
                                       body.camera_ids)
    for cam in db.list_cameras():
        running = pipeline.get_pipeline(cam.id)
        if running is not None:
            flags = detection_settings.effective(cam.id, settings)
            running.set_detection(flags["person"], flags["phone"])
    return _detection_payload(settings)

@app.post("/api/start", response_model=StatusResponse)
def start(req: StartRequest) -> StatusResponse:
    cam = db.get_camera(req.camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if not cam.rtsp_url:
        raise HTTPException(400, "No RTSP URL — resolve it first")

    device = req.device or cam.device or "auto"
    fps = req.fps or cam.fps or 10
    flags = detection_settings.effective(cam.id)

    pipeline.start_pipeline(cam.id, cam.name, cam.rtsp_url, device, fps,
                            phone_conf=cam.phone_conf,
                            phone_pitch=cam.phone_pitch,
                            role=cam.role, tripwire=cam.tripwire,
                            tripwire_direction=cam.tripwire_direction,
                            count_customer=cam.count_customer,
                            person_detection=flags["person"],
                            phone_use_detection=flags["phone"])
    if device != cam.device or fps != cam.fps:
        db.update_camera(cam.id, {"device": device, "fps": fps})

    return StatusResponse(running=True, camera_id=cam.id, device=device, fps=fps)


@app.post("/api/stop/{camera_id}")
async def stop(camera_id: str) -> StatusResponse:
    pipeline.stop_pipeline(camera_id)
    await _close_camera_peers(camera_id)
    return StatusResponse(running=False, camera_id=camera_id)


@app.get("/api/status")
def status() -> dict:
    return {"active": pipeline.list_pipelines()}


# ---------- WebRTC ----------

@app.post("/api/webrtc/offer/{camera_id}")
async def webrtc_offer(camera_id: str, data: dict) -> dict:
    p = pipeline.get_pipeline(camera_id)
    if p is None:
        return {"error": "camera not running"}

    sdp = data.get("sdp")
    offer_type = data.get("type")
    if not sdp or not offer_type:
        raise HTTPException(400, "Invalid video offer.")

    peers = _peers.setdefault(camera_id, set())
    # Drop stale entries from earlier tabs/devices that did not close cleanly.
    for stale in [item for item in peers
                  if item.connectionState in ("failed", "closed")]:
        peers.discard(stale)
        try:
            await stale.close()
        except Exception:
            pass

    # Every viewer gets its own peer connection; a new device must never
    # replace or disconnect viewers that are already watching.
    pc = RTCPeerConnection()
    peers.add(pc)
    pc.addTrack(FrameTrack(p, p.fps))

    @pc.on("connectionstatechange")
    async def _on_state():
        if pc.connectionState in ("failed", "closed"):
            await _close_peer(camera_id, pc)

    try:
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as exc:
        # Never leave a half-negotiated viewer attached to the pipeline.
        await _close_peer(camera_id, pc)
        raise HTTPException(
            400, "Could not establish the video connection.") from exc
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


# ---------- Persons (face enrollment) ----------

@app.get("/api/face-engine")
def face_engine_status():
    """Tell the enrollment UI whether the required face models are installed."""
    if not enroll.is_available():
        return {"available": False, "detail":
                "OpenCV face support is missing. Run setup.ps1 and restart the app."}
    if not Path(FACE_DETECT_MODEL).is_file() or not Path(FACE_EMBED_MODEL).is_file():
        return {"available": False, "detail":
                "Face models are missing. Run setup.ps1 to download them, then restart the app."}
    return {"available": True, "detail": ""}


def _require_face_engine():
    status = face_engine_status()
    if not status["available"]:
        raise HTTPException(503, status["detail"])


def _face_review_payload(settings):
    return {**settings, "effective": {
        cam.id: {
            **face_review.effective(cam.id, settings),
            "enabled": (face_review.effective(cam.id, settings)["enabled"]
                        and detection_settings.effective(cam.id)["person"]),
        } for cam in db.list_cameras()}}


@app.get("/api/enrollment/review/settings")
def face_review_settings():
    return _face_review_payload(face_review.policy())


@app.put("/api/enrollment/review/settings")
def update_face_review_settings(body: FaceReviewSettingsUpdate):
    if body.scope == "exclusive":
        if not body.camera_ids or len(body.camera_ids) > 100:
            raise HTTPException(400, "Select between 1 and 100 saved cameras")
        for camera_id in body.camera_ids:
            if db.get_camera(camera_id) is None:
                raise HTTPException(404, f"Camera not found: {camera_id}")
    settings = face_review.save_policy(body.scope, body.enabled,
                                       body.per_hour, body.camera_ids)
    return _face_review_payload(settings)


@app.get("/api/enrollment/review/candidates")
def list_face_review_candidates():
    names = {cam.id: cam.name for cam in db.list_cameras()}
    rows = db.list_face_candidates(limit=300)
    for row in rows:
        row.pop("image_path")
        row["camera_name"] = names.get(row["camera_id"], "Deleted camera")
        row["image_url"] = f"/api/enrollment/review/candidates/{row['id']}/image"
    return {"candidates": rows, "count": db.face_candidate_count()}


@app.get("/api/enrollment/review/candidates/{candidate_id}/image")
def face_review_image(candidate_id: str):
    row = db.get_face_candidate(candidate_id)
    if not row or not Path(row["image_path"]).is_file():
        raise HTTPException(404, "Candidate image not found")
    return FileResponse(row["image_path"], media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.delete("/api/enrollment/review/candidates/{candidate_id}")
async def reject_face_review_candidate(candidate_id: str):
    async with _candidate_assignment_lock:
        if not face_review.discard(candidate_id):
            raise HTTPException(404, "Candidate not found")
    return {"ok": True}


@app.post("/api/enrollment/review/candidates/{candidate_id}/assign")
async def assign_face_review_candidate(candidate_id: str,
                                       body: CandidateAssignment):
    if bool(body.person_id) == bool((body.name or "").strip()):
        raise HTTPException(400, "Choose an existing person or enter a new name")
    _require_face_engine()
    async with _candidate_assignment_lock:
        row = db.get_face_candidate(candidate_id)
        if row is None or not Path(row["image_path"]).is_file():
            raise HTTPException(404, "Candidate image not found")
        if body.person_id and db.get_person(body.person_id) is None:
            raise HTTPException(404, "Person not found")
        raw = Path(row["image_path"]).read_bytes()
        upload = UploadFile(file=io.BytesIO(raw), filename="camera-face.jpg")
        try:
            if body.person_id:
                result = await enroll_more_endpoint(body.person_id, [upload])
                if result["added"] != 1:
                    raise HTTPException(400, "; ".join(result["errors"]) or
                                        "Image no longer passes enrollment checks")
                person_id = body.person_id
            else:
                result = await create_person_endpoint(
                    (body.name or "").strip(), body.notes, [upload])
                person_id = result["id"]
        finally:
            await upload.close()
        face_review.discard(candidate_id)
    return {"person_id": person_id, "added": 1}

def _enrich(person: dict) -> dict:
    person["samples"] = db.count_embeddings(person["id"])
    person["has_thumbnail"] = bool(person.get("thumbnail_path"))
    return person


@app.get("/api/persons")
def list_persons():
    return [_enrich(p) for p in db.list_persons()]


@app.get("/api/persons/{person_id}")
def get_person(person_id: str):
    p = db.get_person(person_id)
    if p is None:
        raise HTTPException(404, "Person not found")
    return _enrich(p)


@app.get("/api/persons/{person_id}/thumbnail")
def get_thumbnail(person_id: str):
    p = db.get_person(person_id)
    if p is None or not p.get("thumbnail_path"):
        raise HTTPException(404, "No thumbnail")
    path = Path(p["thumbnail_path"])
    if not path.exists():
        raise HTTPException(404, "Thumbnail file missing")
    return FileResponse(str(path), media_type="image/jpeg")


async def _samples_from_uploads(files: list[UploadFile]):
    """Turn a list of image/video uploads into [(crop, embedding)] tuples.

    Collects per-file error reasons so the API can tell the user WHY each
    file failed (blurry / no face / multiple faces / unsupported format).
    """
    all_samples, errors = [], []
    for f in files:
        fname = f.filename or "upload"
        try:
            raw = await f.read()
            suffix = Path(fname).suffix.lower()
            if suffix in VIDEO_EXTS:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                try:
                    samples, reason = enroll.extract_from_video(tmp_path)
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
            else:
                samples, reason = enroll.extract_from_image(raw)

            if samples:
                all_samples.extend(samples)
            elif reason:
                errors.append(f"{fname}: {reason}")
            else:
                errors.append(f"{fname}: no usable samples")
        except Exception as e:
            errors.append(f"{fname}: {e}")
    return all_samples, errors


@app.post("/api/persons", status_code=201)
async def create_person_endpoint(
    name: str = Form(...),
    notes: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    _require_face_engine()
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required")

    samples, errors = await _samples_from_uploads(files)
    if not samples:
        detail = "; ".join(errors) if errors else "no images processed"
        raise HTTPException(400, "No usable faces found — " + detail)

    person_id = uuid.uuid4().hex
    person_dir = PERSONS_DIR / person_id
    samples_dir = person_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    for i, (crop, _) in enumerate(samples):
        cv2.imwrite(str(samples_dir / f"{i:03d}.jpg"), crop)
    thumb = cv2.resize(samples[0][0], (128, 128))
    thumb_path = person_dir / "thumbnail.jpg"
    cv2.imwrite(str(thumb_path), thumb)

    db.create_person(person_id, name, notes, str(thumb_path))
    db.add_embeddings(person_id, [e for _, e in samples], source="upload")

    return {
        "id": person_id, "name": name, "notes": notes,
        "samples": len(samples), "errors": errors,
    }


@app.post("/api/persons/{person_id}/enroll")
async def enroll_more_endpoint(
    person_id: str,
    files: list[UploadFile] = File(default=[]),
):
    if db.get_person(person_id) is None:
        raise HTTPException(404, "Person not found")
    _require_face_engine()

    samples, errors = await _samples_from_uploads(files)

    person_dir = PERSONS_DIR / person_id
    samples_dir = person_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(samples_dir.glob("*.jpg")))

    for i, (crop, _) in enumerate(samples):
        cv2.imwrite(str(samples_dir / f"{existing + i:03d}.jpg"), crop)
    if samples:
        db.add_embeddings(person_id, [e for _, e in samples], source="upload")

    return {"added": len(samples), "errors": errors}


@app.put("/api/persons/{person_id}")
def update_person_endpoint(person_id: str, body: PersonUpdate):
    patch = body.model_dump(exclude_unset=True)
    p = db.update_person(person_id, patch)
    if p is None:
        raise HTTPException(404, "Person not found")
    return _enrich(p)


@app.delete("/api/persons/{person_id}")
def delete_person_endpoint(person_id: str):
    if db.get_person(person_id) is None:
        raise HTTPException(404, "Person not found")
    shutil.rmtree(PERSONS_DIR / person_id, ignore_errors=True)
    db.delete_person(person_id)
    return {"ok": True}


# ---------- Telegram settings ----------

def _telegram_payload() -> dict:
    payload = telegram_bot.service.status()
    payload["events"] = db.event_counts()
    return payload


@app.get("/api/telegram")
def get_telegram_settings() -> dict:
    return _telegram_payload()


@app.put("/api/telegram")
def save_telegram_settings(body: TelegramSettings) -> dict:
    patch = body.model_dump(exclude_unset=True)
    token = (patch.get("bot_token") or "").strip()
    chat_id = (patch.get("chat_id") or "").strip()
    if not token and not chat_id:
        raise HTTPException(400, "Enter a bot token and a group ID.")
    # Blank fields mean "keep the current value"; Disconnect clears both.
    telegram_bot.service.save(token or None, chat_id or None)
    return _telegram_payload()


@app.post("/api/telegram/test")
def test_telegram_settings(body: TelegramSettings | None = None) -> dict:
    patch = body.model_dump(exclude_unset=True) if body else {}
    return telegram_bot.service.test(
        patch.get("bot_token"), patch.get("chat_id"))


@app.delete("/api/telegram")
def disconnect_telegram() -> dict:
    telegram_bot.service.clear()
    return _telegram_payload()


# ---------- Events, customers and overview ----------

EVENT_STATUSES = ("pending", "confirmed", "rejected", "failed", "all")


def _event_payload(row: dict) -> dict:
    row = dict(row)
    row["customer"] = bool(row.get("customer"))
    row["snapshot_url"] = (
        f"/api/events/{row['id']}/snapshot"
        if row.get("snapshot_path") else None
    )
    return row


@app.get("/api/overview")
def overview() -> dict:
    """Counters and short lists shown on the Overview section."""
    data = db.dashboard_overview()
    data["cameras"] = {
        "total": len(db.list_cameras()),
        "running": len(pipeline.list_pipelines()),
    }
    for key in ("recent_events", "pending_events", "recent_customers"):
        data[key] = [_event_payload(row) for row in data.get(key, [])]
    return data


@app.get("/api/events/summary")
def events_summary() -> dict:
    return db.event_counts()


@app.get("/api/events")
def list_events(status: str = "confirmed", limit: int = 100,
                offset: int = 0, customer: bool | None = None) -> dict:
    if status not in EVENT_STATUSES:
        raise HTTPException(400, "Unknown event status")
    rows = db.list_events(
        None if status == "all" else status,
        limit=limit, offset=offset, customer=customer)
    return {"events": [_event_payload(row) for row in rows],
            "counts": db.event_counts(customer=customer)}


@app.get("/api/customers")
def customers(status: str = "all", limit: int = 100,
              offset: int = 0) -> dict:
    """Customer-tagged entrance/exit crossings, including unconfirmed ones."""
    if status not in EVENT_STATUSES:
        raise HTTPException(400, "Unknown event status")
    rows = db.list_events(
        None if status == "all" else status,
        limit=limit, offset=offset, customer=True)
    return {
        "summary": db.customer_summary(),
        "counts": db.event_counts(customer=True),
        "events": [_event_payload(row) for row in rows],
    }


@app.get("/api/events/{event_id}/snapshot")
def event_snapshot(event_id: str):
    event = db.get_event(event_id)
    if event is None or not event.get("snapshot_path"):
        raise HTTPException(404, "Snapshot not found")
    path = Path(event["snapshot_path"])
    if not path.is_file():
        raise HTTPException(404, "Snapshot file missing")
    return FileResponse(str(path), media_type="image/jpeg")


# ---------- Attendance ----------

@app.get("/api/attendance")
def attendance_overview():
    """Enrolled people plus confirmed entrance/exit records."""
    data = db.attendance_overview()
    for person in data["people"]:
        person["thumbnail_url"] = (
            f"/api/persons/{person['id']}/thumbnail"
            if person.pop("has_thumbnail") else None)
    for event in data["unknown_events"]:
        event["snapshot_url"] = (
            f"/api/events/{event['id']}/snapshot"
            if event.get("snapshot_path") else None)
    return data


# ---------- Review & Label (model training data, independent of alerts) ----------

@app.get("/api/training/capture")
def training_capture_status():
    return review.capture_policy()


@app.put("/api/training/capture")
def training_capture_update(body: ReviewCapture):
    if body.scope == "exclusive":
        if not body.camera_ids or len(body.camera_ids) > 100:
            raise HTTPException(400, "Select between 1 and 100 cameras")
        for camera_id in body.camera_ids:
            if db.get_camera(camera_id) is None:
                raise HTTPException(404, f"Camera not found: {camera_id}")
    try:
        return review.set_capture_policy(
            body.scope, body.enabled,
            body.max_per_hour or review.capture_policy()["max_per_hour"],
            body.camera_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.get("/api/training/samples")
def training_samples(camera_id: str | None = None,
                     label: str | None = None) -> dict:
    if label and label not in review.LABELS:
        raise HTTPException(400, "Unknown label")
    rows = db.list_review_samples(camera_id, label, limit=200)
    for row in rows:
        row.pop("image_path")
        row["image_url"] = f"/api/training/samples/{row['id']}/image"
    return {"samples": rows, "counts": db.review_counts()}


@app.get("/api/training/samples/{sample_id}/image")
def training_image(sample_id: str):
    sample = db.get_review_sample(sample_id)
    if sample is None:
        raise HTTPException(404, "Sample not found")
    path = Path(sample["image_path"])
    if not path.is_file():
        raise HTTPException(404, "Sample image missing")
    return FileResponse(str(path), media_type="image/jpeg")


@app.put("/api/training/samples/{sample_id}")
def training_label(sample_id: str, body: ReviewLabel):
    if body.label not in review.LABELS:
        raise HTTPException(400, "Unknown label")
    if not db.label_review_sample(sample_id, body.label):
        raise HTTPException(404, "Sample not found")
    return {"ok": True}


@app.put("/api/training/samples/{sample_id}/box")
def training_box(sample_id: str, body: ReviewBox):
    sample = db.get_review_sample(sample_id)
    if sample is None:
        raise HTTPException(404, "Sample not found")
    box = body.box
    if box is not None:
        path = Path(sample["image_path"])
        image = cv2.imread(str(path)) if path.is_file() else None
        if image is None:
            raise HTTPException(404, "Sample image missing")
        if (len(box) != 4 or box[0] < 0 or box[1] < 0
                or box[2] > image.shape[1] or box[3] > image.shape[0]
                or box[2] - box[0] < 32 or box[3] - box[1] < 32):
            raise HTTPException(400, "Draw one person box of at least 32 × 32 pixels inside the image")
    db.set_review_box(sample_id, box)
    return {"person_box": box}


@app.delete("/api/training/samples/{sample_id}")
def training_delete(sample_id: str):
    path = db.delete_review_sample(sample_id)
    if path is None:
        raise HTTPException(404, "Sample not found")
    Path(path).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/training/samples", status_code=201)
async def training_upload(camera_id: str = Form(...),
                          file: UploadFile = File(...)):
    if db.get_camera(camera_id) is None:
        raise HTTPException(404, "Camera not found")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".avi"}:
        raise HTTPException(400, "Upload a JPG/PNG or short MP4/MOV/AVI")
    raw = await file.read(30 * 1024 * 1024 + 1)
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(413, "Maximum upload size is 30 MB")
    frames = []
    if suffix in {".jpg", ".jpeg", ".png"}:
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise HTTPException(400, "Could not decode image")
        frames = [image]
    else:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            path = Path(tmp.name)
        try:
            capture = cv2.VideoCapture(str(path))
            try:
                fps = capture.get(cv2.CAP_PROP_FPS)
                count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
                if not capture.isOpened() or fps <= 0 or count <= 0 or count / fps > 20:
                    raise HTTPException(400, "Upload a decodable clip under 20 seconds")
                for second in range(min(8, int(count / fps) + 1)):
                    capture.set(cv2.CAP_PROP_POS_FRAMES, int(second * fps))
                    ok, image = capture.read()
                    if ok:
                        frames.append(image)
            finally:
                capture.release()
        finally:
            path.unlink(missing_ok=True)
    if not frames:
        raise HTTPException(400, "No usable frames in upload")
    if sum(db.review_counts().values()) + len(frames) > review.REVIEW_MAX_SAMPLES:
        raise HTTPException(400, "Review storage limit reached; delete old images first")
    try:
        ids = [review.save_image(camera_id, frame, "upload") for frame in frames]
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ids": ids, "count": len(ids)}


@app.get("/api/training/status")
def training_status():
    return training.status()


@app.post("/api/training/start")
def training_start():
    # Training competes for CPU/memory; never degrade live feeds silently.
    if pipeline.list_pipelines():
        raise HTTPException(409, "Stop live camera pipelines before training")
    try:
        return training.start()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/training/deploy/{model_id}")
def training_deploy(model_id: str):
    if pipeline.list_pipelines():
        raise HTTPException(409, "Stop live cameras before switching models")
    try:
        return training.deploy(model_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/training/rollback")
def training_rollback():
    if pipeline.list_pipelines():
        raise HTTPException(409, "Stop live cameras before switching models")
    return training.rollback()


# ---------- Static frontend (must stay last) ----------
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
