"""
sources/usb_shared.py - USB Shared Source (Existing ASL Node Hardware)
========================================================================
Audio source for users whose weather radio is already connected to their
AllStarLink node via a RIM-Lite or similar USB audio interface.

Uses ALSA dsnoop to tap the audio without modifying Asterisk's config.
The existing node continues operating normally. The EAS monitor only
adds a passive listener tap.

No USRP node is needed — the existing node IS the audio path.
The EAS monitor only controls which remote nodes to connect via AMI.

Config section: [source_usb_shared]
  device      = eas_tap    # ALSA dsnoop device name (set up by installer)
  sample_rate = 22050
"""

import configparser
import subprocess


class USBSharedSource:
    """
    ALSA capture from a dsnoop tap on an existing node's audio device.
    Does not require a USRP node — audio path is already established.
    """

    # This source does NOT use USRP for audio injection.
    # The existing chan_simpleusb/chan_usbradio node carries audio.
    needs_usrp = False

    def __init__(self, config: configparser.ConfigParser):
        self.device      = config.get('source_usb_shared', 'device',
                                      fallback='eas_tap')
        self.sample_rate = config.get('source_usb_shared', 'sample_rate',
                                      fallback='22050')

    def get_process(self) -> subprocess.Popen:
        """
        Returns arecord subprocess reading from the dsnoop tap.
        stdout = raw signed 16-bit PCM mono at sample_rate Hz.
        """
        return subprocess.Popen(
            [
                'arecord',
                '-D', self.device,
                '-r', self.sample_rate,
                '-f', 'S16_LE',
                '-c', '1',
                '-t', 'raw',
                '-q',
                '-'
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

    def describe(self) -> str:
        return f"USB Shared (dsnoop tap: {self.device} @ {self.sample_rate}Hz)"
