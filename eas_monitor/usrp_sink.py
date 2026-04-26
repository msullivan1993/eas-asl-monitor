"""
usrp_sink.py - USRP UDP Audio Sink
=====================================
Injects demodulated audio into app_rpt via the USRP channel driver.
Used by USB Direct, RTL-SDR, and Stream sources.

NOT used by USB Shared (RIM-Lite) — that source uses the existing
channel driver audio path directly.

USRP packet format (chan_usrp.c):
  magic[4]     = "USRP"
  seq[4]       = sequence number (big-endian uint32)
  memory[4]    = 0
  time[4]      = 0
  type[4]      = 0 (voice)
  keyup[4]     = 1 = keyed/active, 0 = unkeyed/idle
  talkgroup[4] = 0
  audio[320]   = 160 signed 16-bit samples at 8kHz (20ms frame)
  Total        = 28 + 320 = 348 bytes per packet
"""

import logging
import socket
import struct
import time

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import audioop
    HAS_AUDIOOP = True
except ImportError:
    HAS_AUDIOOP = False

# USRP packet constants
USRP_MAGIC        = b'USRP'
USRP_HEADER_FMT   = '>4sIIIIII'   # big-endian
USRP_HEADER_SIZE  = struct.calcsize(USRP_HEADER_FMT)  # 28 bytes
USRP_FRAME_SAMPLES = 160           # samples per packet (20ms @ 8kHz)
USRP_FRAME_BYTES   = USRP_FRAME_SAMPLES * 2  # 320 bytes (int16)

# Keepalive: send unkeyed packets periodically so app_rpt doesn't time out
KEEPALIVE_INTERVAL = 5.0   # seconds


class USRPSink:
    """
    Sends demodulated audio to an app_rpt USRP node via UDP.

    PTT is controlled explicitly:
    - key_up()   → node becomes active, audio is forwarded
    - key_down() → node returns to idle
    - write()    → audio is buffered and sent only when keyed

    Between alerts the node is idle — no audio reaches connected nodes.
    """

    def __init__(self, tx_port: int, rx_port: int,
                 host: str = '127.0.0.1',
                 log: logging.Logger = None):
        self.host     = host
        self.tx_port  = tx_port   # port app_rpt (chan_usrp) listens on
        self.rx_port  = rx_port   # port we listen on (for completeness)
        self.log      = log or logging.getLogger('eas_monitor.usrp')
        self._seq     = 0
        self._keyed   = False
        self._last_keepalive = 0.0
        self._resample_state = None  # audioop resample state

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(('127.0.0.1', rx_port))
        self._addr = (host, tx_port)

    # ── Packet building ────────────────────────────────────────────────────

    def _make_packet(self, audio_320: bytes, keyed: bool) -> bytes:
        header = struct.pack(
            USRP_HEADER_FMT,
            USRP_MAGIC,
            self._seq,
            0, 0, 0,
            1 if keyed else 0,
            0
        )
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return header + audio_320

    def _send_packet(self, audio_320: bytes, keyed: bool):
        try:
            pkt = self._make_packet(audio_320, keyed)
            self._sock.sendto(pkt, self._addr)
        except Exception as e:
            self.log.error(f"USRP send error: {e}")

    # ── PTT control ────────────────────────────────────────────────────────

    @property
    def is_keyed(self) -> bool:
        return self._keyed

    def key_up(self):
        """Assert PTT — app_rpt begins forwarding audio to connected nodes."""
        if not self._keyed:
            self._keyed = True
            self.log.debug(f"USRP key up → {self.host}:{self.tx_port}")
            # Send keyed silence frame to assert PTT immediately
            self._send_packet(b'\x00' * USRP_FRAME_BYTES, keyed=True)
            self._last_keepalive = time.time()

    def key_down(self):
        """Drop PTT — app_rpt stops forwarding, node returns to idle."""
        if self._keyed:
            self._keyed = False
            self.log.debug(f"USRP key down → {self.host}:{self.tx_port}")
            self._send_packet(b'\x00' * USRP_FRAME_BYTES, keyed=False)

    def keepalive(self):
        """
        Send a keyed silence frame if PTT is active and interval has elapsed.
        Prevents app_rpt from timing out the channel during quiet passages.
        Call periodically from the main loop.
        """
        if self._keyed:
            now = time.time()
            if now - self._last_keepalive > KEEPALIVE_INTERVAL:
                self._send_packet(b'\x00' * USRP_FRAME_BYTES, keyed=True)
                self._last_keepalive = now

    # ── Audio writing ──────────────────────────────────────────────────────

    def write_pcm_22050(self, pcm_22050: bytes):
        """
        Accept raw signed 16-bit PCM at 22050Hz (from multimon-ng pipeline).
        Resamples to 8kHz and sends to USRP. Silently discards if not keyed.
        """
        if not self._keyed:
            return

        pcm_8k = self._resample_22050_to_8000(pcm_22050)
        if not pcm_8k:
            return

        # Send in 160-sample (20ms) USRP frames
        for i in range(0, len(pcm_8k) - USRP_FRAME_BYTES + 1,
                       USRP_FRAME_BYTES):
            frame = pcm_8k[i:i + USRP_FRAME_BYTES]
            if len(frame) == USRP_FRAME_BYTES:
                self._send_packet(frame, keyed=True)

        self._last_keepalive = time.time()

    def write_pcm_8000(self, pcm_8000: bytes):
        """
        Accept raw signed 16-bit PCM already at 8kHz.
        Sends directly to USRP. Used by wideband RTL-SDR pipeline.
        """
        if not self._keyed:
            return

        for i in range(0, len(pcm_8000) - USRP_FRAME_BYTES + 1,
                       USRP_FRAME_BYTES):
            frame = pcm_8000[i:i + USRP_FRAME_BYTES]
            if len(frame) == USRP_FRAME_BYTES:
                self._send_packet(frame, keyed=True)

        self._last_keepalive = time.time()

    # ── Resampling ─────────────────────────────────────────────────────────

    def _resample_22050_to_8000(self, pcm: bytes) -> bytes:
        """Downsample 22050Hz signed 16-bit PCM to 8000Hz."""
        if HAS_AUDIOOP:
            result, self._resample_state = audioop.ratecv(
                pcm, 2, 1, 22050, 8000, self._resample_state
            )
            return result
        elif HAS_NUMPY:
            arr   = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            ratio = 8000 / 22050
            n_out = int(len(arr) * ratio)
            if n_out < 1:
                return b''
            resampled = np.interp(
                np.linspace(0, len(arr) - 1, n_out),
                np.arange(len(arr)),
                arr
            ).astype(np.int16)
            return resampled.tobytes()
        else:
            # Simple decimation fallback — lower quality but works
            arr = pcm[::3]  # ~22050/8000 ≈ 2.75, close enough for voice
            return arr

    def close(self):
        self.key_down()
        try:
            self._sock.close()
        except Exception:
            pass
