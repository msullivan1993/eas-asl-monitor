"""
pipeline.py - Audio Pipeline Manager
======================================
Routes audio from source processes to multimon-ng, USRP sink,
and the alert recorder.

Hardening:
  - select() timeout on source read — detects hung/frozen audio device
    without blocking the writer thread forever
  - Explicit multimon-ng stdin close on source EOF — ensures multimon-ng
    exits cleanly and unblocks the output reader, allowing the restart loop
    to fire
  - All handler calls wrapped in try/except — a bad FIPS decode or AMI
    hiccup never kills the pipeline
  - Wideband: per-block timing warning when numpy can't keep up (Pi 3B)
  - Wideband: rtl_sdr stderr monitoring for I/Q buffer overflow
  - All subprocess cleanup is idempotent and exception-safe
"""

import logging
import os
import select
import subprocess
import threading
import time

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# Audio chunk size — ~23ms at 22050Hz 16-bit mono
CHUNK_SIZE = 1024

# How long to wait for audio data before assuming the source is hung.
# arecord/rtl_fm should produce data every ~23ms; 5s is very conservative.
SOURCE_READ_TIMEOUT = 5.0  # seconds

# How often to run the purge-time watchdog
TIMEOUT_CHECK = 30  # seconds

# Wideband: warn if a processing block takes longer than this fraction of
# block duration (block ~68ms at 16384 samples / 240kHz)
BLOCK_LAG_WARN = 0.75


def build_multimon_cmd() -> list:
    return ['multimon-ng', '-t', 'raw', '-a', 'EAS', '--timestamp', '-']


def _safe_terminate(proc, name: str = '', log=None):
    """Terminate a subprocess, SIGKILL if it doesn't exit within 3s."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if log:
                    log.warning(
                        f"{name or 'process'} did not exit on SIGTERM "
                        f"— sending SIGKILL"
                    )
                proc.kill()
                proc.wait(timeout=2)
    except Exception:
        pass


def _safe_close_stdin(proc):
    """Close a process stdin pipe without raising."""
    try:
        if proc and proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass


# ── Simple Pipeline ────────────────────────────────────────────────────────

class SimplePipeline:
    """
    Single audio source → multimon-ng → alert handler.

    The audio writer thread reads from source stdout using select() so a
    hung or disconnected device is detected within SOURCE_READ_TIMEOUT
    seconds rather than blocking forever.

    When the source exits, errors, or times out, the writer closes
    multimon-ng stdin. multimon-ng then flushes its buffer and exits,
    which closes its stdout, which unblocks the output reader, which
    returns to the restart loop in main(). Every exit path goes through
    this chain — there is no way for the pipeline to get permanently stuck.
    """

    def __init__(self, source_proc, usrp_sink=None, recorder=None,
                 log=None):
        self.source   = source_proc
        self.usrp     = usrp_sink
        self.recorder = recorder
        self.log      = log or logging.getLogger('eas_monitor.pipeline')
        self._running = False
        self._multimon = None

    def run(self, handler):
        """Run the pipeline. Blocks until source exits or error."""
        try:
            self._multimon = subprocess.Popen(
                build_multimon_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                bufsize=0
            )
        except FileNotFoundError:
            raise RuntimeError(
                "multimon-ng not found. "
                "Install it or build from source (HamVoIP: "
                "scripts/build_multimon_ng.sh)"
            )

        self._running = True

        writer = threading.Thread(
            target=self._audio_writer,
            name='audio_writer',
            daemon=True
        )
        writer.start()

        # Blocks here until multimon-ng exits
        self._read_multimon(handler)

        self._running = False
        writer.join(timeout=5)

        _safe_terminate(self.source,    'audio source', self.log)
        _safe_terminate(self._multimon, 'multimon-ng',  self.log)

    def _audio_writer(self):
        """
        Read audio from source (with select timeout) and distribute
        to multimon-ng, USRP, and recorder.

        On any exit path, closes multimon-ng stdin to trigger clean shutdown.
        """
        stalls = 0
        try:
            while self._running:

                # select() — returns immediately if data ready, else waits timeout
                try:
                    ready, _, _ = select.select(
                        [self.source.stdout], [], [], SOURCE_READ_TIMEOUT
                    )
                except (ValueError, OSError):
                    self.log.debug("Source stdout closed")
                    break

                if not ready:
                    stalls += 1
                    self.log.warning(
                        f"No audio data for "
                        f"{SOURCE_READ_TIMEOUT * stalls:.0f}s "
                        f"— source may be hung or disconnected"
                    )
                    if self.source.poll() is not None:
                        self.log.error(
                            f"Audio source exited "
                            f"(code {self.source.returncode})"
                        )
                        break
                    if stalls >= 6:  # 30 seconds
                        self.log.error(
                            "Audio source unresponsive — forcing restart"
                        )
                        break
                    continue

                stalls = 0
                chunk  = self.source.stdout.read(CHUNK_SIZE)
                if not chunk:
                    self.log.warning(
                        "Audio source EOF — device disconnected?"
                    )
                    break

                # Feed multimon-ng
                try:
                    self._multimon.stdin.write(chunk)
                    self._multimon.stdin.flush()
                except (BrokenPipeError, OSError) as e:
                    self.log.warning(f"multimon-ng pipe closed: {e}")
                    break

                # Feed USRP if keyed
                if self.usrp:
                    try:
                        if self.usrp.is_keyed:
                            self.usrp.write_pcm_22050(chunk)
                        else:
                            self.usrp.keepalive()
                    except Exception as e:
                        self.log.debug(f"USRP write error: {e}")

                # Feed recorder if active
                if self.recorder and self.recorder.is_active:
                    try:
                        self.recorder.write(chunk)
                    except Exception as e:
                        self.log.debug(f"Recorder write error: {e}")

        except Exception as e:
            self.log.error(f"Audio writer error: {e}")

        finally:
            # CRITICAL: This close() is what makes the pipeline exit cleanly.
            # It signals multimon-ng to flush its buffer and exit, which
            # closes its stdout, which ends the for-loop in _read_multimon.
            _safe_close_stdin(self._multimon)
            self.log.debug("Audio writer done, multimon-ng stdin closed")

    def _read_multimon(self, handler):
        """
        Read multimon-ng stdout. Blocks until multimon-ng exits.
        All handler calls are wrapped so a decode error or AMI failure
        cannot kill this loop.
        """
        last_check = time.time()
        try:
            for raw_line in self._multimon.stdout:
                line = raw_line.decode(errors='replace').strip()
                if not line:
                    continue

                self.log.debug(f"multimon: {line}")

                if 'ZCZC' in line:
                    try:
                        handler.handle_header(line)
                    except Exception as e:
                        self.log.error(f"handle_header raised: {e}")

                elif 'NNNN' in line:
                    try:
                        handler.handle_eom()
                    except Exception as e:
                        self.log.error(f"handle_eom raised: {e}")

                now = time.time()
                if now - last_check > TIMEOUT_CHECK:
                    try:
                        handler.check_timeouts()
                    except Exception as e:
                        self.log.error(f"check_timeouts raised: {e}")
                    last_check = now

        except Exception as e:
            self.log.error(f"multimon reader error: {e}")

        self.log.debug("multimon-ng output reader exited")

    def terminate(self):
        """Graceful external termination (e.g. on SIGTERM)."""
        self._running = False
        _safe_close_stdin(self._multimon)
        _safe_terminate(self.source,    'audio source', self.log)
        _safe_terminate(self._multimon, 'multimon-ng',  self.log)


# ── Wideband RTL-SDR Pipeline ──────────────────────────────────────────────

class WidebandPipeline:
    """
    Multiple NOAA WX channels from one RTL-SDR via numpy FM demodulation.

    Hardening:
      - Per-block timing: warns when processing exceeds BLOCK_LAG_WARN
        fraction of block duration (Pi 3B with 3+ channels)
      - rtl_sdr stderr monitor: logs USB buffer overflow warnings
      - select() timeout: detects RTL-SDR disconnect cleanly
      - Per-channel error isolation: one bad demod doesn't stop others
      - All subprocess cleanup runs even if exceptions occur
    """

    SAMPLE_RATE   = 240000
    OUTPUT_RATE   = 22050
    BLOCK_SAMPLES = 16384   # ~68ms per block at 240kHz

    def __init__(self, frequencies, device_arg='0',
                 gain=40, ppm=0, usrp_sinks=None, recorder=None, log=None):
        if not HAS_NUMPY:
            raise RuntimeError(
                "numpy is required for wideband RTL-SDR mode. "
                "Install with: pip install numpy --break-system-packages"
            )

        self.frequencies = [int(f) for f in frequencies]
        self.device_arg  = str(device_arg)
        self.gain        = gain
        self.ppm         = ppm
        self.usrp_sinks  = usrp_sinks or {}
        self.recorder    = recorder
        self.log         = log or logging.getLogger('eas_monitor.wideband')
        self._running    = False
        self._block_dur  = self.BLOCK_SAMPLES / self.SAMPLE_RATE  # ~0.068s

        self.center_freq = (min(self.frequencies) + max(self.frequencies)) // 2
        self.log.info(
            f"Wideband: center={self.center_freq/1e6:.3f}MHz  "
            f"channels={[f'{f/1e6:.3f}' for f in self.frequencies]}MHz"
        )

    def run(self, handler):
        # Pipe for rtl_sdr stderr (overflow detection)
        rtl_stderr_r, rtl_stderr_w = os.pipe()

        try:
            rtl_proc = subprocess.Popen(
                ['rtl_sdr',
                 '-f', str(self.center_freq),
                 '-s', str(self.SAMPLE_RATE),
                 '-g', str(self.gain),
                 '-p', str(self.ppm),
                 '-d', self.device_arg, '-'],
                stdout=subprocess.PIPE,
                stderr=rtl_stderr_w
            )
        except FileNotFoundError:
            os.close(rtl_stderr_r)
            os.close(rtl_stderr_w)
            raise RuntimeError(
                "rtl_sdr not found. Install the rtl-sdr package."
            )

        os.close(rtl_stderr_w)   # parent only needs read end
        self._running = True

        threading.Thread(
            target=self._monitor_rtl_stderr,
            args=(rtl_stderr_r,),
            name='rtl_stderr', daemon=True
        ).start()

        multimons = {}
        for freq in self.frequencies:
            try:
                mm = subprocess.Popen(
                    build_multimon_cmd(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=False, bufsize=0
                )
                multimons[freq] = mm
            except FileNotFoundError:
                raise RuntimeError("multimon-ng not found")

        for freq, mm in multimons.items():
            threading.Thread(
                target=self._read_channel_output,
                args=(freq, mm, handler),
                name=f'reader_{freq}', daemon=True
            ).start()

        try:
            self._iq_loop(rtl_proc, multimons)
        finally:
            self._running = False
            try:
                os.close(rtl_stderr_r)
            except Exception:
                pass
            _safe_terminate(rtl_proc, 'rtl_sdr', self.log)
            for freq, mm in multimons.items():
                _safe_close_stdin(mm)
                _safe_terminate(
                    mm, f'multimon-ng[{freq/1e6:.3f}MHz]', self.log
                )

    def _iq_loop(self, rtl_proc, multimons):
        bytes_per_block  = self.BLOCK_SAMPLES * 2
        warn_threshold   = self._block_dur * BLOCK_LAG_WARN
        slow_count       = 0
        stalls           = 0

        while self._running:
            try:
                ready, _, _ = select.select(
                    [rtl_proc.stdout], [], [], SOURCE_READ_TIMEOUT
                )
            except (ValueError, OSError):
                break

            if not ready:
                stalls += 1
                self.log.warning(
                    f"No I/Q data for {SOURCE_READ_TIMEOUT*stalls:.0f}s "
                    f"— RTL-SDR may be disconnected"
                )
                if rtl_proc.poll() is not None:
                    self.log.error(
                        f"rtl_sdr exited (code {rtl_proc.returncode})"
                    )
                    break
                if stalls >= 6:
                    self.log.error("rtl_sdr unresponsive — forcing restart")
                    break
                continue

            stalls = 0
            raw    = rtl_proc.stdout.read(bytes_per_block)
            if not raw or len(raw) < bytes_per_block:
                self.log.warning("rtl_sdr short read — device disconnected?")
                break

            t_start = time.monotonic()

            samples = (
                np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
            ) / 127.5
            iq = samples[0::2] + 1j * samples[1::2]

            for freq in self.frequencies:
                if not self._running:
                    break
                try:
                    pcm = self._demod_channel(iq, freq - self.center_freq)
                except Exception as e:
                    self.log.error(f"Demod [{freq/1e6:.3f}MHz]: {e}")
                    continue

                mm = multimons.get(freq)
                if mm and mm.poll() is None:
                    try:
                        mm.stdin.write(pcm.tobytes())
                        mm.stdin.flush()
                    except (BrokenPipeError, OSError):
                        self.log.warning(
                            f"multimon-ng [{freq/1e6:.3f}MHz] pipe broken"
                        )

                usrp = self.usrp_sinks.get(freq)
                if usrp:
                    try:
                        if usrp.is_keyed:
                            pcm8 = self._downsample(
                                pcm, self.OUTPUT_RATE, 8000
                            )
                            usrp.write_pcm_8000(pcm8.tobytes())
                        else:
                            usrp.keepalive()
                    except Exception as e:
                        self.log.debug(f"USRP [{freq/1e6:.3f}MHz]: {e}")

                if self.recorder and self.recorder.is_active:
                    try:
                        self.recorder.write(pcm.tobytes())
                    except Exception:
                        pass

            elapsed = time.monotonic() - t_start
            if elapsed > warn_threshold:
                slow_count += 1
                if slow_count <= 5 or slow_count % 60 == 0:
                    self.log.warning(
                        f"Block took {elapsed*1000:.0f}ms "
                        f"(threshold {warn_threshold*1000:.0f}ms). "
                        f"{len(self.frequencies)} channels — "
                        f"consider fewer frequencies or faster hardware."
                    )
            else:
                slow_count = 0

    def _monitor_rtl_stderr(self, fd):
        """Log rtl_sdr stderr — especially USB buffer overflow lines."""
        try:
            with os.fdopen(fd, 'r', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if 'lost' in line.lower():
                        self.log.warning(f"rtl_sdr USB buffer: {line}")
                    else:
                        self.log.debug(f"rtl_sdr: {line}")
        except Exception:
            pass

    def _demod_channel(self, iq, offset_hz):
        n       = len(iq)
        t       = np.arange(n)
        shifted = iq * np.exp(-2j * np.pi * offset_hz * t / self.SAMPLE_RATE)
        decim   = self.SAMPLE_RATE // self.OUTPUT_RATE
        d       = shifted[::decim]
        demod   = np.angle(d[1:] * np.conj(d[:-1]))
        return (demod / np.pi * 32767).astype(np.int16)

    @staticmethod
    def _downsample(pcm, from_rate, to_rate):
        n_in  = len(pcm)
        n_out = max(1, int(n_in * to_rate / from_rate))
        return np.interp(
            np.linspace(0, n_in - 1, n_out),
            np.arange(n_in),
            pcm.astype(np.float32)
        ).astype(np.int16)

    def _read_channel_output(self, freq, mm, handler):
        label      = f"{freq/1e6:.3f}MHz"
        last_check = time.time()
        try:
            for raw_line in mm.stdout:
                line = raw_line.decode(errors='replace').strip()
                if not line:
                    continue
                self.log.debug(f"[{label}] {line}")
                if 'ZCZC' in line:
                    try:
                        handler.handle_header(line)
                    except Exception as e:
                        self.log.error(f"[{label}] handle_header: {e}")
                elif 'NNNN' in line:
                    try:
                        handler.handle_eom()
                    except Exception as e:
                        self.log.error(f"[{label}] handle_eom: {e}")
                now = time.time()
                if now - last_check > TIMEOUT_CHECK:
                    try:
                        handler.check_timeouts()
                    except Exception as e:
                        self.log.error(f"[{label}] check_timeouts: {e}")
                    last_check = now
        except Exception as e:
            self.log.error(f"[{label}] reader error: {e}")
        self.log.debug(f"[{label}] reader exited")
