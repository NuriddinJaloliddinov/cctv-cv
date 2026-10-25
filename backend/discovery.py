"""Credential-aware discovery of NVRs and IP cameras.

The scanner deliberately combines several independent sources so a device is
not missed just because it uses an unusual port or does not answer one
protocol:

* every local IPv4 interface and every IPv4 route,
* ONVIF WS-Discovery (multicast and unicast probes),
* SSDP/UPnP announcements,
* the operating system's ARP/neighbour table,
* a broad TCP port sweep, with an optional full-range scan.

Discovery does not submit credentials. Anonymous ONVIF/RTSP responses identify
candidate endpoints; protected RTSP endpoints are labelled ``credentials``
until the operator chooses one and explicitly submits its login. Only then
are its streams checked for a decodable frame before it can be added.

Only endpoints with ONVIF or RTSP evidence are returned; an authenticated
RTSP service is not claimed as verified video before a login succeeds.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import ipaddress
import os
import re
import select
import socket
import subprocess
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import cv2
import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from urllib3.exceptions import InsecureRequestWarning

from . import db
from .models import (DiscoveredChannel, DiscoveredDevice, DiscoveryReport,
                     DiscoveryRequest, DiscoveryConnectRequest)

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Ports probed on every host before the wider list is used on responsive hosts.
QUICK_PORTS = (80, 443, 554, 8000, 8080, 8554)

# HTTP/ONVIF ports. These cover the default ports of the major NVR/camera
# vendors as well as common alternates.
HTTP_PORTS = (
    80, 81, 82, 83, 84, 85, 88, 443, 800, 808, 888, 1080, 1443, 2000, 3000,
    5000, 7001, 8000, 8001, 8008, 8010, 8080, 8081, 8088, 8090, 8443, 8800,
    8888, 8899, 9000, 9001, 9080, 9443, 10000, 10280, 18000, 20000,
)

# RTSP ports, including vendor alternates. A device may advertise a different
# port through WS-Discovery/SSDP; that exact port is probed as well.
RTSP_PORTS = (554, 555, 556, 1554, 6654, 8554, 9554, 10554)

ALL_PROBE_PORTS = tuple(sorted(set(HTTP_PORTS) | set(RTSP_PORTS)))

ONVIF_PATHS = (
    "/onvif/device_service",
    "/onvif/device_service/",
    "/onvif/services",
    "/onvif/device",
)

# Common RTSP paths, tried only when a server answers but the SDP is not on
# the default path. This is what lets a camera with a vendor-specific path be
# identified without asking the user for a URL.
# Ordered by how common the vendor path is. Authentication failures are
# capped, so the most likely path must come first: a camera can lock the
# account after repeated bad logins.
COMMON_RTSP_PATHS = (
    "/Streaming/Channels/101",              # Hikvision / HiLook / OEM
    "/Streaming/Channels/202",              # NVR channel 2 substream
    "/Streaming/Channels/102",              # camera substream
    "/cam/realmonitor?channel=1&subtype=0",  # Dahua / Amcrest
    "/axis-media/media.amp",                # Axis
    "/h264/ch1/main/av_stream",             # Hikvision OEM
    "/live",
    "/stream1",
    "/onvif1",
    "/",
    "/live.sdp",
    "/h264",
    "/video",
    "/media/video1",
    "/img/video.sav",
    "/mpeg4",
)

# Cameras commonly lock an account after a handful of bad logins, so the
# whole discovery run spends a small budget of password attempts per host
# instead of multiplying attempts across every port and every path. ONVIF and
# RTSP keep separate counters: a camera with ONVIF disabled answers 401 to
# ONVIF even for the correct password, and that must not consume the RTSP
# attempts that can actually verify the stream.
MAX_RTSP_AUTH_FAILURES = 4
MAX_ONVIF_AUTH_FAILURES = 2
# Without credentials only a few path guesses are useful; most cameras answer
# 401 immediately and the rest are not public video sources.
MAX_ANON_RTSP_PATHS = 8

TCP_TIMEOUT = 0.45
ONVIF_TIMEOUT = 2.5
RTSP_TIMEOUT = 2.0
STREAM_TIMEOUT = 7.0
WS_TIMEOUT = 3.0
SSDP_TIMEOUT = 2.5
ARP_WAIT = 0.8

MAX_HOSTS_PER_NETWORK = 65534       # one /16
MAX_TOTAL_HOSTS = 65534
MAX_WORKERS = 512
MAX_SCAN_THREADS = 128
VALIDATION_WORKERS = 10
MAX_CREDENTIALS = 4
MAX_SSDP_DESCRIPTIONS = 100
# A reachable device should never be skipped, but scanning 65k addresses that
# the OS cannot prove are alive would take hours. Directly connected networks
# use the ARP cache; routed networks and the no-ARP fallback are capped.
MAX_ROUTED_HOSTS = 4096
MAX_FALLBACK_HOSTS = 1024
MAX_DEEP_HOSTS = 16
MAX_STREAM_CHECKS = 6               # full decodes per device

WS_MULTICAST = ("239.255.255.250", 3702)
SSDP_MULTICAST = ("239.255.255.250", 1900)

_thread_local = threading.local()


def _session() -> requests.Session:
    """One keep-alive HTTP session per worker thread.

    Cameras often use a slow TLS handshake; reusing the connection makes a
    multi-operation ONVIF probe several times faster.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _thread_local.session = session
    return session


_SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
_SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
_WSSE = ("http://docs.oasis-open.org/wss/2004/01/"
         "oasis-200401-wss-wssecurity-secext-1.0.xsd")
_WSU = ("http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-utility-1.0.xsd")
_PASSWORD_DIGEST = ("http://docs.oasis-open.org/wss/2004/01/"
                    "oasis-200401-wss-username-token-profile-1.0"
                    "#PasswordDigest")
_PASSWORD_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
                  "oasis-200401-wss-username-token-profile-1.0"
                  "#PasswordText")
_BASE64 = ("http://docs.oasis-open.org/wss/2004/01/"
           "oasis-200401-wss-soap-message-security-1.0#Base64Binary")

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA10_NS = "http://www.onvif.org/ver10/media/wsdl"
_MEDIA20_NS = "http://www.onvif.org/ver20/media/wsdl"

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(root, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for element in root.iter():
        if _local(element.tag).lower() in wanted:
            return (element.text or "").strip()
    return ""


def _descendant_text(element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if _local(child.tag).lower() in wanted:
            return (child.text or "").strip()
    return ""


def _has_onvif_response(root, name: str) -> bool:
    """True only when the response element is in an ONVIF namespace.

    Checking the local tag alone would let an unrelated SOAP service (a
    printer, a router, a NAS) masquerade as a camera.
    """
    for element in root.iter():
        if (_local(element.tag).lower() == name.lower()
                and "onvif.org" in element.tag):
            return True
    return False


def _safe_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _prefix_from_netmask(netmask: str) -> int:
    return ipaddress.ip_network(f"0.0.0.0/{netmask}", strict=False).prefixlen


def _get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def _is_ignored_network(net: ipaddress.IPv4Network) -> bool:
    return (net.is_loopback or net.is_link_local or net.is_multicast
            or net.is_unspecified or net == ipaddress.ip_network("255.255.255.255/32"))


def _cap_network(net: ipaddress.IPv4Network,
                 anchor: str | None = None) -> ipaddress.IPv4Network:
    """Return a network small enough to scan, keeping it near ``anchor``."""
    if net.num_addresses <= MAX_HOSTS_PER_NETWORK:
        return net
    if anchor:
        try:
            address = ipaddress.ip_address(anchor)
            prefix = 16
            return ipaddress.ip_network(f"{address}/{prefix}", strict=False)
        except ValueError:
            pass
    return ipaddress.ip_network(f"{net.network_address}/16", strict=False)


# ---------------------------------------------------------------------------
# Interface, route and neighbour discovery
# ---------------------------------------------------------------------------

def local_interfaces() -> list[tuple[str, int]]:
    """Every usable IPv4 interface as (address, prefix_length)."""
    try:
        import psutil

        found = set()
        for addrs in psutil.net_if_addrs().values():
            for address in addrs:
                if address.family != socket.AF_INET or not address.netmask:
                    continue
                if address.address.startswith("127."):
                    continue
                try:
                    found.add((address.address,
                               _prefix_from_netmask(address.netmask)))
                except ValueError:
                    continue
        if found:
            return sorted(found)
    except Exception:
        pass
    return [(_get_local_ip(), 24)]


def _route_networks() -> list[ipaddress.IPv4Network]:
    """Networks in the local routing table (the useful non-default routes)."""
    networks: list[ipaddress.IPv4Network] = []
    try:
        if os.name == "nt":
            output = subprocess.run(
                ["route", "print", "-4"], capture_output=True, text=True,
                timeout=5, check=False).stdout
            for line in output.splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    destination = ipaddress.ip_address(parts[0])
                    mask = ipaddress.ip_address(parts[1])
                except ValueError:
                    continue
                if destination.version != 4:
                    continue
                try:
                    prefix = _prefix_from_netmask(str(mask))
                    network = ipaddress.ip_network(
                        f"{destination}/{prefix}", strict=False)
                except ValueError:
                    continue
                networks.append(network)
        elif os.name == "posix":
            output = subprocess.run(
                ["ip", "-4", "route", "show"], capture_output=True, text=True,
                timeout=5, check=False).stdout
            if output.strip():
                for line in output.splitlines():
                    token = line.split()[0] if line.split() else ""
                    if not token or token == "default":
                        continue
                    try:
                        networks.append(
                            ipaddress.ip_network(token, strict=False))
                    except ValueError:
                        continue
            else:
                output = subprocess.run(
                    ["netstat", "-rn", "-f", "inet"], capture_output=True,
                    text=True, timeout=5, check=False).stdout
                for line in output.splitlines():
                    parts = line.split()
                    if len(parts) < 3 or parts[0] in ("Destination", "default"):
                        continue
                    destination, gateway, netmask = parts[0], parts[1], parts[2]
                    try:
                        prefix = _prefix_from_netmask(netmask)
                        networks.append(ipaddress.ip_network(
                            f"{destination}/{prefix}", strict=False))
                    except ValueError:
                        continue
    except Exception:
        pass

    useful = []
    seen = set()
    for network in networks:
        if _is_ignored_network(network) or network.prefixlen == 0:
            continue
        if network not in seen:
            seen.add(network)
            useful.append(network)
    return useful


def _neighbour_table() -> dict[str, str]:
    """Return {ip: mac} from the OS ARP/neighbour cache."""
    found: dict[str, str] = {}
    try:
        if os.name == "nt":
            output = subprocess.run(
                ["arp", "-a"], capture_output=True, text=True, timeout=5,
                check=False).stdout
            for line in output.splitlines():
                match = re.match(
                    r"\s*(\d+\.\d+\.\d+\.\d+)\s+"
                    r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})\s+",
                    line)
                if not match:
                    continue
                ip, mac = match.group(1), match.group(2).lower()
                if mac not in ("ff-ff-ff-ff-ff-ff", "00-00-00-00-00-00"):
                    found[ip] = mac
        elif os.name == "posix":
            output = subprocess.run(
                ["ip", "neigh", "show"], capture_output=True, text=True,
                timeout=5, check=False).stdout
            if output.strip():
                for line in output.splitlines():
                    parts = line.split()
                    if not parts:
                        continue
                    if "FAILED" in parts or "INCOMPLETE" in parts:
                        continue
                    match = re.search(
                        r"lladdr\s+([0-9a-fA-F:]{17})", line)
                    if match:
                        found[parts[0]] = match.group(1).lower()
            else:
                output = subprocess.run(
                    ["arp", "-an"], capture_output=True, text=True, timeout=5,
                    check=False).stdout
                for line in output.splitlines():
                    match = re.search(
                        r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+"
                        r"([0-9a-fA-F:]{17})", line)
                    if match and "incomplete" not in line.lower():
                        found[match.group(1)] = match.group(2).lower()
    except Exception:
        pass
    return found


def _arp_sweep(hosts: list[str], wait: float = ARP_WAIT) -> set[str]:
    """Populate the neighbour cache with a quick UDP probe to each host."""
    if not hosts:
        return set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        for host in hosts:
            try:
                sock.sendto(b"", (host, 9))
            except OSError:
                pass
    finally:
        sock.close()
    if wait:
        time.sleep(wait)
    return set(_neighbour_table())


# ---------------------------------------------------------------------------
# WS-Discovery / SSDP
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    host: str
    port: int
    scheme: str = "http"
    path: str | None = None
    source: str = "scan"
    name: str = ""
    xaddr: str | None = None
    kind_hint: str = ""

    @property
    def url(self) -> str:
        if self.xaddr:
            return self.xaddr
        path = self.path or ""
        return f"{self.scheme}://{self.host}:{self.port}{path}"


def _ws_probe_message(message_id: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="{_SOAP12}"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>{message_id}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body>
  <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
 </e:Body>
</e:Envelope>""".encode("utf-8")


def _parse_ws_response(data: bytes, source_ip: str) -> list[Endpoint]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []

    types = " ".join(
        (element.text or "") for element in root.iter()
        if _local(element.tag) == "Types")
    scopes: list[str] = []
    for element in root.iter():
        if _local(element.tag) != "Scopes" or not element.text:
            continue
        scopes.extend(element.text.split())

    if ("NetworkVideoTransmitter" not in types
            and "onvif.org" not in types.lower()
            and not any("onvif.org" in scope.lower() for scope in scopes)):
        return []

    name = ""
    # NetworkVideoTransmitter is used by many NVRs as well as cameras; only
    # an explicit recorder scope is sufficient to distinguish an NVR here.
    kind_hint = ("nvr" if any(
        re.search(r"(?:network[_-]?video[_-]?recorder|\bnvr\b|\bdvr\b|\bxvr\b)",
                  unquote(scope), re.IGNORECASE)
        for scope in scopes if "/type/" in scope.lower()) else "")
    for scope in scopes:
        if "/name/" in scope:
            name = unquote(scope.split("/name/", 1)[1])
        elif "/hardware/" in scope and not name:
            name = unquote(scope.split("/hardware/", 1)[1])

    endpoints: list[Endpoint] = []
    for element in root.iter():
        if _local(element.tag) != "XAddrs" or not element.text:
            continue
        for address in element.text.split():
            parsed = urlsplit(address)
            host = parsed.hostname or source_ip
            scheme = parsed.scheme if parsed.scheme in ("http", "https") else "http"
            port = parsed.port or (443 if scheme == "https" else 80)
            if not host:
                continue
            endpoints.append(Endpoint(
                host=host,
                port=port,
                scheme=scheme,
                path=parsed.path or None,
                source="ws",
                name=name,
                xaddr=address,
                kind_hint=kind_hint,
            ))
    return endpoints


def _ws_discover(timeout: float = WS_TIMEOUT,
                 unicast_hosts: list[str] | None = None) -> list[Endpoint]:
    """ONVIF WS-Discovery over multicast, plus unicast probes."""
    endpoints: list[Endpoint] = []
    try:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tx.bind(("", 0))
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    except OSError:
        return []

    rx = None
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", 3702))
        rx.setblocking(False)
    except OSError:
        rx = None

    try:
        message_id = f"uuid:{uuid.uuid4()}"
        message = _ws_probe_message(message_id)
        for _ in range(2):
            try:
                tx.sendto(message, WS_MULTICAST)
            except OSError:
                pass
            time.sleep(0.05)
        for host in unicast_hosts or []:
            try:
                tx.sendto(message, (host, 3702))
            except OSError:
                pass

        deadline = time.monotonic() + max(0.5, timeout)
        sockets = [tx] + ([rx] if rx is not None else [])
        while time.monotonic() < deadline:
            wait = min(0.25, max(0.0, deadline - time.monotonic()))
            try:
                readable, _, _ = select.select(sockets, [], [], wait)
            except (OSError, ValueError):
                break
            for sock in readable:
                try:
                    data, address = sock.recvfrom(65535)
                except OSError:
                    continue
                endpoints.extend(_parse_ws_response(data, address[0]))
    finally:
        tx.close()
        if rx is not None:
            rx.close()

    return _dedupe_endpoints(endpoints)


def _ssdp_search_message(search_target: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"ST: {search_target}\r\n"
        "MX: 2\r\n"
        "User-Agent: cctv-cv-discovery\r\n\r\n"
    ).encode("ascii")


def _ssdp_headers(data: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    text = data.decode("utf-8", "replace")
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _ssdp_device_name(location: str) -> str:
    try:
        response = requests.get(
            location, timeout=1.5, verify=False,
            headers={"User-Agent": "cctv-cv-discovery"})
        if not response.ok:
            return ""
        root = ET.fromstring(response.content)
    except Exception:
        return ""
    return (_first_text(root, "friendlyName")
            or _first_text(root, "modelName")
            or _first_text(root, "manufacturer"))


def _ssdp_discover(timeout: float = SSDP_TIMEOUT) -> list[Endpoint]:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
    except OSError:
        return []

    locations: dict[str, str] = {}
    try:
        for target in ("ssdp:all",
                       "urn:schemas-upnp-org:device:DigitalSecurityCamera:1",
                       "urn:schemas-upnp-org:device:Basic:1"):
            message = _ssdp_search_message(target)
            for _ in range(2):
                try:
                    sock.sendto(message, SSDP_MULTICAST)
                except OSError:
                    pass
                time.sleep(0.04)

        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            wait = min(0.25, max(0.0, deadline - time.monotonic()))
            try:
                readable, _, _ = select.select([sock], [], [], wait)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                data, _ = sock.recvfrom(65535)
            except OSError:
                continue
            headers = _ssdp_headers(data)
            location = headers.get("location")
            if location:
                locations.setdefault(location, headers.get("st", ""))
    finally:
        sock.close()

    endpoints: list[Endpoint] = []
    for location, _ in list(locations.items())[:MAX_SSDP_DESCRIPTIONS]:
        parsed = urlsplit(location)
        if not parsed.hostname or not parsed.port:
            continue
        endpoints.append(Endpoint(
            host=parsed.hostname,
            port=parsed.port,
            scheme=parsed.scheme if parsed.scheme in ("http", "https") else "http",
            path=parsed.path or None,
            source="ssdp",
            name=_ssdp_device_name(location),
            xaddr=location,
        ))
    return endpoints


# ---------------------------------------------------------------------------
# TCP scan
# ---------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = TCP_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _scan_ports(host: str, ports, timeout: float = TCP_TIMEOUT) -> list[int]:
    return [port for port in ports if _port_open(host, port, timeout)]


def _probe_many(tasks, timeout: float = TCP_TIMEOUT,
                workers: int = MAX_WORKERS) -> list[tuple[str, int]]:
    """Probe (host, port) pairs with a bounded pool of worker threads.

    ``tasks`` may be an iterable of hundreds of thousands of pairs; a queue of
    counters keeps memory flat instead of creating one Future per port.
    """
    pending = iter(tasks)
    found: list[tuple[str, int]] = []
    lock = threading.Lock()
    exhausted = False

    def worker():
        nonlocal exhausted
        while True:
            with lock:
                if exhausted:
                    return
                try:
                    host, port = next(pending)
                except StopIteration:
                    exhausted = True
                    return
            try:
                open_port = _port_open(host, port, timeout)
            except Exception:
                open_port = False
            if open_port:
                with lock:
                    found.append((host, port))

    threads = [
        threading.Thread(target=worker, daemon=True)
        for _ in range(max(1, min(workers, 256)))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return found


def _scan_hosts(hosts: list[str], deep: bool,
                 warnings_list: list[str]) -> dict[str, list[int]]:
    opened: dict[str, list[int]] = {}
    if not hosts:
        return opened

    quick_only = len(hosts) > 512
    initial_ports = QUICK_PORTS if quick_only else ALL_PROBE_PORTS

    if len(hosts) <= 256:
        # ARP has already proved these hosts exist, so probing one port per
        # task is fast and also parallelises the filtered ports on routers.
        tasks = ((host, port) for host in hosts for port in initial_ports)
        for host, port in _probe_many(tasks):
            opened.setdefault(host, []).append(port)
    else:
        # On a large fallback list, one task per host avoids thousands of
        # simultaneous ARP timeouts for addresses that are not alive.
        def scan_one(host: str) -> tuple[str, list[int]]:
            return host, _scan_ports(host, initial_ports)

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(MAX_SCAN_THREADS, len(hosts))) as pool:
            futures = [pool.submit(scan_one, host) for host in hosts]
            for future in concurrent.futures.as_completed(futures):
                try:
                    host, ports = future.result()
                except Exception:
                    continue
                if ports:
                    opened[host] = ports

    if quick_only and opened:
        responsive = list(opened)
        tasks = ((host, port) for host in responsive for port in ALL_PROBE_PORTS)
        for host, port in _probe_many(tasks):
            if port not in opened[host]:
                opened[host].append(port)

    # A fast, highly parallel TCP sweep can overwhelm a small NVR (or the
    # client's ephemeral-port pool), causing transient false negatives. Retry
    # the most likely camera ports at a gentler rate for modest LAN scopes.
    # This also catches hosts that did not answer the first sweep at all.
    if len(hosts) <= 64:
        priority = tuple(sorted(set(QUICK_PORTS) | {8001, 8081, 8899}))
        for _ in range(2):
            missing = ((host, port) for host in hosts for port in priority
                       if port not in opened.get(host, ()))
            for host, port in _probe_many(missing, timeout=0.9, workers=32):
                bucket = opened.setdefault(host, [])
                if port not in bucket:
                    bucket.append(port)

    for host in opened:
        opened[host].sort()

    if deep:
        # Full-range scans are only sensible on hosts that already look alive.
        # Doing it for every address on a /16 would take hours.
        targets = list(opened)
        if not targets:
            warnings_list.append(
                "Deep scan skipped: no responsive hosts were found.")
        elif len(targets) > MAX_DEEP_HOSTS:
            warnings_list.append(
                f"Deep scan limited to {MAX_DEEP_HOSTS} of {len(targets)} "
                "responsive hosts; add a target to scan a specific one.")
            targets = targets[:MAX_DEEP_HOSTS]
        if targets:
            warnings_list.append(
                f"Deep scan: checking all TCP ports on {len(targets)} "
                "responsive host(s); this can take a while.")
            tasks = ((host, port) for host in targets
                     for port in range(1, 65536))
            for host, port in _probe_many(tasks):
                if port not in opened[host]:
                    opened[host].append(port)
            for host in targets:
                opened[host].sort()
    return opened


# ---------------------------------------------------------------------------
# ONVIF / SOAP
# ---------------------------------------------------------------------------

@dataclass
class SoapResponse:
    root: ET.Element | None = None
    status: int = 0
    text: str = ""
    headers: dict = field(default_factory=dict)
    error: str = ""
    fault: str = ""
    subcode: str = ""


@dataclass
class OnvifResult:
    url: str = ""
    onvif: bool = False
    auth_required: bool = False
    authenticated: bool = False
    auth_failed: bool = False
    username: str = ""
    password: str = ""
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    profiles: list[dict] = field(default_factory=list)
    error: str = ""


@dataclass
class RtspResult:
    url: str = ""
    rtsp: bool = False
    auth_required: bool = False
    authenticated: bool = False
    auth_failed: bool = False
    video: bool = False
    username: str = ""
    password: str = ""
    path: str = ""
    sdp: str = ""
    server: str = ""
    stream_url: str = ""
    error: str = ""


class AuthBudget:
    """Cap failed logins per host so discovery cannot lock a camera account.

    Cameras commonly block an address after a handful of bad passwords. The
    budget is shared by every endpoint on a host because each endpoint would
    otherwise retry the same wrong pair.
    """

    def __init__(self, limit: int = MAX_RTSP_AUTH_FAILURES):
        self.limit = limit
        self.used: dict[str, int] = {}
        self.lock = threading.Lock()

    def allow(self, host: str) -> bool:
        with self.lock:
            return self.used.get(host, 0) < self.limit

    def record_failure(self, host: str) -> None:
        with self.lock:
            self.used[host] = self.used.get(host, 0) + 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _created_text(when: datetime | None = None) -> str:
    return (when or _utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wsse_header(username: str, password: str, created: str,
                 digest: bool = True) -> str:
    if not username:
        return ""
    nonce = os.urandom(16)
    if digest:
        raw = nonce + created.encode("utf-8") + password.encode("utf-8")
        value = base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")
        password_type = _PASSWORD_DIGEST
    else:
        value = password
        password_type = _PASSWORD_TEXT
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    return (
        f'<wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{xml_escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{password_type}">{xml_escape(value)}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{_BASE64}">{nonce_b64}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>")


def _soap_envelope(body: str, username: str = "", password: str = "",
                   created: str | None = None, digest: bool = True,
                   soap12: bool = True) -> bytes:
    ns = _SOAP12 if soap12 else _SOAP11
    security = (_wsse_header(username, password, created or _created_text(),
                             digest) if username else "")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{ns}">'
        f"<s:Header>{security}</s:Header>"
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")


def _soap_fault(root: ET.Element) -> tuple[str, str]:
    fault = ""
    subcode = ""
    for element in root.iter():
        name = _local(element.tag).lower()
        if name in ("faultstring", "text", "reason") and not fault:
            fault = (element.text or "").strip()
        elif name == "value" and not subcode:
            subcode = (element.text or "").strip()
    return fault, subcode


def _soap_post(url: str, body: str, action: str, username: str = "",
               password: str = "", created: str | None = None,
               digest: bool = True, timeout: float = ONVIF_TIMEOUT,
               soap12: bool = True) -> SoapResponse:
    envelope = _soap_envelope(body, username, password, created, digest, soap12)
    headers = {
        "Content-Type": (f'application/soap+xml; charset=utf-8; action="{action}"'
                         if soap12 else "text/xml; charset=utf-8"),
        "User-Agent": "cctv-cv-discovery",
    }
    if not soap12:
        headers["SOAPAction"] = f'"{action}"'

    auth = None
    for attempt in range(2):
        try:
            response = _session().post(
                url, data=envelope, headers=headers, timeout=timeout,
                verify=False, auth=auth, allow_redirects=True)
        except requests.RequestException as exc:
            return SoapResponse(status=0, error=str(exc))

        if response.status_code in (401, 403) and username and auth is None:
            challenge = response.headers.get("WWW-Authenticate", "")
            lowered = challenge.lower()
            if "digest" in lowered:
                auth = HTTPDigestAuth(username, password)
                continue
            if "basic" in lowered:
                auth = HTTPBasicAuth(username, password)
                continue
        break

    result = SoapResponse(
        status=response.status_code,
        text=response.text or "",
        headers=dict(response.headers),
    )
    if not result.text:
        return result
    try:
        result.root = ET.fromstring(result.text.encode("utf-8"))
    except ET.ParseError:
        return result
    result.fault, result.subcode = _soap_fault(result.root)
    return result


def _fault_is_auth(result: SoapResponse) -> bool:
    blob = f"{result.fault} {result.subcode}".lower()
    if result.status in (401, 403):
        return True
    return any(word in blob for word in (
        "notauthorized", "not authorized", "authentication",
        "notauthenticated", "failedauthentication", "securitytoken"))


def _soap_action(service_ns: str, operation: str) -> str:
    return f"{service_ns}/{operation}"


def _get_system_date_body() -> str:
    return f'<GetSystemDateAndTime xmlns="{_DEVICE_NS}"/>'


def _get_device_info_body() -> str:
    return f'<GetDeviceInformation xmlns="{_DEVICE_NS}"/>'


def _get_capabilities_body() -> str:
    return (f'<GetCapabilities xmlns="{_DEVICE_NS}">'
            "<Category>All</Category></GetCapabilities>")


def _get_services_body() -> str:
    return (f'<GetServices xmlns="{_DEVICE_NS}">'
            "<IncludeCapability>false</IncludeCapability></GetServices>")


def _get_profiles_body(ns: str = _MEDIA10_NS) -> str:
    return f'<GetProfiles xmlns="{ns}"/>'


def _get_stream_uri_body(token: str, ns: str = _MEDIA10_NS) -> str:
    token = xml_escape(token)
    if ns == _MEDIA20_NS:
        return (f'<GetStreamUri xmlns="{ns}">'
                f"<ProfileToken>{token}</ProfileToken>"
                "<Protocol>RTSP</Protocol></GetStreamUri>")
    return (
        f'<GetStreamUri xmlns="{ns}">'
        '<StreamSetup xmlns="http://www.onvif.org/ver10/schema">'
        '<Stream>RTP-Unicast</Stream>'
        "<Transport><Protocol>RTSP</Protocol></Transport>"
        "</StreamSetup>"
        f"<ProfileToken>{token}</ProfileToken>"
        "</GetStreamUri>")


def _parse_device_time(root: ET.Element | None) -> datetime | None:
    if root is None:
        return None
    values = {}
    for name in ("Year", "Month", "Day", "Hour", "Minute", "Second"):
        value = _first_text(root, name)
        if value:
            number = _safe_int(value)
            if number is not None:
                values[name] = number
    if len(values) != 6:
        return None
    try:
        return datetime(values["Year"], values["Month"], values["Day"],
                        values["Hour"], values["Minute"], values["Second"],
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_capability_media(root: ET.Element) -> str:
    media_urls: list[str] = []
    for element in root.iter():
        name = _local(element.tag)
        if name in ("Media", "Media2"):
            xaddr = _descendant_text(element, "XAddr")
            if xaddr:
                media_urls.append(xaddr)
        elif name == "Service":
            namespace = _descendant_text(element, "Namespace").lower()
            xaddr = _descendant_text(element, "XAddr")
            if xaddr and ("media" in namespace or "media" in xaddr.lower()):
                media_urls.append(xaddr)
    for url in media_urls:
        if "/ver10/media/" in url.lower() or url.lower().endswith("/media"):
            return url
    return media_urls[0] if media_urls else ""


def _parse_profiles(root: ET.Element) -> list[dict]:
    profiles: list[dict] = []
    for element in root.iter():
        if _local(element.tag) != "Profiles":
            continue
        token = element.attrib.get("token") or element.attrib.get("Token")
        if not token:
            continue
        name = _descendant_text(element, "Name") or token
        encoding = _descendant_text(element, "Encoding")
        width = _safe_int(_descendant_text(element, "Width"))
        height = _safe_int(_descendant_text(element, "Height"))
        profiles.append({
            "token": token,
            "name": name,
            "encoding": encoding,
            "width": width,
            "height": height,
            "rtsp_url": None,
            "error": None,
        })
    # Some devices repeat Profiles in nested elements; keep one per token.
    unique: dict[str, dict] = {}
    for profile in profiles:
        unique.setdefault(profile["token"], profile)
    return list(unique.values())


def _parse_stream_uri(root: ET.Element) -> str:
    for element in root.iter():
        if _local(element.tag) == "Uri" and (element.text or "").strip():
            return element.text.strip()
    return ""


def _media_urls(device_url: str) -> list[str]:
    parsed = urlsplit(device_url)
    if not parsed.hostname:
        return []
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    scheme = parsed.scheme or "http"
    netloc = f"{host}:{port}"
    paths = ["/onvif/media_service", "/onvif/media", "/onvif/Media",
             "/onvif/media2_service"]
    if parsed.path and "media" in parsed.path.lower():
        paths.insert(0, parsed.path)
    # A device_service path also gives a useful base.
    if parsed.path and "device_service" in parsed.path.lower():
        paths.append(parsed.path.replace("device_service", "media_service"))
    seen = set()
    urls = []
    for path in paths:
        url = f"{scheme}://{netloc}{path}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _onvif_urls(base_url: str) -> list[str]:
    parsed = urlsplit(base_url)
    if not parsed.hostname:
        return []
    path = (parsed.path or "").lower()
    if "onvif" in path and not path.endswith(("/", "/description.xml")):
        return [base_url]
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "http"
    return [f"{scheme}://{host}:{port}{path}" for path in ONVIF_PATHS]


def _soap_call(url: str, service_ns: str, operation: str, body: str,
               username: str = "", password: str = "",
               device_time: datetime | None = None,
               digest: bool = True,
               timeout: float = ONVIF_TIMEOUT) -> SoapResponse:
    action = _soap_action(service_ns, operation)
    created = _created_text(device_time) if username else None
    result = _soap_post(url, body, action, username, password, created,
                        digest, timeout, soap12=True)
    if result.root is None and result.status in (400, 415, 500):
        # Some older devices only speak SOAP 1.1.
        result = _soap_post(url, body, action, username, password, created,
                            digest, timeout, soap12=False)
    return result


def _try_onvif_url(url: str, username: str = "", password: str = "",
                   timeout: float = ONVIF_TIMEOUT,
                   trust_auth: bool = False) -> OnvifResult:
    """Query one ONVIF device-service URL with a credential pair.

    ``trust_auth`` is set only when another source (WS-Discovery, an open
    RTSP port, or a device the user already saved) already proved that this
    host is video equipment. Without that evidence a bare 401 from an
    arbitrary web server is never enough to call it a camera.
    """
    result = OnvifResult(url=url, username=username, password=password)
    device_time: datetime | None = None
    proof = False

    def auth_failure() -> OnvifResult:
        """The endpoint answered as ONVIF but rejected this credential pair."""
        result.auth_failed = True
        result.error = "Authentication failed"
        # ``trust_auth`` means another source already proved this is video
        # equipment, so a rejected password must not hide the device.
        result.onvif = proof or trust_auth
        return result

    # 1. GetSystemDateAndTime is the one ONVIF operation that should answer
    #    without credentials. A real response is absolute proof of ONVIF.
    date_response = _soap_call(
        url, _DEVICE_NS, "GetSystemDateAndTime",
        _get_system_date_body(), "", "", timeout=timeout)
    if date_response.root is not None:
        if _has_onvif_response(date_response.root,
                               "GetSystemDateAndTimeResponse"):
            proof = True
            device_time = _parse_device_time(date_response.root)
        elif _fault_is_auth(date_response):
            result.auth_required = True
    if date_response.status in (401, 403):
        result.auth_required = True
    if date_response.status == 0:
        # Connection refused / TLS mismatch / timeout: no point querying the
        # remaining operations on this URL.
        result.error = date_response.error or "no HTTP response"
        return result
    if (not proof and not result.auth_required
            and date_response.root is None):
        # A plain HTML 200/404/405 response, not a SOAP service. Sending the
        # other ONVIF operations here would only repeat the same answer.
        return result

    # Some devices require auth even for the clock. Retrying with credentials
    # synchronises the WS-Security timestamp, which many cameras reject if it
    # is more than a few minutes away from their clock.
    if (result.auth_required and username and device_time is None
            and not proof):
        auth_date = _soap_call(
            url, _DEVICE_NS, "GetSystemDateAndTime",
            _get_system_date_body(), username, password, timeout=timeout)
        if auth_date.root is not None:
            if _has_onvif_response(auth_date.root,
                                   "GetSystemDateAndTimeResponse"):
                proof = True
                device_time = _parse_device_time(auth_date.root)
            elif _fault_is_auth(auth_date):
                result.auth_required = True
        if (auth_date.status in (401, 403)
                or (auth_date.root is not None
                    and _fault_is_auth(auth_date))):
            result.auth_required = True
            if username:
                return auth_failure()

    # 2. Device information identifies the model and proves authentication.
    info_response = _soap_call(
        url, _DEVICE_NS, "GetDeviceInformation", _get_device_info_body(),
        username, password, device_time, timeout=timeout)
    if info_response.root is not None:
        if _has_onvif_response(info_response.root,
                               "GetDeviceInformationResponse"):
            proof = True
            result.authenticated = True
            result.manufacturer = _first_text(
                info_response.root, "Manufacturer")
            result.model = _first_text(info_response.root, "Model")
            result.firmware = _first_text(
                info_response.root, "FirmwareVersion")
        elif _fault_is_auth(info_response):
            result.auth_required = True
    if info_response.status in (401, 403):
        result.auth_required = True
        if username:
            return auth_failure()
    if (info_response.root is not None and _fault_is_auth(info_response)
            and username):
        result.auth_required = True
        return auth_failure()
    if (not proof and not result.auth_required
            and info_response.root is None and info_response.status != 0):
        return result

    # If authentication is required but the supplied pair was rejected, try
    # PasswordText as a compatibility fallback for very old devices.
    if (proof and result.auth_required and username
            and not result.authenticated):
        alt = _soap_call(
            url, _DEVICE_NS, "GetDeviceInformation", _get_device_info_body(),
            username, password, device_time, digest=False, timeout=timeout)
        if alt.root is not None and _has_onvif_response(
                alt.root, "GetDeviceInformationResponse"):
            result.authenticated = True
            result.manufacturer = _first_text(alt.root, "Manufacturer")
            result.model = _first_text(alt.root, "Model")
            result.firmware = _first_text(alt.root, "FirmwareVersion")

    # 3. Capabilities/GetServices can prove ONVIF on devices that do not
    #    implement GetSystemDateAndTime.
    media_url = ""
    caps = _soap_call(
        url, _DEVICE_NS, "GetCapabilities", _get_capabilities_body(),
        username, password, device_time, timeout=timeout)
    if caps.root is not None:
        if _has_onvif_response(caps.root, "GetCapabilitiesResponse"):
            proof = True
            media_url = _parse_capability_media(caps.root)
        elif _fault_is_auth(caps):
            result.auth_required = True
    if caps.status in (401, 403):
        result.auth_required = True
        if username:
            return auth_failure()
    if (caps.root is not None and _fault_is_auth(caps) and username):
        result.auth_required = True
        return auth_failure()
    if (not proof and not result.auth_required
            and caps.root is None and caps.status != 0):
        return result

    if not proof:
        services = _soap_call(
            url, _DEVICE_NS, "GetServices", _get_services_body(),
            username, password, device_time, timeout=timeout)
        if services.root is not None:
            if _has_onvif_response(services.root, "GetServicesResponse"):
                proof = True
                media_url = _parse_capability_media(services.root)
            elif _fault_is_auth(services):
                result.auth_required = True
        if services.status in (401, 403):
            result.auth_required = True
        if result.auth_required and username:
            return auth_failure()

    if not proof:
        if result.auth_required and trust_auth:
            result.onvif = True
            result.error = "Authentication required"
        return result

    result.onvif = True

    # 4. Find the media service and enumerate every channel/profile.
    media_candidates = ([media_url] if media_url else []) + _media_urls(url)
    seen = set()
    for candidate in media_candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        for namespace in (_MEDIA10_NS, _MEDIA20_NS):
            profiles_response = _soap_call(
                candidate, namespace, "GetProfiles",
                _get_profiles_body(namespace), username, password, device_time,
                timeout=timeout)
            if profiles_response.root is None:
                continue
            if _fault_is_auth(profiles_response):
                result.auth_required = True
                if username:
                    return auth_failure()
                continue
            profiles = _parse_profiles(profiles_response.root)
            if not profiles:
                continue

            for profile in profiles:
                uri_response = _soap_call(
                    candidate, namespace, "GetStreamUri",
                    _get_stream_uri_body(profile["token"], namespace),
                    username, password, device_time, timeout=timeout)
                if uri_response.root is None:
                    profile["error"] = "no response from GetStreamUri"
                    continue
                if _fault_is_auth(uri_response):
                    result.auth_required = True
                    if username:
                        return auth_failure()
                uri = _parse_stream_uri(uri_response.root)
                if uri:
                    profile["rtsp_url"] = uri
                else:
                    profile["error"] = "device returned no stream URI"
            result.profiles = profiles
            return result

    if result.auth_required and not result.authenticated:
        result.error = "Authentication required"
    elif result.authenticated:
        result.error = "Device reports no media profiles"
    else:
        result.error = "No media service answered"
    return result


def probe_onvif(url: str, username: str = "", password: str = "",
                timeout: float = ONVIF_TIMEOUT,
                trust_auth: bool = False) -> OnvifResult:
    """Probe one ONVIF device-service URL, trying the standard paths."""
    last = OnvifResult(url=url, username=username, password=password)
    for candidate in _onvif_urls(url):
        result = _try_onvif_url(candidate, username, password, timeout,
                                trust_auth)
        if result.onvif or result.auth_failed:
            return result
        last = result
        # A connection-level failure means this port does not speak HTTP at
        # all; the remaining paths would only repeat the same timeout.
        if result.error and not result.auth_required:
            break
    return last


# ---------------------------------------------------------------------------
# RTSP
# ---------------------------------------------------------------------------

def _rtsp_url(host: str, port: int, path: str = "/") -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if not path.startswith("/"):
        path = "/" + path
    return f"rtsp://{host}:{port}{path}"


def _recv_rtsp(sock: socket.socket, timeout: float) -> tuple[str, dict, bytes]:
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 1_000_000:
            break
    header, _, body = data.partition(b"\r\n\r\n")
    text = header.decode("iso-8859-1", "replace")
    lines = text.split("\r\n")
    status_line = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = _safe_int(headers.get("content-length")) or 0
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status_line, headers, body


def _rtsp_exchange(host: str, port: int, method: str, url: str,
                   cseq: int, extra: dict[str, str] | None = None,
                   timeout: float = RTSP_TIMEOUT) -> tuple[str, dict, bytes]:
    lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}",
             "User-Agent: cctv-cv-discovery"]
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    request = "\r\n".join(lines) + "\r\n\r\n"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(request.encode("utf-8"))
            return _recv_rtsp(sock, timeout)
    except OSError:
        return "", {}, b""


def _parse_auth_challenge(value: str) -> tuple[str, dict[str, str]]:
    if not value:
        return "", {}
    match = re.search(r"\b(Digest|Basic)\b", value, re.IGNORECASE)
    if not match:
        return "", {}
    scheme = match.group(1).lower()
    fields: dict[str, str] = {}
    for key, quoted, bare in re.findall(
            r"(\w+)\s*=\s*(?:\"([^\"]*)\"|([^,\s]+))", value):
        fields[key.lower()] = quoted or bare
    return scheme, fields


def _rtsp_auth_header(challenge: str, method: str, uri: str,
                      username: str, password: str) -> str:
    scheme, fields = _parse_auth_challenge(challenge)
    if scheme == "basic":
        token = base64.b64encode(
            f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    if scheme == "digest":
        realm = fields.get("realm", "")
        nonce = fields.get("nonce", "")
        qops = [part.strip().lower() for part in
                fields.get("qop", "").split(",") if part.strip()]
        if qops and "auth" not in qops:
            return ""  # auth-int requires a body digest; never send a bogus one.
        qop = "auth" if qops else ""
        opaque = fields.get("opaque", "")
        algorithm = fields.get("algorithm", "MD5")
        algorithm_name = algorithm.lower()
        if algorithm_name not in ("md5", "md5-sess", "sha-256", "sha-256-sess"):
            return ""
        cnonce = os.urandom(8).hex()
        nc = "00000001"

        def digest(value: str) -> str:
            method_hash = (hashlib.sha256 if algorithm_name.startswith("sha-256")
                           else hashlib.md5)
            return method_hash(value.encode("utf-8")).hexdigest()

        ha1 = digest(f"{username}:{realm}:{password}")
        if algorithm_name.endswith("-sess"):
            ha1 = digest(f"{ha1}:{nonce}:{cnonce}")
        ha2 = digest(f"{method}:{uri}")
        if qop:
            response = digest(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        else:
            response = digest(f"{ha1}:{nonce}:{ha2}")
        parts = [
            f'username="{username}"', f'realm="{realm}"',
            f'nonce="{nonce}"', f'uri="{uri}"',
            f'response="{response}"',
        ]
        # MD5 is the RFC 2617 default, so the parameter may be omitted. Some
        # cameras (e.g. the "IP Camera(L4368)" firmware family) reject an
        # otherwise valid digest when it is present, intermittently, so it is
        # only sent for an algorithm that actually differs from the default.
        if algorithm_name != "md5":
            parts.append(f"algorithm={algorithm}")
        if qop:
            parts.extend([f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'])
        elif algorithm_name.endswith("-sess"):
            parts.append(f'cnonce="{cnonce}"')
        if opaque:
            parts.append(f'opaque="{opaque}"')
        return "Digest " + ", ".join(parts)
    return ""


def _sdp_has_video(sdp: str) -> bool:
    return any(line.strip().lower().startswith("m=video")
               for line in (sdp or "").splitlines())


def _rtsp_request(sock: socket.socket, url: str, cseq: int,
                  extra: dict[str, str], timeout: float) -> tuple[
        str, dict, bytes]:
    lines = [f"DESCRIBE {url} RTSP/1.0", f"CSeq: {cseq}",
             "User-Agent: cctv-cv-discovery"]
    for key, value in extra.items():
        lines.append(f"{key}: {value}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    return _recv_rtsp(sock, timeout)


def _rtsp_describe(host: str, port: int, path: str, username: str,
                   password: str, timeout: float = RTSP_TIMEOUT) -> (
        tuple[str, str, str, str]):
    """Return (status_line, headers, sdp, error) for a DESCRIBE.

    The challenge and the authenticated request must travel over the same TCP
    connection: several camera brands bind the nonce to the connection and
    reject an otherwise valid digest when the socket is reopened.
    """
    url = _rtsp_url(host, port, path)
    extra = {"Accept": "application/sdp"}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            status, headers, body = _rtsp_request(sock, url, 1, extra, timeout)
            if not status:
                return "", {}, "", "connection failed"
            if (status.startswith("RTSP/1.0 401")
                    or status.startswith("RTSP/1.0 403")):
                if not username:
                    return status, headers, "", "authentication required"
                challenge = headers.get("www-authenticate", "")
                auth_header = _rtsp_auth_header(
                    challenge, "DESCRIBE", url, username, password)
                if auth_header:
                    extra["Authorization"] = auth_header
                    status, headers, body = _rtsp_request(
                        sock, url, 2, extra, timeout)
    except OSError:
        return "", {}, "", "connection failed"
    if not status:
        return "", {}, "", "connection failed"
    sdp = body.decode("utf-8", "replace")
    if not status.startswith("RTSP/1.0 2"):
        return status, headers, sdp, f"DESCRIBE failed ({status})"
    return status, headers, sdp, ""


def _rtsp_paths(saved_paths: list[str] | None = None) -> list[str]:
    saved = [path for path in (saved_paths or []) if path]
    return saved + [path for path in COMMON_RTSP_PATHS
                    if path not in saved]


def probe_rtsp(host: str, port: int, path: str | None = None,
               username: str = "", password: str = "",
               timeout: float = RTSP_TIMEOUT,
               saved_paths: list[str] | None = None,
               budget: AuthBudget | None = None) -> RtspResult:
    """Probe an RTSP endpoint and require a video track in the SDP.

    Known paths for saved cameras are tried first. Many cameras answer 401
    even for a path that does not exist, so a failed DESCRIBE is counted
    against the host's shared ``AuthBudget``; without that cap, walking every
    vendor path with a wrong password could lock the camera account.
    """
    result = RtspResult(username=username, password=password)
    paths = [path] if path else _rtsp_paths(saved_paths)
    if not username and not path:
        paths = paths[:MAX_ANON_RTSP_PATHS]
    last_error = ""
    for candidate in paths:
        if username and budget is not None and not budget.allow(host):
            result.auth_required = True
            result.error = ("Authentication attempts stopped to avoid locking "
                            "the camera account")
            return result
        status, headers, sdp, error = _rtsp_describe(
            host, port, candidate, username, password, timeout)
        if not status:
            last_error = error or last_error
            continue
        if (status.startswith("RTSP/1.0 401")
                or status.startswith("RTSP/1.0 403")):
            result.rtsp = True
            result.auth_required = True
            result.error = "Authentication required"
            if candidate and not result.path:
                result.path = candidate
            if not username:
                # A 401 already proves an RTSP service; opening every other
                # path without credentials only triples the camera's log
                # entries and can trigger its anti-bruteforce block.
                break
            result.auth_failed = True
            if budget is not None:
                budget.record_failure(host)
            # With credentials a 401 may only mean "wrong path", so keep
            # looking for a path that accepts the pair (the budget caps this).
            continue
        if status.startswith("RTSP/1.0 2") and _sdp_has_video(sdp):
            result.rtsp = True
            result.authenticated = True
            result.auth_failed = False
            result.error = ""
            result.video = True
            result.sdp = sdp
            result.server = headers.get("server", "")
            result.path = candidate
            result.stream_url = _rtsp_url(host, port, candidate)
            return result
        last_error = error or f"no video track ({status})"

    if result.rtsp:
        return result
    result.error = last_error or "not an RTSP video server"
    return result


def probe_rtsp_url(url: str, username: str = "", password: str = "",
                   timeout: float = RTSP_TIMEOUT) -> RtspResult:
    """Probe an exact RTSP URL, including its path and query string."""
    if not url:
        return RtspResult(error="no stream URL")
    parsed = urlsplit(url)
    if not parsed.hostname:
        return RtspResult(url=url, error="invalid stream URL")
    host = parsed.hostname
    port = parsed.port or 554
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    user = username or unquote(parsed.username or "")
    secret = password or unquote(parsed.password or "")
    result = probe_rtsp(host, port, path, user, secret, timeout)
    result.url = url
    return result


# ---------------------------------------------------------------------------
# Verification and device construction
# ---------------------------------------------------------------------------

def _url_with_credentials(url: str, username: str,
                           password: str) -> str:
    if not username or not url:
        return url
    parsed = urlsplit(url)
    if parsed.username or not parsed.hostname:
        return url
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}"
    netloc = f"{userinfo}@{host}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path,
                       parsed.query, parsed.fragment))


def _without_userinfo(url: str) -> str:
    if not url:
        return url
    parsed = urlsplit(url)
    if not parsed.hostname:
        return url
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path,
                       parsed.query, parsed.fragment))


def _credential_pairs(host: str, request: DiscoveryRequest,
                      saved_credentials: dict[str, tuple[str, str]]) -> list[
        tuple[str, str]]:
    pairs: list[tuple[str, str]] = []

    def add(user: str, password: str):
        pair = (user or "", password or "")
        if pair not in pairs:
            pairs.append(pair)

    # Prefer the pair the operator just supplied over stale saved credentials.
    add(request.username, request.password)
    for credential in request.credentials or []:
        add(credential.username, credential.password)
    if host in saved_credentials:
        add(*saved_credentials[host])
    env_user = os.getenv("ONVIF_USER", "")
    env_pass = os.getenv("ONVIF_PASS", "")
    if env_user or env_pass:
        add(env_user, env_pass)
    if not pairs:
        add("", "")
    return pairs[:MAX_CREDENTIALS + 1]


def _channel_checks(profiles: list[dict], username: str,
                    password: str) -> list[RtspResult]:
    """DESCRIBE every profile in parallel (no video decoding yet)."""
    if not profiles:
        return []
    results: list[RtspResult] = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(profiles))) as pool:
        futures = []
        for profile in profiles:
            raw = profile.get("rtsp_url") or ""
            if not raw:
                futures.append(None)
                continue
            url = _url_with_credentials(raw, username, password)
            futures.append(pool.submit(
                probe_rtsp_url, url, username, password))
        for future in futures:
            if future is None:
                results.append(RtspResult(error="no stream URI"))
                continue
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(RtspResult(error=str(exc)))
    return results


def _onvif_device(endpoint: Endpoint, result: OnvifResult,
                  saved: bool, allow_credentials: bool = True) -> DiscoveredDevice:
    host = endpoint.host
    port = endpoint.port
    name = endpoint.name or " ".join(
        part for part in (result.manufacturer, result.model) if part).strip()
    if not name:
        name = f"ONVIF device {host}"

    username = result.username
    password = result.password
    profiles = result.profiles
    if not allow_credentials:
        # Public ONVIF profiles occasionally contain RTSP userinfo. Discovery
        # must never send that login, even if the device volunteered it.
        profiles = [{**profile,
                     "rtsp_url": _without_userinfo(profile.get("rtsp_url") or "")}
                    for profile in profiles]
        username = password = ""
    checks = _channel_checks(profiles, username, password)
    channels: list[DiscoveredChannel] = []
    for profile, check in zip(profiles, checks):
        raw = profile.get("rtsp_url") or ""
        url = _url_with_credentials(raw, username, password) if raw else ""
        accessible = bool(url and check.video)
        channels.append(DiscoveredChannel(
            token=profile["token"],
            name=profile["name"],
            encoding=profile.get("encoding", ""),
            width=profile.get("width"),
            height=profile.get("height"),
            rtsp_url=url or None,
            accessible=accessible,
            error=(check.error if not accessible else None)
                  or profile.get("error"),
        ))

    # Prove the device delivers video by decoding one real frame. Prefer the
    # channel with the largest picture, which is normally the main stream.
    def _pixels(channel: DiscoveredChannel) -> int:
        return (channel.width or 0) * (channel.height or 0)

    candidates = [channel for channel in channels
                  if channel.rtsp_url and channel.accessible]
    if not candidates:
        candidates = [channel for channel in channels if channel.rtsp_url]
    candidates.sort(key=_pixels, reverse=True)
    verified = False
    for channel in candidates[:1]:
        checked = check_stream(channel.rtsp_url)
        if checked.get("accessible"):
            channel.accessible = True
            channel.error = None
            if checked.get("width") and checked.get("height"):
                channel.width = checked["width"]
                channel.height = checked["height"]
            verified = True

    auth_required = result.auth_required and not result.authenticated
    if verified:
        status = "ready"
    elif auth_required:
        status = "credentials"
    elif channels and any(channel.accessible for channel in channels):
        status = "no_video"
    else:
        status = "no_video" if result.profiles else (
            "credentials" if auth_required else "no_video")

    detail = result.error
    if status == "credentials" and not detail:
        detail = "Authentication required"
    elif status == "no_video" and not detail:
        detail = ("Streams answered but no frame could be decoded"
                  if any(channel.accessible for channel in channels)
                  else "No decodable video stream")

    kind = _onvif_kind(result.model, endpoint.kind_hint)

    primary = next((channel.rtsp_url for channel in channels
                    if channel.accessible), None)
    if primary is None:
        primary = next((channel.rtsp_url for channel in channels), None)

    return DiscoveredDevice(
        id=f"{host}:{port}",
        name=name,
        address=result.url or endpoint.url,
        rtsp_url=primary,
        source_type="onvif",
        kind=kind,
        manufacturer=result.manufacturer,
        model=result.model,
        firmware=result.firmware,
        host=host,
        port=port,
        verified=verified,
        status=status,
        detail=detail,
        channels=channels,
        saved=saved,
    )


def _onvif_kind(model: str, hint: str = "") -> str:
    """Classify only when the device identifies its hardware type.

    Four profiles can be four encodings of a *single* camera, not an NVR;
    ONVIF's NetworkVideoTransmitter type also covers NVRs. Neither is proof.
    """
    if hint == "nvr" or re.search(
            r"(?<![a-z])(?:nvr|dvr|xvr)(?![a-z])|"
            r"(?<![a-z])recorder(?![a-z])",
            model, re.IGNORECASE):
        return "nvr"
    if re.search(
            r"(?<![a-z0-9])(?:camera|ipcam|ipc)(?![a-z0-9])",
            model, re.IGNORECASE):
        return "camera"
    return "onvif"


def _rtsp_device(endpoint: Endpoint, result: RtspResult,
                 username: str, password: str,
                 saved: bool) -> DiscoveredDevice:
    url = (result.stream_url or
           (_rtsp_url(endpoint.host, endpoint.port, result.path)
            if result.path else endpoint.url))
    authenticated_url = _url_with_credentials(url, username, password)
    checked = (check_stream(authenticated_url)
               if result.video and result.authenticated else
               {"accessible": False, "error": result.error})
    verified = bool(result.video and result.authenticated
                    and checked.get("accessible"))
    status = "ready" if verified else (
        "credentials" if result.auth_required and not result.authenticated
        else "no_video")
    detail = result.error or (
        "RTSP video source" if verified else "No decodable video stream")
    # An authenticated RTSP endpoint might be a generic media server. Never
    # claim its physical device type just because it answers on port 554.
    name = endpoint.name or f"RTSP source {endpoint.host}"
    return DiscoveredDevice(
        id=f"{endpoint.host}:{endpoint.port}",
        name=name,
        address=url,
        rtsp_url=authenticated_url if result.authenticated else None,
        source_type="manual-rtsp",
        kind="rtsp",
        host=endpoint.host,
        port=endpoint.port,
        verified=verified,
        status=status,
        detail=detail,
        saved=saved,
    )


def _verify_endpoint(endpoint: Endpoint, request: DiscoveryRequest,
                     saved_credentials: dict[str, tuple[str, str]],
                     saved: bool,
                     evidence: bool = False,
                     budget: AuthBudget | None = None,
                     saved_paths: list[str] | None = None,
                     onvif_budget: AuthBudget | None = None,
                     authenticate: bool = True
                     ) -> DiscoveredDevice | None:
    pairs = (_credential_pairs(endpoint.host, request, saved_credentials)
             if authenticate else [])
    host = endpoint.host
    auth_budget = onvif_budget if endpoint.scheme in ("http", "https") else budget

    def allowed() -> bool:
        return auth_budget is None or auth_budget.allow(host)

    def failed() -> None:
        if auth_budget is not None:
            auth_budget.record_failure(host)

    if endpoint.scheme in ("http", "https"):
        url = (endpoint.url if authenticate else
               _without_userinfo(endpoint.url))
        first = probe_onvif(url, "", "", trust_auth=evidence)
        if not first.onvif:
            # A device may use the opposite scheme on a non-standard port.
            alt_scheme = "http" if endpoint.scheme == "https" else "https"
            alt = Endpoint(host=endpoint.host, port=endpoint.port,
                           scheme=alt_scheme, source=endpoint.source,
                           name=endpoint.name)
            candidate = probe_onvif(alt.url, "", "", trust_auth=evidence)
            if not candidate.onvif:
                return None
            first = candidate
        if first.authenticated or not first.auth_required or not authenticate:
            return _onvif_device(endpoint, first, saved,
                                 allow_credentials=authenticate)
        for username, password in pairs:
            if not (username or password) or not allowed():
                continue
            candidate = probe_onvif(
                first.url, username, password, trust_auth=True)
            if candidate.auth_failed:
                failed()
            if candidate.onvif and (candidate.authenticated
                                    or candidate.profiles):
                return _onvif_device(endpoint, candidate, saved)
        return _onvif_device(endpoint, first, saved)

    first = probe_rtsp(endpoint.host, endpoint.port, endpoint.path, "", "")
    if not first.rtsp:
        return None
    if first.video and first.authenticated:
        return _rtsp_device(endpoint, first, "", "", saved)
    if first.auth_required and authenticate:
        hints = [path for path in (saved_paths or []) if path]
        # If anonymous DESCRIBE was challenged on a candidate path, try each
        # supplied login there before spending attempts on other vendor paths.
        # Otherwise a wrong first login exhausts the whole budget before a
        # correct second camera login is even tried.
        if not hints and first.path:
            hints = [first.path]
        rejected_pairs: list[tuple[str, str]] = []
        for username, password in pairs:
            if not (username or password) or not allowed():
                continue
            hint_rejected = False
            for hint in hints:
                candidate = probe_rtsp(
                    endpoint.host, endpoint.port, hint, username, password,
                    budget=budget)
                if candidate.rtsp and candidate.video:
                    return _rtsp_device(
                        endpoint, candidate, username, password, saved)
                if candidate.auth_failed:
                    # A known-good path rejected this pair, so the pair is
                    # wrong; do not spend the rest of the budget elsewhere.
                    hint_rejected = True
                    break
                if candidate.rtsp and not candidate.auth_required:
                    # Credentials were accepted but this path has no video.
                    return _rtsp_device(
                        endpoint, candidate, username, password, saved)
            if hint_rejected:
                if not saved_paths:
                    rejected_pairs.append((username, password))
                continue
            # No saved path answered: try the common vendor paths. The budget
            # caps the number of rejected attempts.
            candidate = probe_rtsp(
                endpoint.host, endpoint.port, None, username, password,
                budget=budget)
            if candidate.rtsp and candidate.video:
                return _rtsp_device(
                    endpoint, candidate, username, password, saved)
            if candidate.rtsp and not candidate.auth_required:
                return _rtsp_device(
                    endpoint, candidate, username, password, saved)
            if not allowed():
                break
        # A challenged vendor path may not exist. Once every supplied login
        # had its fair turn on that path, use any remaining attempts on other
        # common paths instead of declaring a correct login invalid.
        if rejected_pairs:
            for candidate_path in COMMON_RTSP_PATHS:
                if not allowed():
                    break
                if candidate_path == first.path:
                    continue
                username, password = rejected_pairs[0]
                candidate = probe_rtsp(
                    endpoint.host, endpoint.port, candidate_path,
                    username, password, budget=budget)
                if candidate.rtsp and candidate.video:
                    return _rtsp_device(
                        endpoint, candidate, username, password, saved)
    return _rtsp_device(endpoint, first, "", "", saved)


def _endpoints_from_scan(opened: dict[str, list[int]]) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for host, ports in opened.items():
        for port in ports:
            if port in RTSP_PORTS:
                endpoints.append(Endpoint(host=host, port=port,
                                          scheme="rtsp", source="scan"))
            elif port in HTTP_PORTS:
                scheme = "https" if port in (443, 8443, 9443) else "http"
                endpoints.append(Endpoint(host=host, port=port,
                                          scheme=scheme, source="scan"))
            else:
                # Ports found by a deep scan are fingerprinted as both.
                endpoints.append(Endpoint(host=host, port=port,
                                          scheme="http", source="scan"))
                endpoints.append(Endpoint(host=host, port=port,
                                          scheme="rtsp", source="scan"))
    return endpoints


def _dedupe_endpoints(endpoints: list[Endpoint]) -> list[Endpoint]:
    unique: dict[tuple[str, int, str], Endpoint] = {}
    for endpoint in endpoints:
        key = (endpoint.host, endpoint.port, endpoint.scheme)
        current = unique.get(key)
        if current is None:
            unique[key] = endpoint
        else:
            if not current.name and endpoint.name:
                current.name = endpoint.name
            if not current.xaddr and endpoint.xaddr:
                current.xaddr = endpoint.xaddr
            if not current.path and endpoint.path:
                current.path = endpoint.path
            if not current.kind_hint and endpoint.kind_hint:
                current.kind_hint = endpoint.kind_hint
    return list(unique.values())


# ---------------------------------------------------------------------------
# Scope and public API
# ---------------------------------------------------------------------------

def _expand_target(value: str) -> list[str]:
    value = (value or "").strip()
    if not value:
        return []
    if "://" in value:
        parsed = urlsplit(value)
        value = parsed.hostname or ""
        if not value:
            return []
    try:
        network = ipaddress.ip_network(value, strict=False)
        if network.version != 4:
            return []
        if network.num_addresses <= 1:
            return [str(network.network_address)]
        return [str(host) for host in network.hosts()]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(value, None, socket.AF_INET)
        return sorted({info[4][0] for info in infos})
    except socket.gaierror:
        return []


def _network_hosts(network: ipaddress.IPv4Network,
                   limit: int | None = None,
                   anchor: str | None = None) -> list[str]:
    """Hosts of a network, optionally a window of ``limit`` around ``anchor``."""
    try:
        hosts = list(network.hosts())
    except ValueError:
        return []
    if limit is None or len(hosts) <= limit:
        return [str(host) for host in hosts]
    start = 0
    if anchor:
        try:
            target = ipaddress.ip_address(anchor)
            start = hosts.index(target)
        except (ValueError, IndexError):
            start = 0
    start = max(0, min(start - limit // 2, len(hosts) - limit))
    return [str(host) for host in hosts[start:start + limit]]


def _parse_subnets(subnets: list[str],
                   warnings_list: list[str]) -> list[ipaddress.IPv4Network]:
    parsed: list[ipaddress.IPv4Network] = []
    for subnet in subnets or []:
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as exc:
            warnings_list.append(f"Ignored subnet '{subnet}': {exc}")
            continue
        if network.version != 4:
            warnings_list.append(f"Ignored IPv6 subnet '{subnet}'.")
            continue
        parsed.append(
            _cap_network(network, str(next(network.hosts(), network.network_address))))
    return parsed


def _build_scope(request: DiscoveryRequest, warnings_list: list[str]) -> tuple[
        list[ipaddress.IPv4Network], list[str]]:
    local_addresses: set[str] = set()
    direct_networks: list[ipaddress.IPv4Network] = []
    anchors: dict[ipaddress.IPv4Network, str] = {}

    for address, prefix in local_interfaces():
        local_addresses.add(address)
        try:
            network = ipaddress.ip_network(
                f"{address}/{prefix}", strict=False)
        except ValueError:
            continue
        network = _cap_network(network, address)
        if _is_ignored_network(network) or network.prefixlen == 0:
            continue
        if network not in direct_networks:
            direct_networks.append(network)
        anchors.setdefault(network, address)

    broadcasts = {str(network.broadcast_address)
                  for network in direct_networks}

    routed_networks: list[ipaddress.IPv4Network] = []
    for raw in _route_networks():
        if _is_ignored_network(raw) or raw.prefixlen == 0:
            continue
        # /32 routes for a local address or an interface broadcast are not
        # useful scan targets.
        if (raw.prefixlen == 32
                and str(raw.network_address) in local_addresses | broadcasts):
            continue
        network = _cap_network(raw)
        if _is_ignored_network(network) or network.prefixlen == 0:
            continue
        if any(network == direct or network.subnet_of(direct)
               for direct in direct_networks):
            continue
        if network not in routed_networks:
            routed_networks.append(network)

    requested_networks = _parse_subnets(request.subnets or [], warnings_list)

    all_networks: list[ipaddress.IPv4Network] = []
    for network in direct_networks + routed_networks + requested_networks:
        if network not in all_networks:
            all_networks.append(network)

    # On a directly connected network every reachable IP must answer ARP.
    # Sending one UDP datagram per address populates the OS neighbour cache in
    # about a second, so only live hosts need a TCP port scan. If the cache
    # cannot be read at all, fall back to scanning addresses directly.
    direct_hosts: set[str] = set()
    for network in direct_networks:
        network_hosts = set(_network_hosts(network)) - local_addresses
        if not network_hosts:
            continue
        live = _arp_sweep(sorted(network_hosts))
        if live:
            direct_hosts |= network_hosts & live
        else:
            warnings_list.append(
                f"Could not read the ARP/neighbour table for {network}; "
                "probing addresses directly.")
            if len(network_hosts) <= MAX_FALLBACK_HOSTS:
                direct_hosts |= network_hosts
            else:
                direct_hosts.update(_network_hosts(
                    network, MAX_FALLBACK_HOSTS, anchors.get(network)))

    routed_hosts: set[str] = set()
    for network in routed_networks + requested_networks:
        if any(network == direct or network.subnet_of(direct)
               for direct in direct_networks):
            continue  # already covered exhaustively above
        count = 0
        for host in _network_hosts(network):
            if host in local_addresses:
                continue
            routed_hosts.add(host)
            count += 1
            if count >= MAX_ROUTED_HOSTS:
                warnings_list.append(
                    f"Routed/requested network {network} has more than "
                    f"{MAX_ROUTED_HOSTS} addresses; only the first "
                    f"{MAX_ROUTED_HOSTS} were probed.")
                break

    direct_hosts -= local_addresses

    explicit: set[str] = set()
    for value in request.targets or []:
        resolved = _expand_target(value)
        if not resolved:
            warnings_list.append(f"Could not resolve target '{value}'.")
        explicit.update(resolved)
    if request.include_saved:
        for saved in db.list_cameras():
            for value in (saved.address, saved.rtsp_url):
                parsed = urlsplit(value or "")
                if parsed.hostname:
                    explicit.add(parsed.hostname)
    explicit -= local_addresses

    hosts = sorted(direct_hosts | routed_hosts | explicit, key=_host_key)
    if len(hosts) > MAX_TOTAL_HOSTS:
        warnings_list.append(
            f"Found {len(hosts)} hosts; limiting the scan to "
            f"{MAX_TOTAL_HOSTS}. Add a subnet or target to focus the scan.")
        hosts = hosts[:MAX_TOTAL_HOSTS]

    return all_networks, hosts


def _host_key(host: str):
    try:
        return tuple(int(part) for part in host.split("."))
    except ValueError:
        return (999, 999, 999, 999)


def _saved_state():
    addresses = set()
    hosts = set()
    credentials: dict[str, tuple[str, str]] = {}
    paths: dict[str, list[str]] = {}
    for saved in db.list_cameras():
        addresses.add(saved.address)
        if saved.rtsp_url:
            addresses.add(saved.rtsp_url)
            parsed = urlsplit(saved.rtsp_url)
        else:
            parsed = urlsplit(saved.address or "")
        if parsed.hostname:
            hosts.add(parsed.hostname)
            pair = ((saved.onvif_user, saved.onvif_pass)
                    if saved.onvif_user or saved.onvif_pass else
                    (unquote(parsed.username or ""),
                     unquote(parsed.password or "")))
            # A saved entry without credentials must not mask a later entry
            # on the same host whose RTSP URL has an authenticated userinfo.
            if pair != ("", "") or parsed.hostname not in credentials:
                credentials[parsed.hostname] = pair
            if parsed.path:
                stream_path = parsed.path
                if parsed.query:
                    stream_path += "?" + parsed.query
                bucket = paths.setdefault(parsed.hostname, [])
                if stream_path not in bucket:
                    bucket.append(stream_path)
    return addresses, hosts, credentials, paths


def discover_devices(request: DiscoveryRequest | None = None) -> DiscoveryReport:
    """List camera/RTSP endpoints without using or testing any credentials."""
    request = request or DiscoveryRequest()
    started = time.perf_counter()
    warnings_list: list[str] = []

    networks, hosts = _build_scope(request, warnings_list)
    print(f"[discover] scanning {', '.join(str(n) for n in networks)} "
          f"({len(hosts)} hosts)")

    # Protocol discovery runs while the port sweep is in progress.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        ws_future = pool.submit(_ws_discover, WS_TIMEOUT, hosts)
        ssdp_future = pool.submit(_ssdp_discover, SSDP_TIMEOUT)
        opened = _scan_hosts(hosts, request.deep, warnings_list)
        try:
            ws_endpoints = ws_future.result()
        except Exception:
            ws_endpoints = []
        try:
            ssdp_endpoints = ssdp_future.result()
        except Exception:
            ssdp_endpoints = []

    endpoints = _endpoints_from_scan(opened)
    endpoints.extend(ws_endpoints)
    endpoints.extend(ssdp_endpoints)
    saved_addresses, saved_hosts, saved_credentials, saved_paths = _saved_state()
    if request.include_saved:
        # A saved stream is a useful exact-port hint even if a transient
        # connect timeout made the generic port sweep miss that host.
        for saved in db.list_cameras():
            url = saved.rtsp_url or saved.address
            parsed = urlsplit(url or "")
            if not parsed.hostname or parsed.scheme.lower() != "rtsp":
                continue
            endpoints.append(Endpoint(
                host=parsed.hostname, port=parsed.port or 554,
                scheme="rtsp", source="saved"))
    endpoints = _dedupe_endpoints(endpoints)
    budget = AuthBudget()
    onvif_budget = AuthBudget(MAX_ONVIF_AUTH_FAILURES)
    ws_hosts = {endpoint.host for endpoint in ws_endpoints}
    rtsp_hosts = {host for host, ports in opened.items()
                  if any(port in RTSP_PORTS for port in ports)}
    # Evidence turns a bare 401 into a camera instead of an arbitrary web
    # server: WS-Discovery advertises a network video transmitter, an open
    # RTSP port is a video service, and a device the user already saved is
    # known to be video equipment.
    evidenced_hosts = ws_hosts | rtsp_hosts | saved_hosts

    by_host: dict[str, list[Endpoint]] = {}
    for endpoint in endpoints:
        by_host.setdefault(endpoint.host, []).append(endpoint)

    def verify_host(host: str) -> tuple[str, DiscoveredDevice | None]:
        """Probe every endpoint of one host, cheapest evidence first."""
        best: DiscoveredDevice | None = None
        identified: DiscoveredDevice | None = None
        ordered = sorted(by_host.get(host, []),
                         key=lambda ep: 0 if ep.scheme == "rtsp" else 1)
        for endpoint in ordered:
            try:
                device = _verify_endpoint(
                    endpoint, request, saved_credentials,
                    host in saved_hosts, host in evidenced_hosts,
                    budget, [], onvif_budget,
                    authenticate=False)
            except Exception:
                continue
            if device is None:
                continue
            if device.source_type == "onvif" and device.kind in ("nvr", "camera"):
                identified = device
            if best is None or _device_rank(device) > _device_rank(best):
                best = device
            # A complete ONVIF answer already lists every channel.
            if (device.source_type == "onvif" and device.verified
                    and device.channels):
                break
        if (best is not None and best.kind == "rtsp" and identified is not None):
            # The RTSP stream may be the only decodable endpoint while ONVIF
            # still supplies a trustworthy model or explicit recorder scope.
            best.kind = identified.kind
            best.manufacturer = identified.manufacturer
            best.model = identified.model
            best.firmware = identified.firmware
            if identified.model:
                best.name = identified.name
        return host, best

    verified: dict[str, DiscoveredDevice] = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(VALIDATION_WORKERS,
                            max(1, len(by_host)))) as pool:
        futures = [pool.submit(verify_host, host) for host in by_host]
        for future in concurrent.futures.as_completed(futures):
            try:
                host, device = future.result()
            except Exception:
                continue
            if device is not None:
                verified[host] = device

    devices = []
    for host in sorted(verified, key=_host_key):
        device = verified[host]
        address = device.address
        is_saved = (host in saved_hosts or address in saved_addresses
                    or (device.rtsp_url and device.rtsp_url in saved_addresses))
        device.saved = is_saved
        if is_saved and not request.include_saved:
            continue
        devices.append(device)

    if not endpoints:
        warnings_list.append(
            "No ONVIF or RTSP service answered. Check that the devices are "
            "powered, on the same network, and not isolated by a firewall.")

    duration_ms = int((time.perf_counter() - started) * 1000)
    print(f"[discover] {len(devices)} video endpoint(s) found")
    return DiscoveryReport(
        devices=devices,
        networks=[str(network) for network in networks],
        scanned_hosts=len(hosts),
        duration_ms=duration_ms,
        warnings=warnings_list,
    )


def connect_discovered_device(request: DiscoveryConnectRequest) -> DiscoveredDevice:
    """Verify exactly the device the operator chose with their entered login."""
    parsed = urlsplit(request.address)
    if parsed.scheme not in ("rtsp", "http", "https") or not parsed.hostname:
        raise ValueError("Select a discovered ONVIF or RTSP device")
    if parsed.username or parsed.password:
        raise ValueError("Device address must not contain credentials")
    try:
        port = parsed.port or (554 if parsed.scheme == "rtsp" else
                               443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Invalid device port") from exc
    path = (parsed.path or "/") if parsed.scheme == "rtsp" else parsed.path
    if parsed.query and parsed.scheme == "rtsp":
        path += "?" + parsed.query
    endpoint = Endpoint(host=parsed.hostname, port=port,
                        scheme=parsed.scheme, path=path,
                        xaddr=request.address if parsed.scheme != "rtsp" else None)
    _, _, _, saved_paths = _saved_state()
    device = _verify_endpoint(
        endpoint, DiscoveryRequest(username=request.username,
                                   password=request.password), {}, False,
        evidence=True, budget=AuthBudget(),
        saved_paths=saved_paths.get(parsed.hostname, []),
        onvif_budget=AuthBudget(MAX_ONVIF_AUTH_FAILURES))
    if device is None:
        raise ValueError("This endpoint did not answer as ONVIF or RTSP")
    return device


def _device_rank(device: DiscoveredDevice) -> tuple:
    return (
        1 if device.verified else 0,
        1 if device.status == "ready" else 0,
        1 if device.source_type == "onvif" else 0,
        1 if device.channels else 0,
        len(device.channels),
    )


# ---------------------------------------------------------------------------
# ONVIF helpers used by the camera settings screen
# ---------------------------------------------------------------------------

def fetch_stream_profiles(address: str, username: str,
                          password: str) -> list[dict]:
    """All media profiles (NVR channels) with their RTSP URIs.

    Requires credentials supplied by the user; never called by discovery.
    """
    result = probe_onvif(address, username or "", password or "",
                         trust_auth=True)
    if not result.onvif:
        raise RuntimeError("The device did not answer as an ONVIF camera")
    if result.auth_required and not result.authenticated:
        raise RuntimeError("ONVIF authentication failed — check the username and password")
    if not result.profiles:
        raise RuntimeError(result.error or "Device reports no media profiles")

    profiles = []
    for profile in result.profiles:
        item = {
            "token": profile["token"],
            "name": profile["name"],
            "encoding": profile.get("encoding", ""),
            "width": profile.get("width"),
            "height": profile.get("height"),
            "rtsp_url": profile.get("rtsp_url"),
        }
        if item["rtsp_url"] and username:
            item["rtsp_url"] = _url_with_credentials(
                item["rtsp_url"], username, password)
        if profile.get("error"):
            item["error"] = profile["error"]
        profiles.append(item)
    return profiles


def fetch_rtsp_url(address: str, username: str, password: str) -> str:
    """First RTSP URI reported by the device (kept for the Run shortcut)."""
    for profile in fetch_stream_profiles(address, username, password):
        if profile.get("rtsp_url"):
            return profile["rtsp_url"]
    raise RuntimeError("Device reports no RTSP stream URI")


def check_stream(rtsp_url: str, timeout: float = STREAM_TIMEOUT) -> dict:
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
        height, width = frame.shape[:2]
        return {"accessible": True, "width": width, "height": height}
    except Exception as exc:
        return {"accessible": False, "error": str(exc)}
    finally:
        cap.release()
