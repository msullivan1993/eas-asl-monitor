#!/usr/bin/env python3
"""
usrp_source.py - Receive USRP UDP audio from Asterisk, write PCM to stdout
===========================================================================
Launched as a subprocess by USRPNodeSource. Binds to a UDP port, receives
USRP packets from Asterisk's chan_usrp, strips the 28-byte header, resamples
8kHz -> 22050Hz, and writes raw S16LE PCM to stdout for multimon-ng.

Usage: python3 usrp_source.py <rx_port>

Python 3.5 compatible.
"""

import socket
import struct
import sys

try:
    import audioop
    HAS_AUDIOOP = True
except ImportError:
    HAS_AUDIOOP = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

USRP_MAGIC       = b'USRP'
USRP_HEADER_FMT  = '>4sIIIIII'
USRP_HEADER_SIZE = struct.calcsize(USRP_HEADER_FMT)
USRP_AUDIO_SIZE  = 320          # 160 samples x 2 bytes @ 8kHz
SILENCE_8K       = b'\x00' * USRP_AUDIO_SIZE
TIMEOUT_SECS     = 1.0          # send silence on timeout to keep pipeline alive


def resample_8k_to_22050(pcm_8k, state=None):
    """Resample 8kHz S16LE PCM to 22050Hz."""
    if HAS_AUDIOOP:
        out, new_state = audioop.ratecv(pcm_8k, 2, 1, 8000, 22050, state)
        return out, new_state
    elif HAS_NUMPY:
        arr   = np.frombuffer(pcm_8k, dtype=np.int16).astype(np.float32)
        n_out = int(len(arr) * 22050.0 / 8000.0)
        if n_out < 1:
            return b'', None
        out = np.interp(
            np.linspace(0, len(arr) - 1, n_out),
            np.arange(len(arr)),
            arr
        ).astype(np.int16)
        return out.tobytes(), None
    else:
        # Crude 8->22: repeat each sample ~2.756x using integer steps
        # Not high quality but functional
        arr = struct.unpack('<%dh' % (len(pcm_8k) // 2), pcm_8k)
        out = []
        acc = 0.0
        for s in arr:
            acc += 22050.0 / 8000.0
            while acc >= 1.0:
                out.append(s)
                acc -= 1.0
        return struct.pack('<%dh' % len(out), *out), None


def main():
    rx_port = int(sys.argv[1]) if len(sys.argv) > 1 else 34001

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', rx_port))
    sock.settimeout(TIMEOUT_SECS)

    out = sys.stdout.buffer if hasattr(sys.stdout, 'buffer') else sys.stdout
    resample_state = None
    silence_22k, _ = resample_8k_to_22050(SILENCE_8K)

    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            # Keep pipeline alive with silence during quiet periods
            out.write(silence_22k)
            out.flush()
            continue
        except Exception as e:
            sys.stderr.write("usrp_source recv error: %s\n" % e)
            break

        if len(data) < USRP_HEADER_SIZE + USRP_AUDIO_SIZE:
            continue

        # Validate USRP magic
        if data[:4] != USRP_MAGIC:
            continue

        # Extract audio payload (8kHz S16LE mono)
        audio_8k = data[USRP_HEADER_SIZE:USRP_HEADER_SIZE + USRP_AUDIO_SIZE]

        # Resample to 22050Hz for multimon-ng
        audio_22k, resample_state = resample_8k_to_22050(audio_8k, resample_state)

        out.write(audio_22k)
        out.flush()


if __name__ == '__main__':
    main()
