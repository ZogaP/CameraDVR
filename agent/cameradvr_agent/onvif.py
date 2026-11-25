"""Servidor ONVIF mínimo (Profile S) para o DVR/NVR reconhecer o PC como câmera IP.

- WS-Discovery (UDP 3702): o DVR acha o PC na busca automática de câmeras.
- Device/Media service (HTTP): informa perfil, resolução e o endereço RTSP.
- Autenticação WS-UsernameToken (digest e texto) com usuário/senha da câmera.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import socket
import struct
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from .config import AGENT_VERSION, Config
from .license_client import local_ip, machine_id

log = logging.getLogger(__name__)

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
}
ENV_NS = (
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:ter="http://www.onvif.org/ver10/error"'
)
# Ações liberadas sem senha (o DVR as usa antes de autenticar).
PUBLIC_ACTIONS = {"GetSystemDateAndTime", "GetCapabilities", "GetServices", "GetServiceCapabilities", "GetDeviceInformation"}

PASSWORD_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"


def device_uuid() -> str:
    return str(uuid.UUID(machine_id()[:32]))


def screen_size() -> tuple[int, int]:
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.SetProcessDPIAware()
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:  # noqa: BLE001
        return 1920, 1080


def stream_resolution(max_height: int) -> tuple[int, int]:
    w, h = screen_size()
    if h > max_height:
        w, h = round(w * max_height / h / 2) * 2, max_height
    return w, h


def envelope(body: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {ENV_NS}><s:Body>{body}</s:Body></s:Envelope>'.encode()


def fault(code: str, subcode: str, reason: str) -> bytes:
    return envelope(
        f"<s:Fault><s:Code><s:Value>s:{code}</s:Value><s:Subcode><s:Value>ter:{subcode}</s:Value>"
        f"</s:Subcode></s:Code><s:Reason><s:Text xml:lang=\"en\">{escape(reason)}</s:Text></s:Reason></s:Fault>"
    )


def check_auth(root: ET.Element, user: str, password: str) -> bool:
    tok = root.find(".//wsse:UsernameToken", NS)
    if tok is None:
        return False
    u = tok.findtext("wsse:Username", default="", namespaces=NS)
    pw_el = tok.find("wsse:Password", NS)
    if pw_el is None or not hmac.compare_digest(u, user):
        return False
    given = (pw_el.text or "").strip()
    if pw_el.get("Type", "") == PASSWORD_DIGEST:
        nonce_b64 = tok.findtext("wsse:Nonce", default="", namespaces=NS)
        created = tok.findtext("wsu:Created", default="", namespaces=NS)
        try:
            nonce = base64.b64decode(nonce_b64)
        except ValueError:
            return False
        expected = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        return hmac.compare_digest(expected, given)
    return hmac.compare_digest(given, password)


class OnvifDevice:
    """Gera as respostas SOAP. `features_fn` devolve os limites do plano (fps/altura)."""

    def __init__(self, cfg: Config, features_fn, licensed_fn):
        self.cfg = cfg
        self.features_fn = features_fn
        self.licensed_fn = licensed_fn
        self.uuid = device_uuid()

    # endereços
    def xaddr(self, ip: str) -> str:
        return f"http://{ip}:{self.cfg.onvif_port}/onvif/device_service"

    def media_xaddr(self, ip: str) -> str:
        return f"http://{ip}:{self.cfg.onvif_port}/onvif/media_service"

    def rtsp_uri(self, ip: str) -> str:
        return f"rtsp://{ip}:{self.cfg.rtsp_port}/{self.cfg.stream_path}"

    def _video_xml(self) -> tuple[str, str]:
        f = self.features_fn() or {}
        fps = int(f.get("max_fps", 10))
        w, h = stream_resolution(int(f.get("max_height", 720)))
        src = (
            '<tt:VideoSourceConfiguration token="vsc_1"><tt:Name>Tela</tt:Name><tt:UseCount>1</tt:UseCount>'
            f'<tt:SourceToken>vs_1</tt:SourceToken><tt:Bounds x="0" y="0" width="{w}" height="{h}"/>'
            "</tt:VideoSourceConfiguration>"
        )
        enc = (
            '<tt:VideoEncoderConfiguration token="vec_1"><tt:Name>H264</tt:Name><tt:UseCount>1</tt:UseCount>'
            f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
            "<tt:Quality>5</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{fps}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{self.cfg.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
            f"<tt:H264><tt:GovLength>{fps * 2}</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            '<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>'
            "<tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout></tt:VideoEncoderConfiguration>"
        )
        return src, enc

    def _profile(self) -> str:
        src, enc = self._video_xml()
        return f'<trt:Profiles token="profile_1" fixed="true"><tt:Name>Tela</tt:Name>{src}{enc}</trt:Profiles>'

    def handle(self, action: str, root: ET.Element, ip: str) -> bytes:
        host = escape(socket.gethostname())
        if action == "GetSystemDateAndTime":
            n = datetime.now(timezone.utc)
            return envelope(
                "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
                "<tt:DaylightSavings>false</tt:DaylightSavings><tt:TimeZone><tt:TZ>UTC</tt:TZ></tt:TimeZone>"
                f"<tt:UTCDateTime><tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
                f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date></tt:UTCDateTime>"
                "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
            )
        if action == "GetDeviceInformation":
            return envelope(
                "<tds:GetDeviceInformationResponse><tds:Manufacturer>CameraDVR</tds:Manufacturer>"
                f"<tds:Model>ScreenCam {host}</tds:Model><tds:FirmwareVersion>{AGENT_VERSION}</tds:FirmwareVersion>"
                f"<tds:SerialNumber>{self.uuid[:13]}</tds:SerialNumber><tds:HardwareId>PC</tds:HardwareId>"
                "</tds:GetDeviceInformationResponse>"
            )
        if action == "GetCapabilities":
            return envelope(
                "<tds:GetCapabilitiesResponse><tds:Capabilities>"
                f"<tt:Device><tt:XAddr>{self.xaddr(ip)}</tt:XAddr></tt:Device>"
                f"<tt:Media><tt:XAddr>{self.media_xaddr(ip)}</tt:XAddr><tt:StreamingCapabilities>"
                "<tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
                "</tt:StreamingCapabilities></tt:Media>"
                "</tds:Capabilities></tds:GetCapabilitiesResponse>"
            )
        if action == "GetServices":
            return envelope(
                "<tds:GetServicesResponse>"
                f"<tds:Service><tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace><tds:XAddr>{self.xaddr(ip)}</tds:XAddr>"
                "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>"
                f"<tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace><tds:XAddr>{self.media_xaddr(ip)}</tds:XAddr>"
                "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>"
                "</tds:GetServicesResponse>"
            )
        if action == "GetServiceCapabilities":
            return envelope(
                '<trt:GetServiceCapabilitiesResponse><trt:Capabilities SnapshotUri="true" Rotation="false">'
                '<trt:ProfileCapabilities MaximumNumberOfProfiles="1"/>'
                '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true"/>'
                "</trt:Capabilities></trt:GetServiceCapabilitiesResponse>"
            )
        if action == "GetScopes":
            scopes = [
                "onvif://www.onvif.org/type/video_encoder",
                "onvif://www.onvif.org/Profile/Streaming",
                f"onvif://www.onvif.org/name/{escape(socket.gethostname())}",
                "onvif://www.onvif.org/hardware/PC",
            ]
            items = "".join(f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{s}</tt:ScopeItem></tds:Scopes>" for s in scopes)
            return envelope(f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>")
        if action == "GetHostname":
            return envelope(f"<tds:GetHostnameResponse><tds:HostnameInformation><tt:FromDHCP>false</tt:FromDHCP><tt:Name>{host}</tt:Name></tds:HostnameInformation></tds:GetHostnameResponse>")
        if action == "GetNetworkInterfaces":
            return envelope(
                '<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token="eth0"><tt:Enabled>true</tt:Enabled>'
                f"<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config><tt:Manual><tt:Address>{ip}</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>"
                "<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>"
            )
        if action == "GetProfiles":
            return envelope(f"<trt:GetProfilesResponse>{self._profile()}</trt:GetProfilesResponse>")
        if action == "GetProfile":
            return envelope(f"<trt:GetProfileResponse>{self._profile().replace('trt:Profiles', 'trt:Profile')}</trt:GetProfileResponse>")
        if action == "GetVideoSources":
            w, h = stream_resolution(int((self.features_fn() or {}).get("max_height", 720)))
            fps = int((self.features_fn() or {}).get("max_fps", 10))
            return envelope(
                f'<trt:GetVideoSourcesResponse><trt:VideoSources token="vs_1"><tt:Framerate>{fps}</tt:Framerate>'
                f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>"
            )
        if action in ("GetVideoSourceConfigurations", "GetVideoSourceConfiguration"):
            src, _ = self._video_xml()
            tag = "Configurations" if action.endswith("s") else "Configuration"
            src = src.replace("tt:VideoSourceConfiguration", f"trt:{tag}", 2)
            return envelope(f"<trt:{action}Response>{src}</trt:{action}Response>")
        if action in ("GetVideoEncoderConfigurations", "GetVideoEncoderConfiguration"):
            _, enc = self._video_xml()
            tag = "Configurations" if action.endswith("s") else "Configuration"
            enc = enc.replace("tt:VideoEncoderConfiguration", f"trt:{tag}", 2)
            return envelope(f"<trt:{action}Response>{enc}</trt:{action}Response>")
        if action in ("GetAudioSources", "GetAudioSourceConfigurations", "GetAudioEncoderConfigurations"):
            return envelope(f"<trt:{action}Response/>")
        if action == "GetStreamUri":
            return envelope(
                f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{self.rtsp_uri(ip)}</tt:Uri>"
                "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
                "<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>"
            )
        if action == "GetSnapshotUri":
            return envelope(
                f"<trt:GetSnapshotUriResponse><trt:MediaUri><tt:Uri>http://{ip}:{self.cfg.onvif_port}/snapshot.jpg</tt:Uri>"
                "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
                "<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:GetSnapshotUriResponse>"
            )
        if action in ("SetSystemDateAndTime", "SetVideoEncoderConfiguration", "SetSynchronizationPoint"):
            return envelope(f"<{'tds' if action == 'SetSystemDateAndTime' else 'trt'}:{action}Response/>")
        return fault("Receiver", "ActionNotSupported", f"{action} not supported")

    def snapshot(self) -> bytes | None:
        """Um quadro JPEG tirado do próprio stream RTSP local."""
        from .stream import _exe

        url = f"rtsp://{self.cfg.camera_user}:{self.cfg.camera_password}@127.0.0.1:{self.cfg.rtsp_port}/{self.cfg.stream_path}"
        try:
            out = subprocess.run(
                [_exe("ffmpeg"), "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url,
                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                capture_output=True, timeout=10,
                creationflags=0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            return out.stdout or None
        except (OSError, subprocess.TimeoutExpired):
            return None


def make_handler(dev: OnvifDevice):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silencia log padrão
            log.debug("onvif %s - " + fmt, self.client_address[0], *args)

        def _send(self, code: int, body: bytes, ctype: str = "application/soap+xml; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/snapshot.jpg"):
                if not self._basic_ok():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="CameraDVR"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                img = dev.snapshot() if dev.licensed_fn() else None
                if img:
                    return self._send(200, img, "image/jpeg")
                return self._send(503, b"", "text/plain")
            self._send(404, b"", "text/plain")

        def _basic_ok(self) -> bool:
            h = self.headers.get("Authorization", "")
            if not h.startswith("Basic "):
                return False
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
            except ValueError:
                return False
            return hmac.compare_digest(u, dev.cfg.camera_user) and hmac.compare_digest(p, dev.cfg.camera_password)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(min(length, 256 * 1024))
            try:
                root = ET.fromstring(raw)
                body = root.find("s:Body", NS)
                if body is None:  # SOAP 1.1
                    body = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
                action = list(body)[0].tag.split("}")[-1]
            except (ET.ParseError, IndexError, TypeError):
                return self._send(400, fault("Sender", "WellFormed", "bad request"))
            if action not in PUBLIC_ACTIONS and not check_auth(root, dev.cfg.camera_user, dev.cfg.camera_password):
                return self._send(400, fault("Sender", "NotAuthorized", "Sender not Authorized"))
            ip = self.headers.get("Host", "").split(":")[0] or local_ip()
            resp = dev.handle(action, root, ip)
            self._send(500 if b"<s:Fault>" in resp else 200, resp)

    return Handler


def probe_match_xml(dev: OnvifDevice, relates_to: str, ip: str) -> bytes:
    scopes = " ".join([
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/Profile/Streaming",
        f"onvif://www.onvif.org/name/{escape(socket.gethostname())}",
        "onvif://www.onvif.org/hardware/PC",
    ])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        "<s:Header>"
        f"<a:MessageID>uuid:{uuid.uuid4()}</a:MessageID>"
        f"<a:RelatesTo>{escape(relates_to)}</a:RelatesTo>"
        "<a:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:To>"
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action>"
        "</s:Header><s:Body><d:ProbeMatches><d:ProbeMatch>"
        f"<a:EndpointReference><a:Address>urn:uuid:{dev.uuid}</a:Address></a:EndpointReference>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        f"<d:Scopes>{scopes}</d:Scopes>"
        f"<d:XAddrs>{dev.xaddr(ip)}</d:XAddrs>"
        "<d:MetadataVersion>1</d:MetadataVersion>"
        "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
    ).encode()


class Discovery(threading.Thread):
    MCAST = "239.255.255.250"
    PORT = 3702

    def __init__(self, dev: OnvifDevice):
        super().__init__(daemon=True, name="ws-discovery")
        self.dev = dev
        self.sock: socket.socket | None = None

    def run(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self.PORT))
            mreq = struct.pack("4s4s", socket.inet_aton(self.MCAST), socket.inet_aton(local_ip()))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.sock = s
        except OSError as e:
            log.warning("WS-Discovery indisponível (%s); adicione a câmera manualmente no DVR", e)
            return
        log.info("WS-Discovery ativo")
        while True:
            try:
                data, addr = s.recvfrom(65535)
                root = ET.fromstring(data)
                if root.find(".//d:Probe", NS) is None:
                    continue
                types = root.findtext(".//d:Types", default="", namespaces=NS)
                if types and "NetworkVideoTransmitter" not in types and "Device" not in types:
                    continue
                msg_id = root.findtext(".//a:MessageID", default="", namespaces=NS)
                if not self.dev.licensed_fn():
                    continue  # sem licença, não aparece para o DVR
                s.sendto(probe_match_xml(self.dev, msg_id, local_ip()), addr)
            except ET.ParseError:
                continue
            except OSError:
                if self.sock is None:
                    return


class OnvifServer:
    def __init__(self, cfg: Config, features_fn, licensed_fn):
        self.dev = OnvifDevice(cfg, features_fn, licensed_fn)
        self.httpd = ThreadingHTTPServer(("0.0.0.0", cfg.onvif_port), make_handler(self.dev))
        self.discovery = Discovery(self.dev)

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="onvif-http").start()
        self.discovery.start()
        log.info("ONVIF em http://%s:%s/onvif/device_service", local_ip(), self.dev.cfg.onvif_port)

    def stop(self):
        self.httpd.shutdown()
        if self.discovery.sock:
            s, self.discovery.sock = self.discovery.sock, None
            s.close()
