"""sources/usb_direct.py - Python 3.5 compatible."""
import subprocess


class USBDirectSource(object):
    needs_usrp = True

    def __init__(self, config):
        self.device      = config.get('source_usb_direct', 'device',
                                      fallback='hw:1,0')
        self.sample_rate = config.get('source_usb_direct', 'sample_rate',
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
        return "USB Direct (dedicated dongle: %s @ %sHz)" % (
            self.device, self.sample_rate
        )
