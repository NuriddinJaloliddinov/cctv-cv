"""Direct Subnet Scanner for NVRs and IP Cameras (No multicast required)."""
import socket
import ipaddress
import concurrent.futures
import requests
from urllib.parse import urlparse

from onvif import ONVIFCamera
from . import db
from .models import CameraInfo

COMMON_PORTS = [80, 554, 8000, 8080]


def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _check_host(ip: str, timeout: float = 0.5) -> str | None:
    open_ports = []
    for port in COMMON_PORTS:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                open_ports.append(port)
        except Exception:
            pass

    if not open_ports:
        return None

    try:
        r = requests.get(f"http://{ip}/onvif/device_service", timeout=1)
        if r.status_code in (200, 401, 405):
            return ip
    except Exception:
        pass

    if 554 in open_ports:
        return ip

    return None


def discover_devices(timeout: int = 4, subnet: str = None) -> list[CameraInfo]:
    """Scan the local subnet for devices with camera-specific ports open.

    Already-persisted cameras (matched by address) are filtered out,
    so the frontend only ever shows *new* candidates.
    """
    if subnet is None:
        local_ip = _get_local_ip()
        network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    else:
        network = ipaddress.ip_network(subnet, strict=False)

    print(f"[scanner] Scanning {network} for cameras...")
    hosts = [str(ip) for ip in network.hosts()]
    found_ips = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(_check_host, ip): ip for ip in hosts}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                found_ips.append(result)
                print(f"[scanner] Found potential camera at {result}")

    existing_addresses = {c.address for c in db.list_cameras()}

    cameras = []
    for ip in found_ips:
        address = f"http://{ip}/onvif/device_service"
        if address in existing_addresses:
            continue
        cameras.append(CameraInfo(
            id=ip,
            name=f"Device {ip}",
            address=address,
            rtsp_url=None,
            source_type="onvif",
        ))

    print(f"[scanner] Scan complete. Found {len(cameras)} new device(s).")
    return cameras


def fetch_rtsp_url(address: str, username: str, password: str) -> str:
    """Given an ONVIF device address, return the first RTSP URL."""
    parsed = urlparse(address)
    if not parsed.hostname:
        raise RuntimeError(f"Invalid ONVIF address: {address}")
    host, port = parsed.hostname, parsed.port or 80

    try:
        cam = ONVIFCamera(host, port, username, password)
        media = cam.create_media_service()
        profiles = media.GetProfiles()
    except Exception as e:
        raise RuntimeError(f"ONVIF connect failed (check username/password): {e}") from e

    if not profiles:
        raise RuntimeError("Device reports no media profiles")

    uri = media.GetStreamUri({
        "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
        "ProfileToken": profiles[0].token,
    })
    return uri.Uri