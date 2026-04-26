"""
sources/rtlsdr.py - RTL-SDR Source
=====================================
Software-defined radio source using an RTL-SDR USB dongle.

Two modes selected automatically based on number of configured frequencies:
  Single frequency  → rtl_fm pipe (simple, ~5% CPU, battle-tested)
  Multi frequency   → WidebandPipeline (numpy demod, ~20-35% CPU on Pi 3B)

NOAA Weather Radio frequencies (US):
  162.400  162.425  162.450  162.475  162.500  162.525  162.550 MHz

Requires USRP private node(s) for audio injection into Asterisk.
One USRP node per monitored frequency.

Config section: [source_rtlsdr]
  frequencies    = 162550000         # Single, or comma-separated list
  device_serial  = WXMON01           # Preferred: stable across USB ports
  device_index   = 0                 # Fallback: numeric index (port-dependent)
  gain           = 40                # Tuner gain (0 = auto)
  ppm_correction = 0                 # PPM frequency correction
  sample_rate    = 22050             # Output rate (single freq mode)

Device addressing:
  device_serial is preferred over device_index. Both rtl_fm and rtl_sdr
  accept -d with either a numeric index or a serial string. Using a serial
  written to the dongle's EEPROM (via rtl_eeprom) ensures the same dongle
  is always found regardless of which USB port it is plugged into.

PPM calibration:
  Run 'rtl_test -t' to measure your dongle's PPM offset.
  A poorly calibrated dongle can shift the center frequency by 5-20kHz
  which degrades SAME tone decode quality.
"""

import configparser
import subprocess

# Standard NOAA WX frequencies in Hz
NOAA_FREQUENCIES = [
    162400000, 162425000, 162450000, 162475000,
    162500000, 162525000, 162550000
]

# Serials that ship as factory defaults — not unique, not reliable
GENERIC_SERIALS = {'00000001', '0', '00000000', '1', '', 'rtlsdr', 'RTLSDR'}


class RTLSDRSource:
    """
    RTL-SDR audio source. Automatically selects single or wideband mode.
    Prefers serial-based device addressing over numeric index.
    """

    needs_usrp = True

    def __init__(self, config: configparser.ConfigParser):
        raw_freqs = config.get('source_rtlsdr', 'frequencies',
                               fallback='162550000')
        self.frequencies = [
            int(f.strip()) for f in raw_freqs.split(',')
            if f.strip().isdigit()
        ]
        if not self.frequencies:
            raise ValueError(
                "source_rtlsdr: at least one frequency must be specified"
            )

        # Prefer device_serial over device_index for stable addressing
        self.device_serial = config.get('source_rtlsdr', 'device_serial',
                                        fallback='').strip()
        self.device_index  = config.getint('source_rtlsdr', 'device_index',
                                           fallback=0)
        self.gain          = config.getint('source_rtlsdr', 'gain',
                                           fallback=40)
        self.ppm           = config.getint('source_rtlsdr', 'ppm_correction',
                                           fallback=0)
        self.sample_rate   = config.get('source_rtlsdr', 'sample_rate',
                                        fallback='22050')
        self.squelch       = config.getint('source_rtlsdr', 'squelch',
                                           fallback=0)

        self.is_wideband = len(self.frequencies) > 1

    @property
    def device_arg(self) -> str:
        """
        Returns the -d argument value for rtl_fm / rtl_sdr / rtl_eeprom.
        Uses serial string if available and not a known generic,
        otherwise falls back to numeric index.
        Both rtl_fm and rtl_sdr accept either form natively.
        """
        if self.device_serial and self.device_serial not in GENERIC_SERIALS:
            return self.device_serial
        return str(self.device_index)

    @property
    def primary_frequency(self) -> int:
        return self.frequencies[0]

    def get_process(self) -> subprocess.Popen:
        """
        Returns rtl_fm subprocess for single-frequency mode.
        stdout = raw signed 16-bit PCM mono at sample_rate Hz.
        """
        if self.is_wideband:
            raise RuntimeError(
                "Multi-frequency RTL-SDR requires WidebandPipeline. "
                "Call get_wideband_pipeline() instead."
            )
        return self._rtl_fm_process(self.frequencies[0])

    def get_wideband_pipeline(self, usrp_sinks: dict, recorder=None, log=None):
        """
        Returns a WidebandPipeline for multi-frequency monitoring.
        usrp_sinks: {freq_hz: USRPSink}
        """
        from ..pipeline import WidebandPipeline
        return WidebandPipeline(
            frequencies  = self.frequencies,
            device_arg   = self.device_arg,
            gain         = self.gain,
            ppm          = self.ppm,
            usrp_sinks   = usrp_sinks,
            recorder     = recorder,
            log          = log
        )

    def _rtl_fm_process(self, frequency: int) -> subprocess.Popen:
        """Spawn rtl_fm for a single frequency."""
        cmd = [
            'rtl_fm',
            '-f', str(frequency),
            '-M', 'fm',
            '-s', self.sample_rate,
            '-g', str(self.gain),
            '-p', str(self.ppm),
            '-d', self.device_arg,   # serial string or numeric index
        ]
        if self.squelch > 0:
            cmd += ['-l', str(self.squelch)]
        cmd.append('-')

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

    def describe(self) -> str:
        mode   = 'wideband' if self.is_wideband else 'single'
        freqs  = ', '.join(f"{f/1e6:.3f}MHz" for f in self.frequencies)
        dev    = (f"serial={self.device_serial}"
                  if self.device_serial and self.device_serial not in GENERIC_SERIALS
                  else f"index={self.device_index}")
        return f"RTL-SDR ({mode}: {freqs}, {dev}, gain={self.gain}, ppm={self.ppm})"

    def check_device(self) -> tuple:
        """
        Pre-flight check that the RTL-SDR device is accessible.
        Returns (ok: bool, message: str).

        Common failure: DVB kernel driver (dvb_usb_rtl28xxu) has claimed
        the device — manifests as 'usb_claim_interface error -6' from rtl_fm.
        The installer blacklists this module but it may reappear after a
        kernel update.
        """
        import subprocess
        try:
            out = subprocess.run(
                ['rtl_test', '-d', self.device_arg, '-t'],
                capture_output=True, text=True, timeout=5
            )
            combined = out.stdout + out.stderr

            if 'usb_claim_interface error -6' in combined:
                return False, (
                    f"RTL-SDR device '{self.device_arg}' is claimed by the "
                    f"DVB kernel driver. Fix with:\n"
                    f"  sudo modprobe -r dvb_usb_rtl28xxu rtl2832\n"
                    f"The blacklist in /etc/modprobe.d/rtlsdr-blacklist.conf "
                    f"should prevent this after a reboot."
                )

            if 'No supported devices found' in combined:
                return False, (
                    f"RTL-SDR device '{self.device_arg}' not found. "
                    f"Check USB connection. "
                    f"If using serial, verify with: rtl_eeprom -d {self.device_arg}"
                )

            if 'Failed to open rtlsdr' in combined:
                return False, (
                    f"Could not open RTL-SDR '{self.device_arg}'. "
                    f"Check permissions — add user to 'plugdev' group: "
                    f"sudo usermod -aG plugdev asterisk"
                )

            return True, "OK"

        except FileNotFoundError:
            return False, (
                "rtl_test not found. Ensure rtl-sdr package is installed."
            )
        except subprocess.TimeoutExpired:
            # rtl_test taking >5s usually means it opened fine
            return True, "OK (timeout — device likely accessible)"
        except Exception as e:
            return True, f"Check skipped: {e}"
