"""FastAPI app: REST + WebRTC signaling + static frontend."""
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from aiortc import RTCPeerConnection, RTCSessionDescription
from pydantic import BaseModel

from . import db, discovery, capabilities, pipeline, enroll
from .discovery import fetch_rtsp_url
from .models import (
    CameraInfo, CameraCreate, CameraUpdate,
    Capability, StartRequest, StatusResponse,
)
from .webrtc_stream import FrameTrack


# ---------- Constants for enrollment ----------
PERSONS_DIR = Path("data/persons")
PERSONS_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}


class PersonUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None


# ---------- App lifecycle ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="CCTV CV", lifespan=lifespan)

_peers: dict[str, RTCPeerConnection] = {}


# ---------- Discovery / capabilities ----------

@app.get("/api/discover")
def discover() -> list[CameraInfo]:
    return discovery.discover_devices()


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
    if db.find_by_address(body.address):
        raise HTTPException(409, "A camera with this address already exists")
    return db.create_camera(body.model_dump())


@app.put("/api/cameras/{camera_id}", response_model=CameraInfo)
def update_camera(camera_id: str, body: CameraUpdate):
    patch = body.model_dump(exclude_unset=True)
    required = {"name", "address", "source_type", "device", "fps"}
    for field in required & patch.keys():
        if not patch[field]:
            raise HTTPException(400, f"'{field}' cannot be empty")
    if "address" in patch:
        existing = db.find_by_address(patch["address"])
        if existing and existing.id != camera_id:
            raise HTTPException(409, "A camera with this address already exists")
    cam = db.update_camera(camera_id, patch)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    return cam


@app.delete("/api/cameras/{camera_id}")
async def delete_camera(camera_id: str):
    if not db.delete_camera(camera_id):
        raise HTTPException(404, "Camera not found")
    pipeline.stop_pipeline(camera_id)
    pc = _peers.pop(camera_id, None)
    if pc is not None:
        try:
            await pc.close()
        except Exception:
            pass
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


# ---------- Pipeline ----------

@app.post("/api/start", response_model=StatusResponse)
def start(req: StartRequest) -> StatusResponse:
    cam = db.get_camera(req.camera_id)
    if cam is None:
        raise HTTPException(404, "Camera not found")
    if not cam.rtsp_url:
        raise HTTPException(400, "No RTSP URL — resolve it first")

    device = req.device or cam.device or "auto"
    fps = req.fps or cam.fps or 10

    pipeline.start_pipeline(cam.id, cam.name, cam.rtsp_url, device, fps)
    if device != cam.device or fps != cam.fps:
        db.update_camera(cam.id, {"device": device, "fps": fps})

    return StatusResponse(running=True, camera_id=cam.id, device=device, fps=fps)


@app.post("/api/stop/{camera_id}")
async def stop(camera_id: str) -> StatusResponse:
    pipeline.stop_pipeline(camera_id)
    pc = _peers.pop(camera_id, None)
    if pc is not None:
        await pc.close()
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

    old = _peers.pop(camera_id, None)
    if old is not None:
        await old.close()

    pc = RTCPeerConnection()
    _peers[camera_id] = pc
    pc.addTrack(FrameTrack(p))

    @pc.on("connectionstatechange")
    async def _on_state():
        if pc.connectionState in ("failed", "closed"):
            await pc.close()

    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


# ---------- Persons (face enrollment) ----------

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
    if not enroll.is_available():
        raise HTTPException(503, "Face engine not available (see setup.ps1).")
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
    if not enroll.is_available():
        raise HTTPException(503, "Face engine not available.")

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


# ---------- Static frontend (must stay last) ----------
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")