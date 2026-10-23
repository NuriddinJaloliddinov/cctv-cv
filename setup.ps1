Write-Host "--- Starting Environment Setup ---" -ForegroundColor Cyan

# 1. Detect Hardware
Write-Host "Detecting hardware..."
$hasNvidia = $false
try { 
    nvidia-smi | Out-Null
    $hasNvidia = $true 
    Write-Host "NVIDIA GPU detected!" -ForegroundColor Green
} catch {
    Write-Host "No NVIDIA GPU detected. Using CPU." -ForegroundColor Yellow
}

# 2. Define common packages
$commonPkgs = @(
    "fastapi", "uvicorn[standard]", "python-multipart", "python-dotenv", 
    "numpy", "opencv-python-headless", "onnx", "ultralytics", "onnxslim", 
    "aiortc", "av", "requests", "onvif-zeep", "WSDiscovery",
    "pillow-heif"
)

# 3. Install based on hardware
if ($hasNvidia) {
    Write-Host "Installing dependencies (GPU mode)..."
    pip install $commonPkgs "onnxruntime-gpu>=1.17,<2.0"
} else {
    Write-Host "Installing dependencies (CPU mode)..."
    pip install $commonPkgs "onnxruntime>=1.17,<2.0"
}

# 4. Download face models (OpenCV Zoo)
$dataDir = "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }

$models = [ordered]@{
    "face_detection_yunet_2023mar.onnx"  = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    "face_recognition_sface_2021dec.onnx" = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
}

Write-Host "`nDownloading face models..." -ForegroundColor Cyan
foreach ($name in $models.Keys) {
    $path = Join-Path $dataDir $name
    if (Test-Path $path) {
        Write-Host "  $name already present" -ForegroundColor Green
        continue
    }
    try {
        Invoke-WebRequest -Uri $models[$name] -OutFile $path -UseBasicParsing
        $sizeMb = [math]::Round((Get-Item $path).Length / 1MB, 2)
        Write-Host "  Downloaded $name ($sizeMb MB)" -ForegroundColor Green
    } catch {
        Write-Host "  Failed to download $name" -ForegroundColor Red
        Write-Host "  Manual: $($models[$name]) -> $path"
    }
}
Write-Host "`nSetting up person detector..." -ForegroundColor Cyan
$yoloPath = Join-Path $dataDir "yolov8n.onnx"
if (Test-Path $yoloPath) {
    Write-Host "  yolov8n.onnx already present" -ForegroundColor Green
} else {
    Write-Host "  Exporting yolov8n.onnx (downloads yolov8n.pt on first run)..." -ForegroundColor Yellow
    python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, opset=12, simplify=True)"
    if (Test-Path "yolov8n.onnx") {
        Move-Item -Force "yolov8n.onnx" $yoloPath
        Remove-Item -Force "yolov8n.pt" -ErrorAction SilentlyContinue
        Write-Host "  Saved $yoloPath" -ForegroundColor Green
    } else {
        Write-Host "  Export failed. Run manually:" -ForegroundColor Red
        Write-Host "  python -c `"from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx')`"" -ForegroundColor Red
    }
}

Write-Host "`nSetup complete! Run 'python run.py' to start the server." -ForegroundColor Cyan