"""Pydantic schemas."""
from pydantic import BaseModel, Field

from .config import PHONE_CONF_THRESHOLD, PHONE_LOOK_PITCH_RATIO


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
    phone_conf: float = PHONE_CONF_THRESHOLD
    phone_pitch: float = PHONE_LOOK_PITCH_RATIO
    role: str | None = None
    tripwire: list[float] | None = None
    tripwire_direction: str | None = None
    count_customer: bool = False
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
    phone_conf: float = PHONE_CONF_THRESHOLD
    phone_pitch: float = PHONE_LOOK_PITCH_RATIO
    role: str | None = None
    tripwire: list[float] | None = None
    tripwire_direction: str | None = None
    count_customer: bool = False


class CameraUpdate(BaseModel):
    name: str | None = None
    address: str | None = None
    rtsp_url: str | None = None
    source_type: str | None = None
    onvif_user: str | None = None
    onvif_pass: str | None = None
    device: str | None = None
    fps: int | None = None
    phone_conf: float | None = None
    phone_pitch: float | None = None
    role: str | None = None
    tripwire: list[float] | None = None
    tripwire_direction: str | None = None
    count_customer: bool | None = None


class DiscoveryCredentials(BaseModel):
    username: str = ""
    password: str = ""


class DiscoveryRequest(BaseModel):
    """Optional inputs for a discovery run.

    Discovery never submits passwords, even when legacy callers supply them.
    ``subnets``/``targets`` may extend the automatically detected scan scope.
    """

    subnets: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    username: str = ""
    password: str = ""
    credentials: list[DiscoveryCredentials] = Field(default_factory=list)
    include_saved: bool = False
    deep: bool = False


class DiscoveryConnectRequest(BaseModel):
    address: str
    username: str = ""
    password: str = ""


class DiscoveredChannel(BaseModel):
    token: str
    name: str
    encoding: str = ""
    width: int | None = None
    height: int | None = None
    rtsp_url: str | None = None
    accessible: bool = False
    error: str | None = None


class DiscoveredDevice(BaseModel):
    id: str
    name: str
    address: str
    rtsp_url: str | None = None
    source_type: str = "onvif"
    kind: str = "onvif"             # camera | nvr | onvif | rtsp | encoder
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    host: str = ""
    port: int = 80
    verified: bool = False
    status: str = "ready"           # ready | credentials | no_video | error
    detail: str = ""
    channels: list[DiscoveredChannel] = Field(default_factory=list)
    saved: bool = False


class DiscoveryReport(BaseModel):
    devices: list[DiscoveredDevice] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    scanned_hosts: int = 0
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)


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


class TelegramSettings(BaseModel):
    bot_token: str | None = None
    chat_id: str | None = None
