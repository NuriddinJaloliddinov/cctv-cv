"""Conservative, observation-based server capacity checks.

Never infer that untested camera configurations will work from CPU model names
or a tiny synthetic inference. All recommendations are estimates, not promises.
"""
import asyncio
import math
import os
import subprocess
from pathlib import Path

import psutil
from pydantic import BaseModel, Field

from . import pipeline
from .capabilities import probe_devices
from .config import DATA_DIR, DETECT_INTERVAL


class CapacityPlan(BaseModel):
    cameras: int = Field(ge=1, le=256)
    fps: int = Field(ge=1, le=60)
    width: int = Field(ge=320, le=7680)
    height: int = Field(ge=240, le=4320)
    people_per_frame: int = Field(ge=0, le=100)
    phone_detection: bool = True
    face_recognition: bool = True
    viewers: int = Field(default=1, ge=0, le=100)
    camera_mbps: float = Field(default=4, gt=0, le=100)
    snapshot_mb_per_day: float = Field(default=100, ge=0, le=100000)
    retention_days: int = Field(default=30, ge=0, le=3650)


def _gpu_info():
    """Optional NVIDIA telemetry; never mistake absence for zero usage."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True,
            timeout=3, check=True)
        rows = []
        for line in result.stdout.splitlines():
            name, total, used, utilization = (part.strip() for part in line.split(",", 3))
            rows.append({"name": name, "vram_mb": int(total),
                         "used_mb": int(used), "utilization_percent": int(utilization)})
        return rows
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def hardware():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path(DATA_DIR).resolve()))
    return {
        "logical_cores": os.cpu_count() or 1,
        "physical_cores": psutil.cpu_count(logical=False),
        "ram_gb": round(mem.total / 2**30, 1),
        "available_ram_gb": round(mem.available / 2**30, 1),
        "disk_free_gb": round(disk.free / 2**30, 1),
        "providers": [{"name": c.name, "status": c.status} for c in probe_devices()],
        "gpus": _gpu_info(),
    }


def assess(plan: CapacityPlan, first: list[dict], last: list[dict],
           cpu_percent: float, elapsed: float, hw: dict, process_ram_gb: float,
           viewer_count: int = 0) -> dict:
    by_id = {p["camera_id"]: p for p in first}
    active = [p for p in last if p.get("camera_id") in by_id]
    issues, limits = [], []
    caveats = ["CPU utilization includes other programs; CPU sizing is a rough projection.",
               "Network throughput, disk I/O and long-term peak scene density were not measured in this short sample."]
    needed_disk = plan.snapshot_mb_per_day * plan.retention_days / 1024
    inbound_mbps = plan.cameras * plan.camera_mbps
    # Viewer traffic depends on encoder settings. This is a planning upper
    # bound, not a measured WebRTC bitrate.
    outbound_mbps = plan.viewers * plan.camera_mbps
    if needed_disk > hw["disk_free_gb"] * .7:
        issues.append("Snapshot retention exceeds 70% of currently free disk space.")
    if hw["available_ram_gb"] < max(1.0, hw["ram_gb"] * .15):
        issues.append("Less than 15% of system RAM is currently available.")
    if cpu_percent >= 85:
        issues.append("CPU was at or above 85% during the sample.")

    equivalent = bool(active) and len(active) == plan.cameras and all(
        p.get("connected") and p.get("person_detection_enabled")
        and p.get("source_width") == plan.width
        and p.get("source_height") == plan.height
        and p.get("configured_fps") == plan.fps
        and bool(p.get("phone_use_detection_enabled")) == plan.phone_detection
        for p in active)
    if viewer_count < plan.viewers:
        limits.append("The requested number of simultaneous viewers was not observed.")
    if not equivalent:
        limits.append("Run the proposed number of cameras at the proposed resolution, FPS and detection settings for a comparable measurement.")
    if not active:
        limits.append("No running cameras were measured. A static hardware inventory cannot verify inference throughput.")
    if plan.face_recognition and not any(
            p.get("face_pass_count", 0) > by_id[p["camera_id"]].get("face_pass_count", 0)
            for p in active):
        limits.append("No face-analysis passes were observed during the sample.")
    if plan.people_per_frame and (not active or
            max(p.get("person_count", 0) for p in active) < plan.people_per_frame):
        limits.append("The observed people per frame did not reach the proposed busy-scene count.")
    ready_gpu = any(p["status"] == "ready" and p["name"] != "CPU"
                    for p in hw["providers"])
    gpu_used = any(p.get("device") not in ("cpu", "auto") or
                   (p.get("device") == "auto" and ready_gpu) for p in active)
    if gpu_used:
        limits.append("GPU inference utilization and shared-session contention require sustained monitoring; a single GPU reading cannot verify headroom.")
        for gpu in hw["gpus"]:
            if gpu["vram_mb"] and gpu["used_mb"] / gpu["vram_mb"] >= .9:
                issues.append(f"{gpu['name']}: at least 90% of GPU memory is occupied.")
    observed = []
    for p in active:
        before = by_id[p["camera_id"]]
        rate = max(0, p.get("inference_count", 0) - before.get("inference_count", 0)) / max(elapsed, 1)
        target = min(plan.fps, 1 / DETECT_INTERVAL) if DETECT_INTERVAL > 0 else plan.fps
        if rate < target * .8:
            issues.append(f"{p['name']}: inference {rate:.1f}/s is below 80% of the {target:.1f}/s target.")
        if p.get("source_fps", 0) < plan.fps * .8:
            issues.append(f"{p['name']}: camera delivery rate is below 80% of requested FPS (may be a camera/network limit).")
        if p.get("capture_age_ms") is not None and p["capture_age_ms"] > 2000:
            issues.append(f"{p['name']}: video is over 2 seconds old.")
        observed.append({"camera": p["name"], "inference_fps": round(rate, 1),
                         "source_fps": p.get("source_fps"),
                         "inference_p95_ms": p.get("inference_p95_ms"),
                         "connected": p.get("connected")})

    # The processor recommendation is only a measured-load extrapolation;
    # decoding, multi-camera GPU scheduling and memory do not scale linearly.
    scale = plan.cameras / len(active) if active else None
    if scale is not None and cpu_percent * scale > 85:
        issues.append("Linear CPU projection exceeds 85%; test the full camera count before purchasing hardware.")
    suggested_cores = (math.ceil(hw["logical_cores"] * cpu_percent / 65 * scale)
                       if scale is not None and cpu_percent > 0 else None)
    guidance = {
        "cpu_logical_cores_estimate": max(1, suggested_cores) if suggested_cores is not None else None,
        "ram_gb_floor": math.ceil((hw["ram_gb"] - hw["available_ram_gb"]) * 1.3) if equivalent else None,
        "free_disk_gb_for_snapshots": math.ceil(needed_disk / .7),
        "inbound_mbps": round(inbound_mbps, 1),
        "viewer_outbound_mbps_upper_bound": round(outbound_mbps, 1),
        "gpu_vram_gb_floor": (math.ceil(max(gpu["used_mb"] for gpu in hw["gpus"]) * 1.3 / 1024)
                              if gpu_used and hw["gpus"] else None),
        "gpu": "Use the same verified inference provider; GPU model and VRAM cannot be sized reliably without a sustained representative workload." if gpu_used else "No GPU requirement can be determined without a representative test.",
    }
    return {"verdict": "insufficient" if issues else "unverified" if limits else "sufficient",
            "issues": issues, "limitations": limits, "caveats": caveats,
            "hardware": hw, "sample_seconds": round(elapsed, 1),
            "cpu_percent": round(cpu_percent, 1), "process_ram_gb": round(process_ram_gb, 2),
            "observed": observed, "recommendation": guidance}


async def measure(plan: CapacityPlan, seconds: int = 15, viewer_count: int = 0):
    first = pipeline.list_pipelines()
    process = psutil.Process()
    psutil.cpu_percent(interval=None)
    start = asyncio.get_running_loop().time()
    await asyncio.sleep(seconds)  # no new pipelines or synthetic load
    elapsed = asyncio.get_running_loop().time() - start
    cpu = psutil.cpu_percent(interval=None)
    last = pipeline.list_pipelines()
    return assess(plan, first, last, cpu, elapsed, hardware(),
                  process.memory_info().rss / 2**30, viewer_count)
