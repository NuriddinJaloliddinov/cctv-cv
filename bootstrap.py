"""Cross-platform, fail-closed setup and readiness check for CCTV CV.

Run with Python 3.10-3.12. Only the project's .venv and missing model files
are written; existing model files and the camera/person database are left
untouched. The --check mode is offline and never installs or downloads.
"""
import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DEFAULT_YOLO = "data/yolov8n.onnx"

# Published OpenCV Zoo weights. Hashes prevent Git LFS pointers, partial
# downloads, or an upstream change from being silently accepted as models.
FACE_MODELS = {
    "data/face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "data/face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/"
        "face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


class SetupError(Exception):
    """A setup blocker with an actionable error message."""


def print_step(message):
    print(f"[setup] {message}", flush=True)


def run(command, *, cwd=ROOT):
    print_step("Running: " + " ".join(str(x) for x in command))
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError(f"Command failed: {command[0]} (see output above): {exc}") from exc


def python_version_check():
    version = sys.version_info
    if not ((3, 10) <= version[:2] <= (3, 12)):
        raise SetupError("Python 3.10-3.12 (64-bit) is required; 3.11 is recommended. "
                         f"You are running {platform.python_version()}.")
    if sys.maxsize <= 2**32:
        raise SetupError("A 64-bit Python installation is required for the CV models.")


def ensure_venv(*, create):
    if VENV.exists() and not (VENV / "pyvenv.cfg").is_file():
        raise SetupError(f"{VENV} exists but is not a virtual environment. "
                         "Move it out of the way yourself and retry.")
    if not VENV.exists():
        if not create:
            raise SetupError(".venv is missing. Run python bootstrap.py first.")
        print_step(f"Creating isolated environment at {VENV}")
        venv.create(VENV, with_pip=True)
    if not VENV_PYTHON.is_file():
        raise SetupError(f"Missing {VENV_PYTHON}. A copied virtual environment "
                         "is not portable; recreate it on this computer.")


def venv_eval(code, *args, capture=False):
    command = [str(VENV_PYTHON), "-c", code, *map(str, args)]
    if capture:
        try:
            return subprocess.check_output(command, cwd=ROOT, text=True).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SetupError(f"Virtual environment could not run: {exc}") from exc
    run(command)


def configured_models():
    code = ("import json; from backend import config; "
            "print(json.dumps([config.MODEL_NAME, "
            "config.FACE_DETECT_MODEL, config.FACE_EMBED_MODEL]))")
    try:
        values = json.loads(venv_eval(code, capture=True))
    except (ValueError, TypeError) as exc:
        raise SetupError("Could not read model paths from .env") from exc
    return tuple(Path(x) if Path(x).is_absolute() else ROOT / x for x in values)


def install_requirements():
    """Preserve an already configured GPU ORT install on repeat setup runs."""
    has_gpu = venv_eval(
        "from importlib import metadata; "
        "print('yes' if any(d.metadata['Name'].lower() == 'onnxruntime-gpu' "
        "for d in metadata.distributions()) else 'no')", capture=True) == "yes"
    requirements = ROOT / "requirements.txt"
    if not has_gpu:
        run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(requirements)])
        return

    print_step("Existing onnxruntime-gpu detected; keeping it (CPU provider "
               "will still be checked).")
    # Never install CPU and GPU ORT packages over each other's files. Use a
    # temporary copy of the requirements without just the CPU-only line.
    lines = requirements.read_text(encoding="utf-8").splitlines(keepends=True)
    filtered = [line for line in lines if not line.lstrip().startswith("onnxruntime>=")]
    with tempfile.TemporaryDirectory(prefix="cctv-req-") as directory:
        path = Path(directory) / "requirements.txt"
        path.write_text("".join(filtered), encoding="utf-8")
        run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(path)])


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def promote_new_file(temporary, path):
    """Publish a validated file without ever replacing an existing model."""
    try:
        os.link(temporary, path)  # atomic, exclusive, same filesystem
    except FileExistsError as exc:
        raise SetupError(f"{path} appeared during setup; it was not replaced") from exc
    except OSError as exc:
        raise SetupError(f"Cannot install {path}: {exc}. Place the validated "
                         "model there manually and rerun --check.") from exc


def ensure_download(path, url, expected_hash):
    """Fetch once, check exact hash, then atomically promote to target path."""
    if path.exists():
        if sha256_file(path) != expected_hash:
            raise SetupError(f"Existing {path} differs from the official model. "
                             "It was not overwritten. Back it up or rename it "
                             "yourself, then rerun setup.")
        print_step(f"Verified existing {path.name}")
        return

    print_step(f"Downloading {path.name} ...")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".model-",
                                         suffix=".download", delete=False) as out:
            temporary = Path(out.name)
            req = urllib.request.Request(url, headers={"User-Agent": "CCTV-CV-setup"})
            with urllib.request.urlopen(req, timeout=90) as response:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
        if sha256_file(temporary) != expected_hash:
            raise SetupError(f"Hash mismatch for {path.name}. The download may be "
                             "incomplete, an LFS pointer, or have changed upstream. "
                             "Please retry or obtain the official file manually.")
        promote_new_file(temporary, path)
        print_step(f"Verified {path.name} ({path.stat().st_size:,} bytes)")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_yolo(path):
    """Called in the project venv. Check both the graph and real inference."""
    import numpy as np
    import onnxruntime as ort

    if not path.is_file():
        raise SetupError(f"Person detector model missing: {path}")
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    if len(inputs) != 1 or inputs[0].shape != [1, 3, 640, 640]:
        raise SetupError(f"{path} is not a supported YOLOv8 640px detector")
    output = session.run(None, {inputs[0].name: np.zeros((1, 3, 640, 640),
                                                     dtype=np.float32)})[0]
    if output.ndim != 3 or output.shape[0] != 1 or output.shape[1] != 84:
        raise SetupError(f"{path} produced an incompatible YOLOv8 output shape: "
                         f"{output.shape}")


def verify_yolo_in_venv(path):
    venv_eval("from pathlib import Path; import sys; "
              "from bootstrap import worker, verify_yolo; "
              "worker(verify_yolo, Path(sys.argv[1]))",
              path)


def ensure_yolo(path):
    if path.is_file():
        print_step(f"Validating existing {path.name} using ONNX Runtime ...")
        verify_yolo_in_venv(path)
        return
    if path != ROOT / DEFAULT_YOLO:
        raise SetupError(f"Custom MODEL_NAME points to missing {path}; provide "
                         "a compatible YOLOv8 ONNX model at that location.")

    print_step("Exporting missing yolov8n.onnx (requires downloading YOLOv8 weights) ...")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Export tooling is intentionally separate from the server venv: the
    # Ultralytics package pulls in a GUI OpenCV wheel and PyTorch. Neither is
    # necessary to run the exported ONNX model on a headless server.
    with tempfile.TemporaryDirectory(prefix="cctv-yolo-") as directory:
        export_venv = Path(directory) / "export-env"
        venv.create(export_venv, with_pip=True)
        exporter = export_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([str(exporter), "-m", "pip", "install", "ultralytics>=8,<9",
             "onnxslim>=0.1.82", "onnxruntime>=1.17,<2"])
        # When Ultralytics installs opencv-python, its GUI build can require
        # libGL even though this export is running on a headless server.
        run([str(exporter), "-m", "pip", "install", "--force-reinstall",
             "--no-deps", "opencv-python-headless>=4.9,<5"])
        code = ("from ultralytics import YOLO; "
                "YOLO('yolov8n.pt').export(format='onnx', imgsz=640, "
                "opset=12, simplify=True)")
        run([str(exporter), "-c", code], cwd=directory)
        exported = Path(directory) / "yolov8n.onnx"
        if not exported.is_file():
            raise SetupError("YOLOv8 export finished without creating yolov8n.onnx")
        verify_yolo_in_venv(exported)
        # Same-filesystem temporary file makes promotion atomic and leaves
        # any existing user model untouched if another process created one.
        staging = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".yolo-",
                                             suffix=".onnx", delete=False) as out:
                staging = Path(out.name)
            shutil.copyfile(exported, staging)
            promote_new_file(staging, path)
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        print_step(f"Verified and installed {path.name}")


def runtime_check():
    """Run in the project venv; fail if any local runtime prerequisite fails."""
    import asyncio
    import tempfile as _tempfile

    import av
    import cv2
    import numpy as np
    import onnx
    import onnxruntime as ort
    import pillow_heif  # HEIC enrollment
    import psutil  # needed by discovery
    from aiortc import RTCPeerConnection
    from onvif import ONVIFCamera
    from wsdiscovery.discovery import ThreadedWSDiscovery
    from backend import config, db, enroll
    from backend.main import app

    python_version_check()
    if "CPUExecutionProvider" not in ort.get_available_providers():
        raise SetupError("ONNX Runtime CPU provider is not available")

    model = lambda p: Path(p) if Path(p).is_absolute() else ROOT / p
    verify_yolo(model(config.MODEL_NAME))
    for key, (_, expected) in FACE_MODELS.items():
        path = ROOT / key
        if path in (model(config.FACE_DETECT_MODEL), model(config.FACE_EMBED_MODEL)):
            if not path.is_file() or sha256_file(path) != expected:
                raise SetupError(f"{path} is missing or differs from the "
                                 "official OpenCV model. Rerun setup.")
    face = enroll.FaceEngine(str(model(config.FACE_DETECT_MODEL)),
                             str(model(config.FACE_EMBED_MODEL)))
    blank = np.zeros((320, 320, 3), dtype=np.uint8)
    if face.detect(blank):
        raise SetupError("Face detector unexpectedly found a face in a blank image")
    if face.recognizer.feature(np.zeros((112, 112, 3), dtype=np.uint8)).size != 128:
        raise SetupError("Face embedding inference failed")
    if not re.search(r"FFMPEG:\s+YES", cv2.getBuildInformation()):
        raise SetupError("OpenCV was built without FFmpeg; RTSP streams will not work")
    if not cv2.imencode(".jpg", blank)[0]:
        raise SetupError("OpenCV JPEG encoding failed")
    av.VideoFrame.from_ndarray(blank, format="bgr24")
    av.CodecContext.create("libvpx", "w")  # WebRTC video encoding
    av.CodecContext.create("h264", "r")  # Common RTSP camera codec

    async def check_peer():
        peer = RTCPeerConnection()
        peer.addTransceiver("video", direction="recvonly")
        await peer.createOffer()
        await peer.close()

    asyncio.run(check_peer())
    # Check schema creation on an isolated database, not the user's cameras.
    original = db.DB_PATH
    try:
        with _tempfile.TemporaryDirectory(prefix="cctv-db-") as directory:
            db.DB_PATH = str(Path(directory) / "check.db")
            db.init_db()
            db.list_cameras()
    finally:
        db.DB_PATH = original
    if not (ROOT / "frontend/index.html").is_file() or not app:
        raise SetupError("Frontend or FastAPI app is missing")
    smoke_server(model(config.MODEL_NAME), model(config.FACE_DETECT_MODEL),
                 model(config.FACE_EMBED_MODEL))
    print_step("PASS: detection inference, face models, video codecs, WebRTC, "
               "database schema, and live HTTP routes")

    try:
        family = socket.AF_INET6 if ":" in config.HOST else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((config.HOST, config.PORT))
    except OSError as exc:
        print_step(f"NOTE: cannot bind {config.HOST}:{config.PORT} ({exc}). "
                   "Stop any server already using it or fix HOST/PORT in .env.")


def check_runtime_in_venv():
    venv_eval("from bootstrap import runtime_check, worker; worker(runtime_check)")


def worker(func, *args):
    """Keep failed subprocess checks concise instead of printing a traceback."""
    try:
        func(*args)
    except Exception as exc:
        print(f"[setup] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def smoke_server(yolo, face_detect, face_embed):
    """Boot the actual ASGI app against an isolated throwaway database."""
    with tempfile.TemporaryDirectory(prefix="cctv-smoke-") as directory:
        work = Path(directory)
        shutil.copytree(ROOT / "frontend", work / "frontend")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        env = os.environ.copy()
        env.update({"PYTHONPATH": str(ROOT), "MODEL_NAME": str(yolo),
                    "FACE_DETECT_MODEL": str(face_detect),
                    "FACE_EMBED_MODEL": str(face_embed)})
        log_path = work / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.main:app",
                 "--host", "127.0.0.1", "--port", str(port)],
                cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise SetupError("Web server exited during startup")
                    try:
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        for route in ("/", "/api/status", "/api/cameras", "/api/face-engine"):
                            conn.request("GET", route)
                            response = conn.getresponse()
                            content = response.read()
                            if response.status != 200:
                                raise SetupError(f"HTTP {route}: {response.status}")
                            if route == "/api/cameras" and json.loads(content) != []:
                                raise SetupError("HTTP smoke check used a nonempty camera database")
                            if route == "/api/face-engine" and not json.loads(content)["available"]:
                                raise SetupError("HTTP face engine reports unavailable")
                        conn.close()
                        break
                    except (OSError, http.client.HTTPException):
                        time.sleep(.25)
                else:
                    raise SetupError("Web server did not respond within 30 seconds")
            except SetupError as exc:
                log.flush()
                raise SetupError(f"{exc}. Server log:\n{log_path.read_text(encoding='utf-8')[-4000:]}") from exc
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def main():
    parser = argparse.ArgumentParser(description="Set up and validate CCTV CV")
    parser.add_argument("--check", action="store_true",
                        help="offline readiness check; install/download nothing")
    args = parser.parse_args()
    if not args.check:
        python_version_check()
    os.chdir(ROOT)
    ensure_venv(create=not args.check)
    venv_eval("from bootstrap import python_version_check, worker; "
              "worker(python_version_check)")

    if not args.check:
        print_step("Installing project dependencies into .venv from requirements.txt ...")
        install_requirements()

        config_file = ROOT / ".env"
        if not config_file.exists():
            shutil.copyfile(ROOT / ".env.example", config_file)
            print_step("Created .env from .env.example")

        yolo, face_detect, face_embed = configured_models()
        for configured in (face_detect, face_embed):
            key = str(configured.relative_to(ROOT)) if configured.is_relative_to(ROOT) else ""
            key = key.replace("\\", "/")
            if key in FACE_MODELS:
                url, checksum = FACE_MODELS[key]
                ensure_download(configured, url, checksum)
            elif not configured.is_file():
                raise SetupError(f"Custom face model path is missing: {configured}")
        ensure_yolo(yolo)

    run([str(VENV_PYTHON), "-m", "pip", "check"])
    check_runtime_in_venv()
    print_step("Local runtime is ready. Resolve any port warning, then start "
               "run.py with .venv's Python.")
    print_step("Camera credentials, RTSP connectivity, firewall and real-time "
               "speed must be checked on the actual server/network.")


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, ValueError) as exc:
        print(f"[setup] FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
