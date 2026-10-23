"""Hardware / inference-backend probe.

Enumerates available ONNX Runtime execution providers and validates
each with a tiny warm-up inference, so the dropdown only ever shows
backends that actually work on this machine.
"""
import os
import tempfile
from pathlib import Path

import numpy as np
import onnxruntime as ort

from .models import Capability


_PROVIDER_NAMES = {
    "CUDAExecutionProvider": "NVIDIA GPU (CUDA)",
    "TensorrtExecutionProvider": "NVIDIA GPU (TensorRT)",
    "ROCMExecutionProvider": "AMD GPU (ROCm)",
    "OpenVINOExecutionProvider": "Intel GPU/NPU (OpenVINO)",
    "CoreMLExecutionProvider": "Apple Neural Engine (CoreML)",
    "DmlExecutionProvider": "DirectML",
    "CPUExecutionProvider": "CPU",
}

_ID_TO_PROVIDER = {
    "cuda": "CUDAExecutionProvider",
    "tensorrt": "TensorrtExecutionProvider",
    "rocm": "ROCMExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
    "coreml": "CoreMLExecutionProvider",
    "dml": "DmlExecutionProvider",
    "cpu": "CPUExecutionProvider",
}

_cache: list[Capability] | None = None


def _make_tiny_model(path: Path) -> None:
    """Build a minimal ONNX model (Identity op) for warm-up validation."""
    import onnx
    from onnx import TensorProto, helper

    x = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3])
    y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 3])
    node = helper.make_node("Identity", ["x"], ["y"])
    graph = helper.make_graph([node], "warmup", [x], [y])
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 13)]
    )
    onnx.save(model, str(path))


def _warm_up(provider: str, model_path: Path) -> tuple[bool, str]:
    """Run one tiny inference on `provider`. Returns (ok, detail)."""
    try:
        sess = ort.InferenceSession(
            str(model_path),
            providers=[provider, "CPUExecutionProvider"],
        )
        # ONNX Runtime silently falls back to CPU when the requested
        # provider is present but not actually usable. Catch that here.
        active = sess.get_providers()
        if provider not in active:
            fallback = active[0] if active else "CPU"
            return False, f"fallback to {fallback}"
        sess.run(None, {"x": np.zeros((1, 3), dtype=np.float32)})
        return True, "warm-up OK"
    except Exception as e:
        first_line = str(e).splitlines()[0] if str(e) else repr(e)
        return False, first_line[:140]


def _do_probe() -> list[Capability]:
    caps: list[Capability] = []
    available = set(ort.get_available_providers())

    with tempfile.TemporaryDirectory() as tmp:
        model_path = Path(tmp) / "warmup.onnx"
        _make_tiny_model(model_path)

        for provider, label in _PROVIDER_NAMES.items():
            if provider not in available:
                continue

            if provider == "CPUExecutionProvider":
                caps.append(Capability(
                    id="cpu",
                    name="CPU",
                    status="ready",
                    detail=f"{os.cpu_count() or 1} cores",
                ))
                continue

            ok, detail = _warm_up(provider, model_path)
            caps.append(Capability(
                id=provider.replace("ExecutionProvider", "").lower(),
                name=label,
                status="ready" if ok else "unavailable",
                detail=detail,
            ))

    return caps


def probe_devices() -> list[Capability]:
    """Enumerate inference backends and validate each with a warm-up.

    Result is cached — repeated calls are instant.
    """
    global _cache
    if _cache is None:
        _cache = _do_probe()
    return _cache


def resolve_device(requested: str) -> str:
    """Map a user choice ('auto' | 'cpu' | provider id) to an ORT provider.

    Raises ValueError if the chosen device is not actually available.
    """
    caps = probe_devices()

    if requested == "auto":
        # Priority order: GPU-ish providers first, CPU as the fallback.
        for cap in caps:
            if cap.status == "ready" and cap.id != "cpu":
                return _ID_TO_PROVIDER[cap.id]
        return "CPUExecutionProvider"

    for cap in caps:
        if cap.id == requested and cap.status == "ready":
            return _ID_TO_PROVIDER[cap.id]

    raise ValueError(f"Device '{requested}' is not available on this machine")