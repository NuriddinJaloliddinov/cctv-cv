"""Unauthenticated discovery of NVRs and IP cameras.

A device is reported only when it exposes a video service that can be
identified without logging in:

* an ONVIF device service that answers a SOAP request (an authentication
  fault still proves the endpoint is ONVIF), or
* an RTSP server that answers an OPTIONS request.

No credentials are used or requested here. Whether a stream is actually
accessible is verified later, when the user supplies credentials for a
device (``fetch_stream_profiles`` + ``check_stream``).
"""
import concurrent.futures
import ipaddress
import os
import socket
import warnings
from urllib.parse import unquote, urlparse

import cv2
import requests
from onvif import ONVIFCamera
from urllib3.exceptions import InsecureRequestWarning

from . import db
from .models import CameraInfo

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

HTTP_PORTS = (80, 443, 8000, 8080)
RTSP_PORTS = (554, 8554)

TCP_TIMEOUT = 0.25
ONVIF_TIMEOUT = 1.5
RTSP_TIMEOUT = 1.0
WS_DISCOVERY_TIMEOUT = 3.0
MAX_HOSTS_PER_NETWORK = 1024
MAX_WORKERS = 160

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

_SOAP_GET_DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Body>
    <GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>
  </s:Body>
</s:Envelope>"""


def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _prefix_from_netmask(netmask: str) -> int:
    return ipaddress.ip_network(f"0.0.0.0/{netmask}", strict=False).prefixlen


def local_interfaces() -> list[tuple[str, int]]:
    """Every usable IPv4 interface as (address, prefix_length).

    Falls back to the default-route address with a /24 when psutil is
    unavailable, so single-NIC machines still work.
    """
    try:
        import psutil

        found = set()
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family != socket.AF_INET or not a.netmask:
                    continue
                if a.address.startswith(("127.", "169.254.")):
                    continue
                try:
                    found.add((a.address, _prefix_from_netmask(a.netmask)))
                except ValueError:
                    continue
        if found:
            return sorted(found)
    except Exception:
        pass
    return [(_get_local_ip(), 24)]


def _bounded_network(ip: str, prefix: int) -> ipaddress.IPv4Network:
    """Containing network, capped so a scan stays practical."""
    net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    if net.num_addresses > MAX_HOSTS_PER_NETWORK:
        net = ipaddress.ip_network(f"{ip}/22", strict=False)
    return net


def _port_open(ip: str, port: int, timeout: float = TCP_TIMEOUT) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _onvif_probe(ip: str, port: int, tls: bool = False) -> bool:
    """True when the endpoint answers like an ONVIF device service."""
    scheme = "https" if tls else "http"
    url = f"{scheme}://{ip}:{port}/onvif/device_service"
    try:
        r = requests.post(
            url,
            data=_SOAP_GET_DEVICE_INFO.encode("utf-8"),
            headers={
                "Content-Type": "application/soap+xml; charset=utf-8",
                "User-Agent": "cctv-cv-discovery",
            },
            timeout=ONVIF_TIMEOUT,
            verify=False,
            allow_redirects=True,
        )
    except requests.RequestException:
        return False

    if "Envelope" in (r.text or ""):
        return True
    challenge = (r.headers.get("WWW-Authenticate") or "").lower()
    return r.status_code == 401 and ("wsse" in challenge or "onvif" in challenge)


def _rtsp_probe(ip: str, port: int) -> bool:
    """True when a real RTSP server answers OPTIONS (401 included)."""
    try:
        with socket.create_connection((ip, port), timeout=RTSP_TIMEOUT) as s:
            s.settimeout(RTSP_TIMEOUT)
            request = (
                f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\n"
                f"CSeq: 1\r\nUser-Agent: cctv-cv-discovery\r\n\r\n"
            )
            s.sendall(request.encode("ascii"))
            return s.recv(256).startswith(b"RTSP/1.0")
    except OSError:
        return False


def _probe_host(ip: str) -> dict | None:
    for port in HTTP_PORTS:
        if _port_open(ip, port) and _onvif_probe(ip, port, tls=(port == 443)):
            return {"ip": ip, "kind": "onvif", "port": port, "name": None, "address": None}

    for port in RTSP_PORTS:
        if _port_open(ip, port) and _rtsp_probe(ip, port):
            return {"ip": ip, "kind": "rtsp", "port": port, "name": None}

    return None


def _ws_discover(timeout: float = WS_DISCOVERY_TIMEOUT) -> dict[str, dict]:
    """ONVIF WS-Discovery over multicast; returns {ip: info}.

    Windows WSD printers/scanners answer the same multicast, so only
    services advertising ONVIF scopes are considered, and their device
    service is probed before being accepted.
    """
    try:
        from wsdiscovery.discovery import ThreadedWSDiscovery
    except Exception:
        return {}

    out: dict[str, dict] = {}
    probed: dict[tuple[str, int, bool], bool] = {}
    wsd = ThreadedWSDiscovery()
    try:
        wsd.start()
        for svc in wsd.searchServices(timeout=timeout):
            scopes = []
            for scope in (svc.getScopes() or []):
                try:
                    scopes.append(scope.getValue())
                except Exception:
                    scopes.append(str(scope))

            if not any("onvif.org" in s.lower() for s in scopes):
                continue

            name = hardware = None
            for value in scopes:
                if "/name/" in value:
                    name = unquote(value.split("/name/", 1)[1])
                elif "/hardware/" in value and not hardware:
                    hardware = unquote(value.split("/hardware/", 1)[1])

            for xaddr in (svc.getXAddrs() or []):
                parsed = urlparse(xaddr)
                host = parsed.hostname
                if not host:
                    continue
                try:
                    if ipaddress.ip_address(host).version != 4:
                        continue
                except ValueError:
                    continue
                tls = parsed.scheme == "https"
                port = parsed.port or (443 if tls else 80)
                key = (host, port, tls)
                if key not in probed:
                    probed[key] = _onvif_probe(host, port, tls=tls)
                if not probed[key]:
                    continue
                out.setdefault(host, {
                    "ip": host,
                    "kind": "onvif",
                    "address": xaddr,
                    "name": name or hardware,
                })
    except Exception:
        pass
    finally:
        try:
            wsd.stop()
        except Exception:
            pass
    return out


def _onvif_address(ip: str, port: int) -> str:
    scheme = "https" if port == 443 else "http"
    default = (port == 80 and scheme == "http") or (port == 443 and scheme == "https")
    return f"{scheme}://{ip}{'' if default else f':{port}'}/onvif/device_service"


def _ip_key(ip: str):
    try:
        return tuple(int(part) for part in ip.split("."))
    except ValueError:
        return (999, 999, 999, 999)


def discover_devices(timeout: int = 4, subnet: str | None = None) -> list[CameraInfo]:
    """Find video-capable devices reachable from this machine.

    Scans every local interface (or ``subnet`` when given) and combines the
    results with WS-Discovery. Devices that do not answer ONVIF or RTSP are
    not reported. Already-saved cameras are filtered out by host.
    """
    if subnet:
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid subnet '{subnet}': {e}") from e
        if network.num_addresses > MAX_HOSTS_PER_NETWORK:
            raise ValueError(f"Subnet {network} is too large; use /22 or smaller")
        networks = [network]
    else:
        networks = []
        for ip, prefix in local_interfaces():
            net = _bounded_network(ip, prefix)
            if net not in networks:
                networks.append(net)

    targets = set()
    for net in networks:
        targets.update(str(ip) for ip in net.hosts())

    print(f"[discover] scanning {', '.join(str(n) for n in networks)} ({len(targets)} hosts)")

    found: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        ws_future = executor.submit(_ws_discover, min(timeout, WS_DISCOVERY_TIMEOUT))
        futures = [executor.submit(_probe_host, ip) for ip in sorted(targets)]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception:
                continue
            if result:
                found[result["ip"]] = result
        try:
            ws_found = ws_future.result()
        except Exception:
            ws_found = {}

    for ip, info in ws_found.items():
        if ip in found:
            found[ip]["name"] = found[ip].get("name") or info.get("name")
            found[ip]["address"] = found[ip].get("address") or info.get("address")
        else:
            found[ip] = info

    existing_addresses = set()
    existing_hosts = set()
    for saved in db.list_cameras():
        existing_addresses.add(saved.address)
        if "#" not in saved.address:
            host = urlparse(saved.address).hostname
            if host:
                existing_hosts.add(host)

    cameras: list[CameraInfo] = []
    for ip in sorted(found, key=_ip_key):
        info = found[ip]
        name = info.get("name") or f"Device {ip}"
        if info["kind"] == "onvif":
            address = info.get("address") or _onvif_address(ip, info.get("port", 80))
            source_type, rtsp_url = "onvif", None
        else:
            address = f"rtsp://{ip}:{info['port']}/"
            source_type, rtsp_url = "manual-rtsp", address

        if ip in existing_hosts or address in existing_addresses:
            continue

        cameras.append(CameraInfo(
            id=ip,
            name=name,
            address=address,
            rtsp_url=rtsp_url,
            source_type=source_type,
        ))

    print(f"[discover] {len(cameras)} video device(s) found")
    return cameras


def fetch_stream_profiles(address: str, username: str, password: str) -> list[dict]:
    """All media profiles (NVR channels) with their RTSP URIs.

    Requires credentials supplied by the user; never called by discovery.
    """
    parsed = urlparse(address)
    if not parsed.hostname:
        raise RuntimeError(f"Invalid ONVIF address: {address}")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        # onvif-zeep keeps the scheme only when it is part of the host
        host = f"https://{host}"

    try:
        cam = ONVIFCamera(host, port, username or "", password or "")
        media = cam.create_media_service()
        profiles = media.GetProfiles()
    except Exception as e:
        raise RuntimeError(f"ONVIF connect failed (check username/password): {e}") from e

    if not profiles:
        raise RuntimeError("Device reports no media profiles")

    out = []
    for p in profiles:
        item = {
            "token": p.token,
            "name": getattr(p, "Name", None) or p.token,
            "encoding": "",
            "width": None,
            "height": None,
            "rtsp_url": None,
        }
        enc = getattr(p, "VideoEncoderConfiguration", None)
        if enc is not None:
            item["encoding"] = getattr(enc, "Encoding", "") or ""
            res = getattr(enc, "Resolution", None)
            if res is not None:
                item["width"] = getattr(res, "Width", None)
                item["height"] = getattr(res, "Height", None)
        try:
            uri = media.GetStreamUri({
                "StreamSetup": {
                    "Stream": "RTP-Unicast",
                    "Transport": {"Protocol": "RTSP"},
                },
                "ProfileToken": p.token,
            })
            item["rtsp_url"] = uri.Uri
        except Exception as e:
            item["error"] = f"stream URI failed: {e}"
        out.append(item)
    return out


def fetch_rtsp_url(address: str, username: str, password: str) -> str:
    """First RTSP URI reported by the device (kept for the Run shortcut)."""
    for profile in fetch_stream_profiles(address, username, password):
        if profile.get("rtsp_url"):
            return profile["rtsp_url"]
    raise RuntimeError("Device reports no RTSP stream URI")


def check_stream(rtsp_url: str, timeout: float = 8.0) -> dict:
    """Open a stream and decode one frame — i.e. prove it is accessible."""
    if not rtsp_url:
        return {"accessible": False, "error": "no stream URL"}

    ms = int(timeout * 1000)
    cap = cv2.VideoCapture(cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms)
        if not cap.open(rtsp_url):
            return {"accessible": False, "error": "could not open stream"}
        ok, frame = cap.read()
        if not ok or frame is None:
            return {"accessible": False, "error": "no video frame received"}
        h, w = frame.shape[:2]
        return {"accessible": True, "width": w, "height": h}
    except Exception as e:
        return {"accessible": False, "error": str(e)}
    finally:
        cap.release()
