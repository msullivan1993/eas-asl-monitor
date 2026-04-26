"""sources/usb_shared.py - Python 3.5 compatible."""
import subprocess


class USBSharedSource(object):
    needs_usrp = False

    def __init__(self, config):
        self.device      = config.get('source_usb_shared', 'device',
                                      fallback='eas_tap')
        self.sample_rate = config.get('source_usb_shared', 'sample_rate',
                                      fallback='22050')

    def get_process(self):
        return subprocess.Popen(
            ['arecord', '-D', self.device,
             '-r', self.sample_rate, '-f', 'S16_LE',
             '-c', '1', '-t', 'raw', '-q', '-'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

    def describe(self):
        return "USB Shared (dsnoop tap: %s @ %sHz)" % (
            self.device, self.sample_rate
        )
