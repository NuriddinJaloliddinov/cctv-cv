"""Pydantic schemas."""
from pydantic import BaseModel


class CameraInfo(BaseModel):
    id: str
    name: str
    address: str
    rtsp_url: str | None = None
    source_type: str = "onvif"
    onvif_user: str = ""
    onvif_pass: str = ""
    device: str = "auto"
    fps: int = 10
    created_at: str | None = None
    updated_at: str | None = None


class CameraCreate(BaseModel):
    name: str
    address: str
    rtsp_url: str | None = None
    source_type: str = "onvif"
    onvif_user: str = ""
    onvif_pass: str = ""
    device: str = "auto"
    fps: int = 10


class CameraUpdate(BaseModel):
    name: str | None = None
    address: str | None = None
    rtsp_url: str | None = None
    source_type: str | None = None
    onvif_user: str | None = None
    onvif_pass: str | None = None
    device: str | None = None
    fps: int | None = None


class Capability(BaseModel):
    id: str
    name: str
    status: str
    detail: str = ""


class StartRequest(BaseModel):
    camera_id: str
    device: str | None = None
    fps: int | None = None


class StatusResponse(BaseModel):
    running: bool
    camera_id: str | None = None
    device: str | None = None
    fps: int | None = None