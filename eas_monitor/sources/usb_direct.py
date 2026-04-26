"""
sources/usb_direct.py - USB Direct Source (Dedicated Weather Radio)
====================================================================
Audio source for a dedicated weather radio or scanner connected to a
USB audio dongle. The EAS monitor owns this device exclusively —
Asterisk does not use it.

Requires a USRP private node to inject audio into Asterisk.
The USRP node's PTT is controlled by the alert handler:
  - Between alerts: PTT=0, node is idle, no audio forwarded
  - During alert:   PTT=1, WX audio reaches connected nodes

Hardware: Any weather radio (Midland, Uniden, etc.) + ~$8 USB audio dongle
          Connected via 3.5mm audio cable (speaker out → dongle line in)

Config section: [source_usb_direct]
  device          = hw:1,0    # ALSA device (run 'arecord -l' to find)
  sample_rate     = 22050
"""

import configparser
import subprocess


class USBDirectSource:
    """
    Direct ALSA capture from a dedicated USB audio device.
    Requires USRP node for audio injection into Asterisk.
    """

    needs_usrp = True

    def __init__(self, config: configparser.ConfigParser):
        self.device      = config.get('source_usb_direct', 'device',
                                      fallback='hw:1,0')
        self.sample_rate = config.get('source_usb_direct', 'sample_rate',
                                      fallback='22050')

    def get_process(self) -> subprocess.Popen:
        """
        Returns arecord subprocess reading directly from the USB dongle.
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
        return (
            f"USB Direct (dedicated dongle: {self.device} "
            f"@ {self.sample_rate}Hz)"
        )
