"""Transmissão da tela como câmera RTSP.

mediamtx  -> servidor RTSP na porta 8554 (o DVR conecta aqui com usuário/senha da câmera)
ffmpeg    -> captura a tela (gdigrab no Windows), codifica H.264 e publica no mediamtx
"""
from __future__ import annotations

import logging
import re
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path

from .config import Config, bin_dir, data_dir

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _exe(name: str) -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    p = bin_dir() / f"{name}{suffix}"
    return str(p) if p.exists() else name  # cai para o PATH


def mediamtx_config(cfg: Config, publisher_pass: str) -> str:
    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    return f"""\
logLevel: warn
logDestinations: [stdout]
api: no
metrics: no
pprof: no
playback: no
rtmp: no
hls: no
webrtc: no
srt: no
rtsp: yes
rtspTransports: [tcp, udp]
rtspAddress: :{cfg.rtsp_port}
# DVRs/NVRs costumam usar digest; basic mantido por compatibilidade.
rtspAuthMethods: [basic, digest]
authMethod: internal
authInternalUsers:
- user: publisher
  pass: {q(publisher_pass)}
  ips: ['127.0.0.1', '::1']
  permissions:
  - action: publish
    path: {q(cfg.stream_path)}
- user: {q(cfg.camera_user)}
  pass: {q(cfg.camera_password)}
  ips: []
  permissions:
  - action: read
    path: {q(cfg.stream_path)}
paths:
  {cfg.stream_path}:
    source: publisher
"""


def _safe_label(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._-]", "", s)[:40] or "PC"


def ffmpeg_args(cfg: Config, features: dict, publisher_pass: str) -> list[str]:
    fps = max(1, int(features.get("max_fps", 10)))
    height = int(features.get("max_height", 720))
    label = _safe_label(socket.gethostname())
    # Carimbo de data/hora + nome do PC na imagem, como em câmeras de CFTV.
    drawtext = (
        "drawtext=text='" + label + r" %{localtime\:%d/%m/%Y %X}'"
        ":x=10:y=10:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.5"
    )
    if sys.platform == "win32":
        drawtext += r":fontfile='C\:/Windows/Fonts/arial.ttf'"
        capture = ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1", "-i", "desktop"]
    else:  # útil para testes em Linux
        capture = ["-f", "x11grab", "-framerate", str(fps), "-draw_mouse", "1", "-i", ":0.0"]
    vf = f"scale=-2:'min({height},ih)':flags=bicubic,{drawtext},format=yuv420p"
    url = f"rtsp://publisher:{publisher_pass}@127.0.0.1:{cfg.rtsp_port}/{cfg.stream_path}"
    return [
        _exe("ffmpeg"), "-hide_banner", "-loglevel", "error",
        *capture,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "main", "-bf", "0", "-g", str(fps * 2),
        "-b:v", f"{cfg.bitrate_kbps}k", "-maxrate", f"{cfg.bitrate_kbps}k", "-bufsize", f"{cfg.bitrate_kbps * 2}k",
        "-f", "rtsp", "-rtsp_transport", "tcp", url,
    ]


class StreamManager:
    """Liga/desliga a transmissão. Reinicia processos que caírem."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pass = secrets.token_urlsafe(16)
        self._mtx: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None
        self._features: dict = {}
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._ff is not None and self._ff.poll() is None

    def _spawn(self, args: list[str], name: str) -> subprocess.Popen:
        logf = open(data_dir() / f"{name}.log", "ab")
        return subprocess.Popen(args, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)

    def ensure(self, features: dict) -> None:
        """Garante que está transmitindo com os limites do plano atual."""
        with self._lock:
            if features != self._features and self._ff is not None:
                self._kill_ffmpeg()
            self._features = features
            if self._mtx is None or self._mtx.poll() is not None:
                cfg_path = Path(data_dir() / "mediamtx.yml")
                cfg_path.write_text(mediamtx_config(self.cfg, self._pass), encoding="utf-8")
                self._mtx = self._spawn([_exe("mediamtx"), str(cfg_path)], "mediamtx")
                log.info("mediamtx iniciado na porta %s", self.cfg.rtsp_port)
            if self._ff is None or self._ff.poll() is not None:
                self._ff = self._spawn(ffmpeg_args(self.cfg, features, self._pass), "ffmpeg")
                log.info("captura de tela iniciada (%s fps, até %sp)", features.get("max_fps"), features.get("max_height"))

    def _kill_ffmpeg(self) -> None:
        if self._ff and self._ff.poll() is None:
            self._ff.terminate()
            try:
                self._ff.wait(5)
            except subprocess.TimeoutExpired:
                self._ff.kill()
        self._ff = None

    def stop(self) -> None:
        with self._lock:
            self._kill_ffmpeg()
            if self._mtx and self._mtx.poll() is None:
                self._mtx.terminate()
                try:
                    self._mtx.wait(5)
                except subprocess.TimeoutExpired:
                    self._mtx.kill()
            self._mtx = None
            self._features = {}
