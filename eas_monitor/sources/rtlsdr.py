"""
sources/rtlsdr.py - RTL-SDR Source
Python 3.5 compatible.
"""
import subprocess

NOAA_FREQUENCIES = [
    162400000, 162425000, 162450000, 162475000,
    162500000, 162525000, 162550000
]

GENERIC_SERIALS = {'00000001', '0', '00000000', '1', '', 'rtlsdr', 'RTLSDR'}


class RTLSDRSource(object):
    needs_usrp = True

    def __init__(self, config):
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
        self.is_wideband   = len(self.frequencies) > 1

    @property
    def device_arg(self):
        if self.device_serial and self.device_serial not in GENERIC_SERIALS:
            return self.device_serial
        return str(self.device_index)

    @property
    def primary_frequency(self):
        return self.frequencies[0]

    def get_process(self):
        if self.is_wideband:
            raise RuntimeError(
                "Multi-frequency RTL-SDR requires WidebandPipeline. "
                "Call get_wideband_pipeline() instead."
            )
        return self._rtl_fm_process(self.frequencies[0])

    def get_wideband_pipeline(self, usrp_sinks, recorder=None, log=None):
        from ..pipeline import WidebandPipeline
        return WidebandPipeline(
            frequencies = self.frequencies,
            device_arg  = self.device_arg,
            gain        = self.gain,
            ppm         = self.ppm,
            usrp_sinks  = usrp_sinks,
            recorder    = recorder,
            log         = log
        )

    def _rtl_fm_process(self, frequency):
        cmd = [
            'rtl_fm',
            '-f', str(frequency),
            '-M', 'fm',
            '-s', self.sample_rate,
            '-g', str(self.gain),
            '-p', str(self.ppm),
            '-d', self.device_arg,
        ]
        if self.squelch > 0:
            cmd += ['-l', str(self.squelch)]
        cmd.append('-')
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

    def check_device(self):
        try:
            out = subprocess.run(
                ['rtl_test', '-d', self.device_arg, '-t'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5
            )
            combined = out.stdout + out.stderr
            if 'usb_claim_interface error -6' in combined:
                return False, (
                    "RTL-SDR '%s' is claimed by the DVB kernel driver. "
                    "Fix: sudo modprobe -r dvb_usb_rtl28xxu rtl2832"
                    % self.device_arg
                )
            if 'No supported devices found' in combined:
                return False, (
                    "RTL-SDR device '%s' not found. Check USB connection."
                    % self.device_arg
                )
            return True, "OK"
        except FileNotFoundError:
            return False, "rtl_test not found. Install rtl-sdr package."
        except Exception as e:
            return True, "Check skipped: %s" % e

    def describe(self):
        mode  = 'wideband' if self.is_wideband else 'single'
        freqs = ', '.join('%sMHz' % (f / 1e6) for f in self.frequencies)
        dev   = ('serial=%s' % self.device_serial
                 if self.device_serial and
                 self.device_serial not in GENERIC_SERIALS
                 else 'index=%s' % self.device_index)
        return "RTL-SDR (%s: %s, %s, gain=%s, ppm=%s)" % (
            mode, freqs, dev, self.gain, self.ppm
        )
