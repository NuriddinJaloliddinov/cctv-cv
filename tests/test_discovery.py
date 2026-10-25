"""Discovery tests: false-positive filtering, credentials and auth safety.

No test opens a real network connection. Every probe is replaced with a stub
so the assertions describe the discovery rules, not the test machine's LAN.
"""
import base64
import hashlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import discovery as d
from backend.models import (
    DiscoveredDevice, DiscoveryCredentials, DiscoveryRequest,
    DiscoveryConnectRequest,
    DiscoveryReport,
)

WS_SOAP = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <s:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>onvif://www.onvif.org/name/Front%20Door
          onvif://www.onvif.org/hardware/DS-2CD</d:Scopes>
        <d:XAddrs>http://192.168.1.50/onvif/device_service</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </s:Body>
</s:Envelope>
"""

WS_PRINTER = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <s:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <d:Types>wsdp:Device</d:Types>
        <d:Scopes>urn:printer:laserjet</d:Scopes>
        <d:XAddrs>http://192.168.1.60:80/wsd</d:XAddrs>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </s:Body>
</s:Envelope>
"""


class WsDiscoveryTests(unittest.TestCase):
    def test_recorder_scope_is_a_classification_hint(self):
        packet = WS_SOAP.replace(
            "onvif://www.onvif.org/hardware/DS-2CD",
            "onvif://www.onvif.org/hardware/DS-2CD "
            "onvif://www.onvif.org/type/Network_Video_Recorder")
        endpoints = d._parse_ws_response(packet.encode(), "192.168.1.50")
        self.assertEqual(endpoints[0].kind_hint, "nvr")

    def test_camera_advertisement_is_parsed(self):
        endpoints = d._parse_ws_response(WS_SOAP.encode(), "192.168.1.50")
        self.assertEqual(len(endpoints), 1)
        endpoint = endpoints[0]
        self.assertEqual(endpoint.host, "192.168.1.50")
        self.assertEqual(endpoint.port, 80)
        self.assertEqual(endpoint.path, "/onvif/device_service")
        self.assertEqual(endpoint.name, "Front Door")

    def test_printer_advertisement_is_ignored(self):
        self.assertEqual(d._parse_ws_response(WS_PRINTER.encode(), "192.168.1.60"), [])

    def test_malformed_xml_is_ignored(self):
        self.assertEqual(d._parse_ws_response(b"<not-soap", "10.0.0.1"), [])


class OnvifProofTests(unittest.TestCase):
    def test_requires_onvif_namespace(self):
        from xml.etree import ElementTree as ET
        onvif = ET.fromstring(
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
            '<e:Body><tds:GetDeviceInformationResponse/></e:Body></e:Envelope>')
        other = ET.fromstring(
            '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:x="urn:printer">'
            '<e:Body><x:GetDeviceInformationResponse/></e:Body></e:Envelope>')
        self.assertTrue(
            d._has_onvif_response(onvif, "GetDeviceInformationResponse"))
        self.assertFalse(
            d._has_onvif_response(other, "GetDeviceInformationResponse"))

    def test_bare_401_is_not_a_camera_without_evidence(self):
        unauthorised = d.SoapResponse(status=401)
        with patch.object(d, "_soap_call", return_value=unauthorised):
            result = d._try_onvif_url("http://10.0.0.9/onvif/device_service")
        self.assertFalse(result.onvif)
        self.assertTrue(result.auth_required)

    def test_bare_401_is_a_camera_when_wsd_proved_it(self):
        unauthorised = d.SoapResponse(status=401)
        with patch.object(d, "_soap_call", return_value=unauthorised):
            result = d._try_onvif_url(
                "http://10.0.0.9/onvif/device_service", trust_auth=True)
        self.assertTrue(result.onvif)
        self.assertTrue(result.auth_required)
        self.assertEqual(result.error, "Authentication required")

    def test_plain_web_page_is_not_a_camera(self):
        html = d.SoapResponse(status=200, root=None, text="<html>router</html>")
        with patch.object(d, "_soap_call", return_value=html) as call:
            result = d._try_onvif_url("http://10.0.0.1/onvif/device_service")
        self.assertFalse(result.onvif)
        self.assertEqual(call.call_count, 1)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.request = DiscoveryRequest(
            username="operator", password="secret",
            credentials=[DiscoveryCredentials(username="admin", password="pw")],
        )

    def test_request_pair_is_tried_first_and_saved_pair_is_deduplicated(self):
        saved = {"10.0.0.5": ("admin", "pw")}
        with patch.dict(os.environ, {"ONVIF_USER": "", "ONVIF_PASS": ""}):
            pairs = d._credential_pairs("10.0.0.5", self.request, saved)
        self.assertEqual(pairs[0], ("operator", "secret"))
        self.assertEqual(pairs.count(("admin", "pw")), 1)
        self.assertIn(("operator", "secret"), pairs)

    def test_credentials_are_capped(self):
        request = DiscoveryRequest(
            username="u0", password="p0",
            credentials=[
                DiscoveryCredentials(username=f"u{i}", password=f"p{i}")
                for i in range(1, 10)
            ],
        )
        with patch.dict(os.environ, {"ONVIF_USER": "", "ONVIF_PASS": ""}):
            pairs = d._credential_pairs("10.0.0.5", request, {})
        self.assertLessEqual(len(pairs), d.MAX_CREDENTIALS + 1)

    def test_url_credentials_are_encoded_once(self):
        url = d._url_with_credentials(
            "rtsp://10.0.0.5:554/live", "ad min", "p@ss:1")
        self.assertEqual(
            url, "rtsp://ad%20min:p%40ss%3A1@10.0.0.5:554/live")
        again = d._url_with_credentials(url, "other", "pair")
        self.assertEqual(again, url)

    def test_auth_budget_stops_after_limit(self):
        budget = d.AuthBudget(limit=2)
        self.assertTrue(budget.allow("10.0.0.5"))
        budget.record_failure("10.0.0.5")
        self.assertTrue(budget.allow("10.0.0.5"))
        budget.record_failure("10.0.0.5")
        self.assertFalse(budget.allow("10.0.0.5"))
        self.assertTrue(budget.allow("10.0.0.6"))


class RtspTests(unittest.TestCase):
    def test_saved_paths_come_first_without_duplicates(self):
        paths = d._rtsp_paths(["/Streaming/Channels/101", "/custom"])
        self.assertEqual(paths[:2], ["/Streaming/Channels/101", "/custom"])
        self.assertEqual(len(paths), len(set(paths)))

    def test_sdp_requires_a_video_track(self):
        self.assertTrue(d._sdp_has_video(
            "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"))
        self.assertFalse(d._sdp_has_video(
            "v=0\r\nm=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"))

    def test_digest_header_matches_rfc2617_math(self):
        challenge = ('Digest realm="test", nonce="abc123", algorithm=MD5')
        header = d._rtsp_auth_header(
            challenge, "DESCRIBE", "/live", "admin", "secret")

        def md5(value):
            return hashlib.md5(value.encode()).hexdigest()

        expected = md5(
            f"{md5('admin:test:secret')}:abc123:{md5('DESCRIBE:/live')}")
        self.assertIn(f'response="{expected}"', header)
        self.assertIn('uri="/live"', header)

    def test_digest_md5_omits_the_default_algorithm(self):
        # A camera family answers 401 to roughly half of the otherwise valid
        # digests that carry "algorithm=MD5"; MD5 is the default, so omit it.
        header = d._rtsp_auth_header(
            'Digest realm="cam", nonce="n1", algorithm=MD5',
            "DESCRIBE", "/live", "admin", "secret")
        self.assertNotIn("algorithm", header)
        header = d._rtsp_auth_header(
            'Digest realm="cam", nonce="n1"',
            "DESCRIBE", "/live", "admin", "secret")
        self.assertNotIn("algorithm", header)
        header = d._rtsp_auth_header(
            'Digest realm="cam", nonce="n1", algorithm=SHA-256',
            "DESCRIBE", "/live", "admin", "secret")
        self.assertIn("algorithm=SHA-256", header)
        ha1 = hashlib.sha256(b"admin:cam:secret").hexdigest()
        ha2 = hashlib.sha256(b"DESCRIBE:/live").hexdigest()
        expected = hashlib.sha256(f"{ha1}:n1:{ha2}".encode()).hexdigest()
        self.assertIn(f'response="{expected}"', header)
        self.assertEqual(d._rtsp_auth_header(
            'Digest realm="cam", nonce="n1", algorithm=UNSUPPORTED',
            "DESCRIBE", "/live", "admin", "secret"), "")
        header = d._rtsp_auth_header(
            'Digest realm="cam", nonce="n1", qop="auth-int,auth"',
            "DESCRIBE", "/live", "admin", "secret")
        self.assertIn("qop=auth,", header)

    def test_basic_header_is_base64(self):
        header = d._rtsp_auth_header(
            "Basic realm=x", "DESCRIBE", "/live", "admin", "secret")
        token = base64.b64encode(b"admin:secret").decode()
        self.assertEqual(header, f"Basic {token}")

    def test_public_video_stream_is_accepted(self):
        def describe(host, port, path, user, password, timeout=d.RTSP_TIMEOUT):
            if path == "/live":
                return ("RTSP/1.0 200 OK", {"server": "cam"},
                        "v=0\r\nm=video 0 RTP/AVP 96\r\n", "")
            return ("RTSP/1.0 404 Not Found", {}, "", "missing")

        with patch.object(d, "_rtsp_describe", side_effect=describe):
            result = d.probe_rtsp("10.0.0.7", 554)
        self.assertTrue(result.rtsp)
        self.assertTrue(result.video)
        self.assertEqual(result.path, "/live")

    def test_auth_attempts_are_limited_per_host(self):
        budget = d.AuthBudget(limit=3)
        with patch.object(
                d, "_rtsp_describe",
                return_value=("RTSP/1.0 401 Unauthorized", {}, "",
                              "authentication required")) as describe:
            result = d.probe_rtsp(
                "10.0.0.7", 554, username="admin", password="wrong",
                budget=budget)
        self.assertTrue(result.rtsp)
        self.assertTrue(result.auth_required)
        self.assertEqual(describe.call_count, 3)
        self.assertEqual(budget.used["10.0.0.7"], 3)

    def test_budget_stops_before_attempting_when_exhausted(self):
        budget = d.AuthBudget(limit=1)
        budget.record_failure("10.0.0.7")
        with patch.object(d, "_rtsp_describe") as describe:
            result = d.probe_rtsp(
                "10.0.0.7", 554, username="admin", password="wrong",
                budget=budget)
        describe.assert_not_called()
        self.assertTrue(result.auth_required)
        self.assertIn("locking", result.error)


class EndpointFilteringTests(unittest.TestCase):
    def test_model_identifies_nvr_camera_or_unknown_without_guessing(self):
        self.assertEqual(d._onvif_kind("NVR4108HS"), "nvr")
        self.assertEqual(d._onvif_kind("DVR-8CH"), "nvr")
        self.assertEqual(d._onvif_kind("IP Camera"), "camera")
        self.assertEqual(d._onvif_kind("IPC-42"), "camera")
        self.assertEqual(d._onvif_kind("Generic video device"), "onvif")
        self.assertEqual(d._onvif_kind("", "nvr"), "nvr")

    def test_four_profiles_alone_do_not_make_an_nvr(self):
        endpoint = d.Endpoint(host="10.0.0.4", port=80, scheme="http")
        session = d.OnvifResult(
            url=endpoint.url, onvif=True, authenticated=True,
            model="IP Camera", profiles=[
                {"token": f"p{i}", "name": f"Stream {i}",
                 "rtsp_url": f"rtsp://10.0.0.4/stream{i}"}
                for i in range(4)])
        with patch.object(d, "_channel_checks",
                          return_value=[d.RtspResult(rtsp=True, video=True)] * 4), \
                patch.object(d, "check_stream",
                             return_value={"accessible": True}):
            device = d._onvif_device(endpoint, session, False)
        self.assertEqual(device.kind, "camera")
        self.assertEqual(len(device.channels), 4)

    def test_discovery_does_not_submit_request_saved_or_env_credentials(self):
        endpoint = d.Endpoint(host="10.0.0.5", port=554, scheme="rtsp")
        request = DiscoveryRequest(username="admin", password="secret")
        challenge = d.RtspResult(
            rtsp=True, auth_required=True, path="/Streaming/Channels/101")
        with patch.dict(os.environ, {"ONVIF_USER": "env", "ONVIF_PASS": "pw"}), \
                patch.object(d, "probe_rtsp", return_value=challenge) as probe, \
                patch.object(d, "_credential_pairs",
                             side_effect=AssertionError("credentials used")):
            device = d._verify_endpoint(
                endpoint, request, {"10.0.0.5": ("saved", "pw")},
                True, True, authenticate=False)
        self.assertEqual(device.status, "credentials")
        self.assertFalse(device.verified)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[3:5], ("", ""))

    def test_discovery_removes_userinfo_from_onvif_advertisements(self):
        endpoint = d.Endpoint(
            host="10.0.0.5", port=80, scheme="http",
            xaddr="http://admin:secret@10.0.0.5/onvif/device_service")
        with patch.object(d, "probe_onvif",
                          return_value=d.OnvifResult(onvif=True,
                                                      auth_required=True,
                                                      url="http://10.0.0.5/onvif/device_service")) as probe, \
                patch.object(d, "_channel_checks", return_value=[]):
            device = d._verify_endpoint(
                endpoint, DiscoveryRequest(), {}, False, True,
                authenticate=False)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[0],
                         "http://10.0.0.5/onvif/device_service")
        self.assertEqual(device.status, "credentials")

    def test_selected_device_connect_uses_only_supplied_login(self):
        connected = DiscoveredDevice(
            id="10.0.0.5:554", name="Camera", host="10.0.0.5", port=554,
            address="rtsp://10.0.0.5:554/live", status="ready", verified=True)
        with patch.object(d, "_verify_endpoint", return_value=connected) as verify:
            result = d.connect_discovered_device(DiscoveryConnectRequest(
                address="rtsp://10.0.0.5:554/live", username="operator",
                password="correct"))
        self.assertIs(result, connected)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(verify.call_args.args[1].username, "operator")
        self.assertEqual(verify.call_args.args[1].password, "correct")
        self.assertEqual(verify.call_args.args[0].path, "/live")

    def test_selected_device_connect_rejects_embedded_credentials(self):
        with self.assertRaises(ValueError):
            d.connect_discovered_device(DiscoveryConnectRequest(
                address="rtsp://admin:secret@10.0.0.5/live"))

    def test_non_rtsp_http_endpoint_is_not_reported(self):
        endpoint = d.Endpoint(host="10.0.0.1", port=80, scheme="http")
        with patch.object(d, "probe_onvif",
                          return_value=d.OnvifResult(onvif=False)):
            device = d._verify_endpoint(
                endpoint, DiscoveryRequest(), {}, False, False)
        self.assertIsNone(device)

    def test_ssdp_web_server_without_onvif_is_not_reported(self):
        endpoint = d.Endpoint(host="10.0.0.2", port=80, scheme="http",
                              source="ssdp")
        with patch.object(d, "probe_onvif",
                          return_value=d.OnvifResult(onvif=False)):
            device = d._verify_endpoint(
                endpoint, DiscoveryRequest(), {}, False, True)
        self.assertIsNone(device)

    def test_closed_rtsp_port_is_not_reported(self):
        endpoint = d.Endpoint(host="10.0.0.3", port=554, scheme="rtsp")
        with patch.object(d, "probe_rtsp",
                          return_value=d.RtspResult(error="connection failed")):
            device = d._verify_endpoint(
                endpoint, DiscoveryRequest(), {}, False, False)
        self.assertIsNone(device)

    def test_onvif_device_without_decodable_video_is_not_verified(self):
        endpoint = d.Endpoint(host="10.0.0.4", port=80, scheme="http")
        session = d.OnvifResult(
            url="http://10.0.0.4/onvif/device_service", onvif=True,
            authenticated=True, manufacturer="Acme", model="Cam 1",
            profiles=[{"token": "p1", "name": "Main", "encoding": "H264",
                       "width": 1920, "height": 1080,
                       "rtsp_url": "rtsp://10.0.0.4/live"}],
            username="admin", password="pw")
        check = d.RtspResult(rtsp=True, video=True,
                             stream_url="rtsp://10.0.0.4/live")
        with patch.object(d, "probe_onvif", return_value=session), \
                patch.object(d, "_channel_checks", return_value=[check]), \
                patch.object(d, "check_stream",
                             return_value={"accessible": False,
                                           "error": "timeout"}):
            device = d._verify_endpoint(
                endpoint, DiscoveryRequest(), {}, False, True)
        self.assertIsNotNone(device)
        self.assertFalse(device.verified)
        self.assertEqual(device.status, "no_video")
        self.assertEqual(len(device.channels), 1)
        self.assertEqual(device.channels[0].width, 1920)

    def test_credential_aware_onvif_device_is_ready_when_frame_decodes(self):
        endpoint = d.Endpoint(host="10.0.0.4", port=80, scheme="http")
        session = d.OnvifResult(
            url="http://10.0.0.4/onvif/device_service", onvif=True,
            authenticated=True, manufacturer="Acme", model="NVR",
            profiles=[{"token": "p1", "name": "Main", "encoding": "H264",
                       "width": 1280, "height": 720,
                       "rtsp_url": "rtsp://10.0.0.4/Streaming/Channels/101"}],
            username="admin", password="pw")
        check = d.RtspResult(rtsp=True, video=True)
        with patch.object(d, "probe_onvif", return_value=session), \
                patch.object(d, "_channel_checks", return_value=[check]), \
                patch.object(d, "check_stream",
                             return_value={"accessible": True,
                                           "width": 1280, "height": 720}):
            device = d._verify_endpoint(
                endpoint, DiscoveryRequest(), {}, False, True)
        self.assertTrue(device.verified)
        self.assertEqual(device.status, "ready")
        self.assertEqual(device.kind, "nvr")
        self.assertIn("admin:pw@", device.rtsp_url)


class SavedPathTests(unittest.TestCase):
    SDP = "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"

    def _describe(self, path_ok):
        def describe(host, port, path, user, password,
                     timeout=d.RTSP_TIMEOUT):
            if not user:
                return ("RTSP/1.0 401 Unauthorized", {}, "",
                        "authentication required")
            if path == path_ok:
                return ("RTSP/1.0 200 OK", {"server": "cam"}, self.SDP, "")
            return ("RTSP/1.0 401 Unauthorized", {}, "",
                    "authentication required")
        return describe

    def test_saved_path_is_tried_first_and_spends_no_budget(self):
        endpoint = d.Endpoint(host="10.0.0.8", port=554, scheme="rtsp")
        budget = d.AuthBudget()
        with patch.object(d, "_rtsp_describe",
                          side_effect=self._describe(
                              "/Streaming/Channels/101")), \
                patch.object(d, "check_stream",
                             return_value={"accessible": True}):
            device = d._verify_endpoint(
                endpoint,
                DiscoveryRequest(username="admin", password="pw"),
                {}, True, True, budget,
                ["/Streaming/Channels/101"])
        self.assertEqual(device.status, "ready")
        self.assertTrue(device.verified)
        self.assertEqual(budget.used, {})

    def test_rejected_saved_path_costs_one_attempt_not_four(self):
        endpoint = d.Endpoint(host="10.0.0.8", port=554, scheme="rtsp")
        budget = d.AuthBudget()
        with patch.object(d, "_rtsp_describe",
                          side_effect=self._describe("/nonexistent")):
            device = d._verify_endpoint(
                endpoint,
                DiscoveryRequest(username="admin", password="wrong"),
                {}, True, True, budget,
                ["/Streaming/Channels/101"])
        self.assertEqual(device.status, "credentials")
        self.assertEqual(budget.used.get("10.0.0.8"), 1)

    def test_second_login_is_tried_before_vendor_path_guesses(self):
        endpoint = d.Endpoint(host="10.0.0.8", port=554, scheme="rtsp")
        request = DiscoveryRequest(
            username="admin", password="wrong",
            credentials=[DiscoveryCredentials(
                username="admin", password="correct")])
        budget = d.AuthBudget(limit=4)
        attempts = []

        def describe(host, port, path, user, password,
                     timeout=d.RTSP_TIMEOUT):
            attempts.append((path, user, password))
            if user and password == "correct":
                return ("RTSP/1.0 200 OK", {}, self.SDP, "")
            return ("RTSP/1.0 401 Unauthorized", {}, "", "auth")

        with patch.object(d, "_rtsp_describe", side_effect=describe), \
                patch.object(d, "check_stream",
                             return_value={"accessible": True}):
            device = d._verify_endpoint(
                endpoint, request, {}, False, True, budget)
        self.assertEqual(device.status, "ready")
        self.assertEqual(budget.used.get("10.0.0.8"), 1)
        self.assertEqual(attempts[:3], [
            ("/Streaming/Channels/101", "", ""),
            ("/Streaming/Channels/101", "admin", "wrong"),
            ("/Streaming/Channels/101", "admin", "correct")])


class ScopeTests(unittest.TestCase):
    def test_priority_port_is_retried_after_transient_failure(self):
        calls = []

        def probe(tasks, timeout=d.TCP_TIMEOUT, workers=d.MAX_WORKERS):
            calls.append((list(tasks), timeout, workers))
            return [] if len(calls) == 1 else [("10.0.0.5", 554)] if len(calls) == 2 else []

        with patch.object(d, "_probe_many", side_effect=probe):
            opened = d._scan_hosts(["10.0.0.5"], False, [])
        self.assertIn(554, opened["10.0.0.5"])
        self.assertNotIn(("10.0.0.5", 554), calls[2][0])
        self.assertEqual(calls[1][2], 32)

    def test_saved_rtsp_url_supplies_encoded_credentials(self):
        saved = SimpleNamespace(
            address="rtsp://10.0.0.5:554/live",
            rtsp_url="rtsp://ad%20min:p%40ss@10.0.0.5:554/live",
            onvif_user="", onvif_pass="")
        with patch.object(d.db, "list_cameras", return_value=[saved]):
            _, hosts, credentials, paths = d._saved_state()
        self.assertEqual(hosts, {"10.0.0.5"})
        self.assertEqual(credentials["10.0.0.5"], ("ad min", "p@ss"))
        self.assertEqual(paths["10.0.0.5"], ["/live"])

    def test_single_address_and_subnet_expand(self):
        self.assertEqual(d._expand_target("10.0.0.5"), ["10.0.0.5"])
        self.assertEqual(
            d._expand_target("10.0.0.0/30"),
            ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(d._expand_target("rtsp://10.0.0.9/live"),
                         ["10.0.0.9"])

    def test_window_around_anchor_keeps_requested_host(self):
        import ipaddress
        network = ipaddress.ip_network("10.0.0.0/24")
        hosts = d._network_hosts(network, limit=10, anchor="10.0.0.200")
        self.assertEqual(len(hosts), 10)
        self.assertIn("10.0.0.200", hosts)


class ReportTests(unittest.TestCase):
    def test_verified_rtsp_stream_inherits_same_hosts_onvif_hardware_type(self):
        rtsp = DiscoveredDevice(
            id="10.0.0.5:554", name="RTSP source 10.0.0.5",
            address="rtsp://10.0.0.5:554/live", source_type="manual-rtsp",
            kind="rtsp", host="10.0.0.5", port=554,
            status="ready", verified=True)
        onvif = DiscoveredDevice(
            id="10.0.0.5:80", name="Acme NVR4108",
            address="http://10.0.0.5/onvif/device_service",
            source_type="onvif", kind="nvr", model="NVR4108",
            host="10.0.0.5", port=80, status="credentials")
        with patch.object(d, "_build_scope", return_value=([], ["10.0.0.5"])), \
                patch.object(d, "_scan_hosts",
                             return_value={"10.0.0.5": [80, 554]}), \
                patch.object(d, "_ws_discover", return_value=[]), \
                patch.object(d, "_ssdp_discover", return_value=[]), \
                patch.object(d, "_saved_state",
                             return_value=(set(), set(), {}, {})), \
                patch.object(d, "_verify_endpoint",
                             side_effect=lambda ep, *a, **k:
                             rtsp if ep.scheme == "rtsp" else onvif), \
                patch.object(d.db, "list_cameras", return_value=[]):
            report = d.discover_devices(DiscoveryRequest())
        self.assertEqual(len(report.devices), 1)
        self.assertEqual(report.devices[0].kind, "nvr")
        self.assertEqual(report.devices[0].status, "ready")
        self.assertEqual(report.devices[0].rtsp_url, rtsp.rtsp_url)

    def test_saved_rtsp_port_is_verified_even_when_sweep_misses_it(self):
        saved = SimpleNamespace(
            address="rtsp://10.0.0.5:10554/live",
            rtsp_url="rtsp://admin:pw@10.0.0.5:10554/live",
            onvif_user="", onvif_pass="")
        device = DiscoveredDevice(
            id="10.0.0.5:10554", name="Cam",
            address="rtsp://10.0.0.5:10554/live", source_type="manual-rtsp",
            host="10.0.0.5", port=10554, status="ready", verified=True)
        seen = []

        def verify(endpoint, *args, **kwargs):
            seen.append(endpoint)
            return device

        with patch.object(d, "_build_scope", return_value=([], ["10.0.0.5"])), \
                patch.object(d, "_scan_hosts", return_value={}), \
                patch.object(d, "_ws_discover", return_value=[]), \
                patch.object(d, "_ssdp_discover", return_value=[]), \
                patch.object(d, "_verify_endpoint", side_effect=verify), \
                patch.object(d.db, "list_cameras", return_value=[saved]):
            report = d.discover_devices(DiscoveryRequest(include_saved=True))
        self.assertEqual(len(report.devices), 1)
        self.assertEqual((seen[0].port, seen[0].path, seen[0].scheme),
                         (10554, None, "rtsp"))

    def test_run_without_any_endpoint_warns_and_returns_no_devices(self):
        with patch.object(d, "_build_scope", return_value=([], [])), \
                patch.object(d, "_ws_discover", return_value=[]), \
                patch.object(d, "_ssdp_discover", return_value=[]), \
                patch.object(d, "_scan_hosts", return_value={}), \
                patch.object(d, "_saved_state",
                             return_value=(set(), set(), {}, {})), \
                patch.object(d, "db") as fake_db:
            fake_db.list_cameras.return_value = []
            report = d.discover_devices(DiscoveryRequest())
        self.assertIsInstance(report, DiscoveryReport)
        self.assertEqual(report.devices, [])
        self.assertEqual(report.scanned_hosts, 0)
        self.assertTrue(any("No ONVIF or RTSP service answered" in w
                            for w in report.warnings))

    def test_discovered_devices_are_grouped_by_host(self):
        endpoint = d.Endpoint(host="10.0.0.5", port=80, scheme="http")
        device = DiscoveredDevice(
            id="10.0.0.5:80", name="Cam", address="http://10.0.0.5",
            source_type="onvif", host="10.0.0.5", port=80,
            status="ready", verified=True)

        with patch.object(d, "_build_scope",
                          return_value=([], ["10.0.0.5"])), \
                patch.object(d, "_ws_discover", return_value=[endpoint]), \
                patch.object(d, "_ssdp_discover", return_value=[]), \
                patch.object(d, "_scan_hosts", return_value={"10.0.0.5": [80]}), \
                patch.object(d, "_saved_state",
                             return_value=(set(), set(), {}, {})), \
                patch.object(d, "_verify_endpoint",
                             side_effect=lambda *a, **k: device), \
                patch.object(d, "db") as fake_db:
            fake_db.list_cameras.return_value = []
            report = d.discover_devices(
                DiscoveryRequest(include_saved=True))
        self.assertEqual(len(report.devices), 1)
        self.assertEqual(report.devices[0].host, "10.0.0.5")
        self.assertEqual(report.scanned_hosts, 1)

    def test_saved_device_is_hidden_unless_requested(self):
        endpoint = d.Endpoint(host="10.0.0.5", port=80, scheme="http")
        device = DiscoveredDevice(
            id="10.0.0.5:80", name="Saved", address="http://10.0.0.5",
            source_type="onvif", host="10.0.0.5", port=80,
            status="ready", verified=True)

        def run(include_saved):
            with patch.object(d, "_build_scope",
                              return_value=([], ["10.0.0.5"])), \
                    patch.object(d, "_ws_discover", return_value=[endpoint]), \
                    patch.object(d, "_ssdp_discover", return_value=[]), \
                    patch.object(d, "_scan_hosts",
                                 return_value={"10.0.0.5": [80]}), \
                    patch.object(d, "_saved_state",
                                 return_value=(set(), {"10.0.0.5"}, {}, {})), \
                    patch.object(d, "_verify_endpoint",
                                 side_effect=lambda *a, **k: device), \
                    patch.object(d, "db") as fake_db:
                fake_db.list_cameras.return_value = []
                return d.discover_devices(
                    DiscoveryRequest(include_saved=include_saved))

        self.assertEqual(run(False).devices, [])
        self.assertEqual(len(run(True).devices), 1)

    def test_no_credential_warning_when_credentials_supplied(self):
        with patch.object(d, "_build_scope", return_value=([], [])), \
                patch.object(d, "_ws_discover", return_value=[]), \
                patch.object(d, "_ssdp_discover", return_value=[]), \
                patch.object(d, "_scan_hosts", return_value={}), \
                patch.object(d, "_saved_state",
                             return_value=(set(), set(), {}, {})), \
                patch.object(d, "db") as fake_db:
            fake_db.list_cameras.return_value = []
            report = d.discover_devices(
                DiscoveryRequest(username="admin", password="pw"))
        self.assertFalse(any("No credentials" in w for w in report.warnings))


if __name__ == "__main__":
    unittest.main()
