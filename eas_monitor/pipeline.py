"""
pipeline.py - Audio Pipeline Manager
Python 3.5 compatible.
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

CHUNK_SIZE          = 1024
SOURCE_READ_TIMEOUT = 5.0
TIMEOUT_CHECK       = 30
BLOCK_LAG_WARN      = 0.75


def build_multimon_cmd():
    return ['multimon-ng', '-t', 'raw', '-a', 'EAS', '--timestamp', '-']


def _safe_terminate(proc, name='', log=None):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if log:
                    log.warning("%s did not exit on SIGTERM — sending SIGKILL",
                                name or 'process')
                proc.kill()
                proc.wait(timeout=2)
    except Exception:
        pass


def _safe_close_stdin(proc):
    try:
        if proc and proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass


class SimplePipeline(object):

    def __init__(self, source_proc, usrp_sink=None,
                 recorder=None, log=None):
        self.source    = source_proc
        self.usrp      = usrp_sink
        self.recorder  = recorder
        self.log       = log or logging.getLogger('eas_monitor.pipeline')
        self._running  = False
        self._multimon = None

    def run(self, handler):
        try:
            self._multimon = subprocess.Popen(
                build_multimon_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0
            )
        except OSError:
            raise RuntimeError(
                "multimon-ng not found. "
                "Build from source: sudo bash scripts/build_multimon_ng.sh"
            )

        self._running = True
        writer = threading.Thread(
            target=self._audio_writer,
            name='audio_writer',
            kwargs={'handler': handler}
        )
        writer.daemon = True
        writer.start()
        self._read_multimon(handler)
        self._running = False
        writer.join(timeout=5)
        _safe_terminate(self.source,    'audio source', self.log)
        _safe_terminate(self._multimon, 'multimon-ng',  self.log)

    def _audio_writer(self, handler=None):
        stalls = 0
        try:
            while self._running:
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
                        "No audio data for %ds — source may be hung",
                        int(SOURCE_READ_TIMEOUT * stalls)
                    )
                    if self.source.poll() is not None:
                        self.log.error(
                            "Audio source exited (code %s)",
                            self.source.returncode
                        )
                        break
                    if stalls >= 6:
                        self.log.error(
                            "Audio source unresponsive — forcing restart"
                        )
                        break
                    continue

                stalls = 0
                chunk  = self.source.stdout.read(CHUNK_SIZE)
                if not chunk:
                    self.log.warning("Audio source EOF — disconnected?")
                    break

                try:
                    self._multimon.stdin.write(chunk)
                    self._multimon.stdin.flush()
                except (IOError, OSError) as e:
                    self.log.warning("multimon-ng pipe closed: %s", e)
                    break

                if self.usrp:
                    try:
                        if self.usrp.is_keyed:
                            self.usrp.write_pcm_22050(chunk)
                        else:
                            self.usrp.keepalive()
                    except Exception as e:
                        self.log.debug("USRP write error: %s", e)

                if self.recorder and self.recorder.is_active:
                    try:
                        self.recorder.write(chunk)
                    except Exception as e:
                        self.log.debug("Recorder write error: %s", e)

        except Exception as e:
            self.log.error("Audio writer error: %s", e)
        finally:
            _safe_close_stdin(self._multimon)
            self.log.debug("Audio writer done, multimon-ng stdin closed")

    def _read_multimon(self, handler):
        last_check = time.time()
        try:
            for raw_line in self._multimon.stdout:
                line = raw_line.decode(errors='replace').strip()
                if not line:
                    continue
                self.log.debug("multimon: %s", line)
                if 'ZCZC' in line:
                    try:
                        handler.handle_header(line)
                    except Exception as e:
                        self.log.error("handle_header raised: %s", e)
                elif 'NNNN' in line:
                    try:
                        handler.handle_eom()
                    except Exception as e:
                        self.log.error("handle_eom raised: %s", e)
                now = time.time()
                if now - last_check > TIMEOUT_CHECK:
                    try:
                        handler.check_timeouts()
                    except Exception as e:
                        self.log.error("check_timeouts raised: %s", e)
                    last_check = now
        except Exception as e:
            self.log.error("multimon reader error: %s", e)
        self.log.debug("multimon-ng output reader exited")

    def terminate(self):
        self._running = False
        _safe_close_stdin(self._multimon)
        _safe_terminate(self.source,    'audio source', self.log)
        _safe_terminate(self._multimon, 'multimon-ng',  self.log)


class WidebandPipeline(object):

    SAMPLE_RATE   = 240000
    OUTPUT_RATE   = 22050
    BLOCK_SAMPLES = 16384

    def __init__(self, frequencies, device_arg='0',
                 gain=40, ppm=0, usrp_sinks=None,
                 recorder=None, log=None):
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
        self._block_dur  = self.BLOCK_SAMPLES / float(self.SAMPLE_RATE)
        self.center_freq = (min(self.frequencies) + max(self.frequencies)) // 2
        self.log.info(
            "Wideband: center=%.3fMHz  channels=%s MHz",
            self.center_freq / 1e6,
            [round(f / 1e6, 3) for f in self.frequencies]
        )

    def run(self, handler):
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
        except OSError:
            os.close(rtl_stderr_r)
            os.close(rtl_stderr_w)
            raise RuntimeError("rtl_sdr not found. Install the rtl-sdr package.")

        os.close(rtl_stderr_w)
        self._running = True

        t = threading.Thread(
            target=self._monitor_rtl_stderr,
            args=(rtl_stderr_r,),
            name='rtl_stderr'
        )
        t.daemon = True
        t.start()

        multimons = {}
        for freq in self.frequencies:
            try:
                mm = subprocess.Popen(
                    build_multimon_cmd(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0
                )
                multimons[freq] = mm
            except OSError:
                raise RuntimeError("multimon-ng not found")

        for freq, mm in multimons.items():
            t2 = threading.Thread(
                target=self._read_channel_output,
                args=(freq, mm, handler),
                name='reader_%s' % freq
            )
            t2.daemon = True
            t2.start()

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
                _safe_terminate(mm, 'multimon-ng[%.3fMHz]' % (freq / 1e6),
                                self.log)

    def _iq_loop(self, rtl_proc, multimons):
        bytes_per_block = self.BLOCK_SAMPLES * 2
        warn_threshold  = self._block_dur * BLOCK_LAG_WARN
        slow_count      = 0
        stalls          = 0

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
                    "No I/Q data for %ds — RTL-SDR may be disconnected",
                    int(SOURCE_READ_TIMEOUT * stalls)
                )
                if rtl_proc.poll() is not None:
                    self.log.error("rtl_sdr exited (code %s)",
                                   rtl_proc.returncode)
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

            t_start = time.time()
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
                    self.log.error("Demod [%.3fMHz]: %s", freq / 1e6, e)
                    continue

                mm = multimons.get(freq)
                if mm and mm.poll() is None:
                    try:
                        mm.stdin.write(pcm.tobytes())
                        mm.stdin.flush()
                    except (IOError, OSError):
                        self.log.warning(
                            "multimon-ng [%.3fMHz] pipe broken", freq / 1e6
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
                        self.log.debug("USRP [%.3fMHz]: %s", freq / 1e6, e)

                if self.recorder and self.recorder.is_active:
                    try:
                        self.recorder.write(pcm.tobytes())
                    except Exception:
                        pass

            elapsed = time.time() - t_start
            if elapsed > warn_threshold:
                slow_count += 1
                if slow_count <= 5 or slow_count % 60 == 0:
                    self.log.warning(
                        "Block took %dms (threshold %dms). "
                        "%d channels — consider fewer freqs or faster hardware.",
                        int(elapsed * 1000),
                        int(warn_threshold * 1000),
                        len(self.frequencies)
                    )
            else:
                slow_count = 0

    def _monitor_rtl_stderr(self, fd):
        try:
            with os.fdopen(fd, 'r', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if 'lost' in line.lower():
                        self.log.warning("rtl_sdr USB buffer: %s", line)
                    else:
                        self.log.debug("rtl_sdr: %s", line)
        except Exception:
            pass

    def _demod_channel(self, iq, offset_hz):
        n       = len(iq)
        t       = np.arange(n)
        shifted = iq * np.exp(
            -2j * np.pi * offset_hz * t / self.SAMPLE_RATE
        )
        decim   = self.SAMPLE_RATE // self.OUTPUT_RATE
        d       = shifted[::decim]
        demod   = np.angle(d[1:] * np.conj(d[:-1]))
        return (demod / np.pi * 32767).astype(np.int16)

    @staticmethod
    def _downsample(pcm, from_rate, to_rate):
        n_in  = len(pcm)
        n_out = max(1, int(n_in * to_rate / float(from_rate)))
        return np.interp(
            np.linspace(0, n_in - 1, n_out),
            np.arange(n_in),
            pcm.astype(np.float32)
        ).astype(np.int16)

    def _read_channel_output(self, freq, mm, handler):
        label      = "%.3fMHz" % (freq / 1e6)
        last_check = time.time()
        try:
            for raw_line in mm.stdout:
                line = raw_line.decode(errors='replace').strip()
                if not line:
                    continue
                self.log.debug("[%s] %s", label, line)
                if 'ZCZC' in line:
                    try:
                        handler.handle_header(line)
                    except Exception as e:
                        self.log.error("[%s] handle_header: %s", label, e)
                elif 'NNNN' in line:
                    try:
                        handler.handle_eom()
                    except Exception as e:
                        self.log.error("[%s] handle_eom: %s", label, e)
                now = time.time()
                if now - last_check > TIMEOUT_CHECK:
                    try:
                        handler.check_timeouts()
                    except Exception as e:
                        self.log.error("[%s] check_timeouts: %s", label, e)
                    last_check = now
        except Exception as e:
            self.log.error("[%s] reader error: %s", label, e)
        self.log.debug("[%s] reader exited", label)
