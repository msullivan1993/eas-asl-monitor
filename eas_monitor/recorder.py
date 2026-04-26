"""
recorder.py - Alert Audio Recorder
=====================================
Captures audio during SAME alerts, converts to 8kHz ulaw, and saves
to a rotating buffer for DTMF playback via rpt localplay.

Hardening:
  - Checks available disk space before starting each recording
  - Validates audio conversion dependencies at import time and fails
    loudly (not silently) if neither audioop nor numpy is available —
    prevents writing garbage ulaw files that Asterisk would play as noise
  - All file I/O is exception-safe; recording failure never affects the
    main alert pipeline
  - Duplicate start() calls are ignored (not an error)
"""

import json
import logging
import os
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path

# ── Dependency check ────────────────────────────────────────────────────────
# Performed at import time so main() gets a clear error before any audio
# processing is attempted, rather than a confusing failure mid-alert.

HAS_AUDIOOP = False
HAS_NUMPY   = False

try:
    import audioop
    HAS_AUDIOOP = True
except ImportError:
    pass

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    pass

# Expose result for main() to check
CONVERSION_AVAILABLE = HAS_AUDIOOP or HAS_NUMPY
CONVERSION_METHOD    = ('audioop' if HAS_AUDIOOP else
                        'numpy'   if HAS_NUMPY   else
                        'none')

# ── Constants ──────────────────────────────────────────────────────────────

MIN_FREE_MB    = 25     # minimum MB before recording is skipped
SAMPLE_RATE_IN = 22050  # incoming PCM rate (from multimon pipeline)
SAMPLE_RATE_OUT = 8000  # Asterisk ulaw rate


class AlertRecorder:

    DEFAULT_DIR = '/var/lib/eas_monitor/recordings'
    DEFAULT_MAX = 5

    def __init__(self, directory=DEFAULT_DIR, max_recordings=DEFAULT_MAX,
                 log=None):
        self.directory       = Path(directory)
        self.max_recordings  = max_recordings
        self.log             = log or logging.getLogger('eas_monitor.recorder')
        self._active         = False
        self._buffer         = BytesIO()
        self._current_meta   = {}
        self._resample_state = None

        self.directory.mkdir(parents=True, exist_ok=True)

        if not CONVERSION_AVAILABLE:
            self.log.error(
                "ALERT RECORDING DISABLED: neither audioop nor numpy is "
                "available. Install numpy: "
                "pip install numpy --break-system-packages"
            )
        else:
            self.log.debug(f"Recorder ready (conversion: {CONVERSION_METHOD})")

    @property
    def is_active(self) -> bool:
        return self._active

    # ── Recording control ──────────────────────────────────────────────────

    def start(self, event: str, fips_codes: list, callsign: str):
        """Begin recording. Called when SAME header is decoded."""
        if self._active:
            self.log.debug("Recorder already active — ignoring start()")
            return

        if not CONVERSION_AVAILABLE:
            return   # Already logged at init time

        if not self._check_disk_space():
            self.log.error(
                f"Insufficient disk space (< {MIN_FREE_MB}MB) — "
                f"recording skipped"
            )
            return

        self._active         = True
        self._buffer         = BytesIO()
        self._resample_state = None
        self._current_meta   = {
            'event':    event,
            'fips':     fips_codes,
            'callsign': callsign,
            'started':  datetime.now().isoformat()
        }
        self.log.debug(f"Recording started: {event} {fips_codes}")

    def write(self, pcm_22050: bytes):
        """Accumulate incoming 22050Hz PCM, converted to 8kHz ulaw."""
        if not self._active:
            return
        try:
            ulaw = self._pcm_to_ulaw(pcm_22050)
            if ulaw:
                self._buffer.write(ulaw)
        except Exception as e:
            self.log.debug(f"Recorder write error: {e}")

    def stop(self):
        """
        Finalize and save the recording.
        Returns path stem (no extension) for rpt localplay, or None.
        """
        if not self._active:
            return None
        self._active = False

        audio = self._buffer.getvalue()
        self._buffer = BytesIO()

        if len(audio) < 1000:
            self.log.debug("Recording too short — discarding")
            return None

        ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
        event    = self._current_meta.get('event', 'UNK')
        filename = f"alert_{ts}_{event}.ulaw"
        filepath = self.directory / filename

        try:
            filepath.write_bytes(audio)
            self.log.info(
                f"Alert recorded: {filename}  "
                f"({len(audio):,} bytes ulaw)"
            )
        except Exception as e:
            self.log.error(f"Failed to save recording: {e}")
            return None

        self._current_meta.update({
            'stopped':  datetime.now().isoformat(),
            'filename': filename,
            'bytes':    len(audio)
        })

        try:
            self._update_index(self._current_meta)
            self._update_playback_symlinks()
        except Exception as e:
            self.log.error(f"Failed to update recording index: {e}")

        return str(filepath.with_suffix(''))

    def discard(self):
        """Abandon current recording without saving (e.g. on restart)."""
        self._active = False
        self._buffer = BytesIO()

    # ── Disk space ─────────────────────────────────────────────────────────

    def _check_disk_space(self) -> bool:
        """Return True if at least MIN_FREE_MB MB is free."""
        try:
            stat     = os.statvfs(str(self.directory))
            free_mb  = stat.f_frsize * stat.f_bavail / (1024 * 1024)
            if free_mb < MIN_FREE_MB:
                self.log.warning(
                    f"Low disk space: {free_mb:.1f}MB free in "
                    f"{self.directory} (minimum {MIN_FREE_MB}MB)"
                )
                return False
            return True
        except Exception:
            return True   # Can't check — allow recording

    # ── Index management ───────────────────────────────────────────────────

    def _load_index(self) -> list:
        index_path = self.directory / 'index.json'
        if index_path.exists():
            try:
                return json.loads(index_path.read_text())
            except Exception:
                return []
        return []

    def _update_index(self, meta: dict):
        recordings = self._load_index()
        recordings.append(meta)

        while len(recordings) > self.max_recordings:
            old      = recordings.pop(0)
            old_path = self.directory / old.get('filename', '')
            try:
                old_path.unlink(missing_ok=True)
                self.log.debug(f"Rotated out: {old_path.name}")
            except Exception:
                pass

        try:
            (self.directory / 'index.json').write_text(
                json.dumps(recordings, indent=2)
            )
        except Exception as e:
            self.log.error(f"Failed to update index: {e}")

    def _update_playback_symlinks(self):
        """
        Maintain recent_N.ulaw symlinks for DTMF playback.
        recent_1.ulaw = most recent alert.
        """
        recordings = self._load_index()
        for i in range(1, self.max_recordings + 1):
            link = self.directory / f"recent_{i}.ulaw"
            try:
                link.unlink(missing_ok=True)
            except Exception:
                pass
            idx = len(recordings) - i
            if idx >= 0:
                target = self.directory / recordings[idx].get('filename', '')
                if target.exists():
                    try:
                        link.symlink_to(target)
                    except Exception as e:
                        self.log.debug(f"Symlink {link.name}: {e}")

    # ── Audio conversion ───────────────────────────────────────────────────

    def _pcm_to_ulaw(self, pcm_22050: bytes) -> bytes:
        """Convert 22050Hz signed 16-bit PCM to 8kHz ulaw."""
        if HAS_AUDIOOP:
            try:
                pcm_8k, self._resample_state = audioop.ratecv(
                    pcm_22050, 2, 1,
                    SAMPLE_RATE_IN, SAMPLE_RATE_OUT,
                    self._resample_state
                )
                return audioop.lin2ulaw(pcm_8k, 2)
            except Exception as e:
                self.log.debug(f"audioop error: {e}")
                return b''

        elif HAS_NUMPY:
            try:
                arr   = np.frombuffer(pcm_22050, dtype=np.int16).astype(np.float32)
                ratio = SAMPLE_RATE_OUT / SAMPLE_RATE_IN
                n_out = max(1, int(len(arr) * ratio))
                resampled = np.interp(
                    np.linspace(0, len(arr) - 1, n_out),
                    np.arange(len(arr)),
                    arr
                ).astype(np.int16)
                return self._numpy_lin2ulaw(resampled)
            except Exception as e:
                self.log.debug(f"numpy conversion error: {e}")
                return b''

        return b''   # CONVERSION_AVAILABLE is False — start() already blocked us

    @staticmethod
    def _numpy_lin2ulaw(samples) -> bytes:
        """ITU-T G.711 ulaw encoding via numpy."""
        import numpy as np
        BIAS  = 0x84
        CLIP  = 32635
        exp_lut = [0,0,1,1,2,2,2,2,3,3,3,3,3,3,3,3,4,4,4,4,4,4,4,4,
                   4,4,4,4,4,4,4,4,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,
                   5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,6,6,6,6,6,6,6,6,
                   6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
                   6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
                   6,6,6,6,6,6,6,6,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
                   7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
                   7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
                   7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
                   7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
                   7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7]
        exp_arr  = np.array(exp_lut, dtype=np.uint8)
        sign     = np.where(samples < 0, np.uint8(0x80), np.uint8(0))
        s        = np.clip(np.abs(samples.astype(np.int32)), 0, CLIP)
        s_biased = (s + BIAS).astype(np.int32)
        exponent = exp_arr[(s_biased >> 7) & 0xFF]
        mantissa = ((s_biased >> (exponent.astype(np.int32) + 3)) & 0x0F
                    ).astype(np.uint8)
        return np.bitwise_not(sign | (exponent << 4) | mantissa
                              ).astype(np.uint8).tobytes()

    # ── Playback info ──────────────────────────────────────────────────────

    def get_recent_path(self, n=1):
        recordings = self._load_index()
        if not recordings:
            return None
        idx = len(recordings) - n
        if idx < 0:
            return None
        path = self.directory / recordings[idx].get('filename', '')
        return str(path.with_suffix('')) if path.exists() else None

    def get_summary(self) -> list:
        return self._load_index()
