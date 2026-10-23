# CCTV CV

Real-time person detection for ONVIF cameras.

## Setup
    pip install -r requirements.txt
    cp .env.example .env
    python run.py
    ### GPU support
    - NVIDIA: `pip install onnxruntime-gpu` (instead of `onnxruntime`)
    - Intel iGPU/NPU: the OpenVINO provider is used automatically if present
    - AMD: install `onnxruntime-rocm` or use the DirectML provider

## Open
    http://localhost:8000

## Face recognition

1. Run `./setup.ps1` on Windows — it downloads YuNet + SFace into `data/`.
   On Linux/macOS download manually from
   `github.com/opencv/opencv_zoo` and place in `data/`.

2. On the dashboard, click **+ Enroll Person**, enter a name, upload
   images (or an optional short video showing the person clearly), and save.

3. Click **Run** on a camera — known people get a green box with their
   name; others get a red **Unknown** box.

### Thresholds
- `FACE_MATCH_THRESHOLD` (default `0.40`) — lower = more permissive
- `FACE_MATCH_HIGH` (default `0.55`) — informational only for now
- Set via `.env` to override.

### Notes
- Enrollment rejects images where the face is < 64 px, blurry, or where
  more than one face is present (avoids mixing people).
- Face embeddings and cropped sample images are stored **plaintext** in
  `data/persons/<id>/` and in `data/cctv.db`. Fine on a trusted LAN.
- Deleting a person removes their files and embeddings immediately.