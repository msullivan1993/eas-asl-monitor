"""
usrp_sink.py - USRP UDP Audio Sink
Python 3.5 compatible.
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

USRP_MAGIC         = b'USRP'
USRP_HEADER_FMT    = '>4sIIIIII'
USRP_HEADER_SIZE   = struct.calcsize(USRP_HEADER_FMT)
USRP_FRAME_SAMPLES = 160
USRP_FRAME_BYTES   = USRP_FRAME_SAMPLES * 2
KEEPALIVE_INTERVAL = 5.0


class USRPSink(object):

    def __init__(self, tx_port, rx_port, host='127.0.0.1', log=None):
        self.host    = host
        self.tx_port = tx_port
        self.rx_port = rx_port
        self.log     = log or logging.getLogger('eas_monitor.usrp')
        self._seq    = 0
        self._keyed  = False
        self._last_keepalive  = 0.0
        self._resample_state  = None

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(('127.0.0.1', rx_port))
        self._addr = (host, tx_port)

    def _make_packet(self, audio_320, keyed):
        header = struct.pack(
            USRP_HEADER_FMT,
            USRP_MAGIC, self._seq, 0, 0, 0,
            1 if keyed else 0, 0
        )
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return header + audio_320

    def _send_packet(self, audio_320, keyed):
        try:
            pkt = self._make_packet(audio_320, keyed)
            self._sock.sendto(pkt, self._addr)
        except Exception as e:
            self.log.error("USRP send error: %s", e)

    @property
    def is_keyed(self):
        return self._keyed

    def key_up(self):
        if not self._keyed:
            self._keyed = True
            self.log.debug("USRP key up -> %s:%s", self.host, self.tx_port)
            self._send_packet(b'\x00' * USRP_FRAME_BYTES, keyed=True)
            self._last_keepalive = time.time()

    def key_down(self):
        if self._keyed:
            self._keyed = False
            self.log.debug("USRP key down -> %s:%s", self.host, self.tx_port)
            self._send_packet(b'\x00' * USRP_FRAME_BYTES, keyed=False)

    def keepalive(self):
        if self._keyed:
            now = time.time()
            if now - self._last_keepalive > KEEPALIVE_INTERVAL:
                self._send_packet(b'\x00' * USRP_FRAME_BYTES, keyed=True)
                self._last_keepalive = now

    def write_pcm_22050(self, pcm_22050):
        if not self._keyed:
            return
        pcm_8k = self._resample_22050_to_8000(pcm_22050)
        if not pcm_8k:
            return
        for i in range(0, len(pcm_8k) - USRP_FRAME_BYTES + 1,
                       USRP_FRAME_BYTES):
            frame = pcm_8k[i:i + USRP_FRAME_BYTES]
            if len(frame) == USRP_FRAME_BYTES:
                self._send_packet(frame, keyed=True)
        self._last_keepalive = time.time()

    def write_pcm_8000(self, pcm_8000):
        if not self._keyed:
            return
        for i in range(0, len(pcm_8000) - USRP_FRAME_BYTES + 1,
                       USRP_FRAME_BYTES):
            frame = pcm_8000[i:i + USRP_FRAME_BYTES]
            if len(frame) == USRP_FRAME_BYTES:
                self._send_packet(frame, keyed=True)
        self._last_keepalive = time.time()

    def _resample_22050_to_8000(self, pcm):
        if HAS_AUDIOOP:
            result, self._resample_state = audioop.ratecv(
                pcm, 2, 1, 22050, 8000, self._resample_state
            )
            return result
        elif HAS_NUMPY:
            arr   = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            ratio = 8000.0 / 22050.0
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
            return pcm[::3]

    def close(self):
        self.key_down()
        try:
            self._sock.close()
        except Exception:
            pass
