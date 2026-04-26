"""
recorder.py - Alert Audio Recorder
Python 3.5 compatible.
"""

import json
import logging
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path

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

CONVERSION_AVAILABLE = HAS_AUDIOOP or HAS_NUMPY
CONVERSION_METHOD    = ('audioop' if HAS_AUDIOOP else
                        'numpy'   if HAS_NUMPY   else
                        'none')

MIN_FREE_MB     = 25
SAMPLE_RATE_IN  = 22050
SAMPLE_RATE_OUT = 8000


class AlertRecorder(object):

    DEFAULT_DIR = '/var/lib/eas_monitor/recordings'
    DEFAULT_MAX = 5

    def __init__(self, directory=None, max_recordings=DEFAULT_MAX, log=None):
        self.directory       = Path(directory or self.DEFAULT_DIR)
        self.max_recordings  = max_recordings
        self.log             = log or logging.getLogger('eas_monitor.recorder')
        self._active         = False
        self._buffer         = BytesIO()
        self._current_meta   = {}
        self._resample_state = None

        self.directory.mkdir(parents=True, exist_ok=True)

        if not CONVERSION_AVAILABLE:
            self.log.error(
                "ALERT RECORDING DISABLED: neither audioop nor numpy available. "
                "Install numpy: pip install numpy --break-system-packages"
            )
        else:
            self.log.debug("Recorder ready (conversion: %s)", CONVERSION_METHOD)

    @property
    def is_active(self):
        return self._active

    def start(self, event, fips_codes, callsign):
        if self._active:
            return
        if not CONVERSION_AVAILABLE:
            return
        if not self._check_disk_space():
            self.log.error(
                "Insufficient disk space (< %dMB) — recording skipped",
                MIN_FREE_MB
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
        self.log.debug("Recording started: %s %s", event, fips_codes)

    def write(self, pcm_22050):
        if not self._active:
            return
        try:
            ulaw = self._pcm_to_ulaw(pcm_22050)
            if ulaw:
                self._buffer.write(ulaw)
        except Exception as e:
            self.log.debug("Recorder write error: %s", e)

    def stop(self):
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
        filename = "alert_%s_%s.ulaw" % (ts, event)
        filepath = self.directory / filename
        try:
            filepath.write_bytes(audio)
            self.log.info("Alert recorded: %s  (%d bytes ulaw)",
                          filename, len(audio))
        except Exception as e:
            self.log.error("Failed to save recording: %s", e)
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
            self.log.error("Failed to update recording index: %s", e)
        return str(filepath.with_suffix(''))

    def discard(self):
        self._active = False
        self._buffer = BytesIO()

    def _check_disk_space(self):
        try:
            stat    = os.statvfs(str(self.directory))
            free_mb = stat.f_frsize * stat.f_bavail / (1024 * 1024)
            if free_mb < MIN_FREE_MB:
                self.log.warning(
                    "Low disk space: %.1fMB free (minimum %dMB)",
                    free_mb, MIN_FREE_MB
                )
                return False
            return True
        except Exception:
            return True

    def _load_index(self):
        index_path = self.directory / 'index.json'
        if index_path.exists():
            try:
                return json.loads(index_path.read_text())
            except Exception:
                return []
        return []

    def _update_index(self, meta):
        recordings = self._load_index()
        recordings.append(meta)
        while len(recordings) > self.max_recordings:
            old      = recordings.pop(0)
            old_path = self.directory / old.get('filename', '')
            try:
                old_path.unlink()
                self.log.debug("Rotated out: %s", old_path.name)
            except Exception:
                pass
        try:
            (self.directory / 'index.json').write_text(
                json.dumps(recordings, indent=2)
            )
        except Exception as e:
            self.log.error("Failed to update index: %s", e)

    def _update_playback_symlinks(self):
        recordings = self._load_index()
        for i in range(1, self.max_recordings + 1):
            link = self.directory / ("recent_%d.ulaw" % i)
            try:
                link.unlink()
            except Exception:
                pass
            idx = len(recordings) - i
            if idx >= 0:
                target = self.directory / recordings[idx].get('filename', '')
                if target.exists():
                    try:
                        link.symlink_to(target)
                    except Exception as e:
                        self.log.debug("Symlink recent_%d: %s", i, e)

    def _pcm_to_ulaw(self, pcm_22050):
        if HAS_AUDIOOP:
            try:
                pcm_8k, self._resample_state = audioop.ratecv(
                    pcm_22050, 2, 1,
                    SAMPLE_RATE_IN, SAMPLE_RATE_OUT,
                    self._resample_state
                )
                return audioop.lin2ulaw(pcm_8k, 2)
            except Exception as e:
                self.log.debug("audioop error: %s", e)
                return b''
        elif HAS_NUMPY:
            try:
                arr   = np.frombuffer(pcm_22050,
                                      dtype=np.int16).astype(np.float32)
                ratio = float(SAMPLE_RATE_OUT) / SAMPLE_RATE_IN
                n_out = max(1, int(len(arr) * ratio))
                resampled = np.interp(
                    np.linspace(0, len(arr) - 1, n_out),
                    np.arange(len(arr)),
                    arr
                ).astype(np.int16)
                return self._numpy_lin2ulaw(resampled)
            except Exception as e:
                self.log.debug("numpy conversion error: %s", e)
                return b''
        return b''

    @staticmethod
    def _numpy_lin2ulaw(samples):
        import numpy as np
        BIAS = 0x84
        CLIP = 32635
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
        return np.bitwise_not(
            sign | (exponent << 4) | mantissa
        ).astype(np.uint8).tobytes()

    def get_recent_path(self, n=1):
        recordings = self._load_index()
        if not recordings:
            return None
        idx = len(recordings) - n
        if idx < 0:
            return None
        path = self.directory / recordings[idx].get('filename', '')
        return str(path.with_suffix('')) if path.exists() else None

    def get_summary(self):
        return self._load_index()
