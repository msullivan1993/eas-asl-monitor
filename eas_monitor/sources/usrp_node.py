"""
sources/usrp_node.py - Receive audio from Asterisk via USRP UDP
================================================================
Creates a private AllStarLink node that the radio node connects to
in local-monitor mode. Audio flows:

  Radio node (simpleusb) -> ilink 8 -> USRP private node -> UDP -> EAS monitor

No existing Asterisk config is modified. Two additive stanzas are
required in rpt.conf and extensions.conf (see install notes).

Python 3.5 compatible.
"""

import os
import struct
import subprocess
import sys

USRP_HEADER_FMT  = '>4sIIIIII'
USRP_HEADER_SIZE = struct.calcsize(USRP_HEADER_FMT)  # 28 bytes
USRP_AUDIO_SIZE  = 320   # 160 samples x 2 bytes @ 8kHz

# Path to the UDP receiver helper script
_SCRIPT = os.path.join(os.path.dirname(__file__),
                       '..', '..', 'scripts', 'usrp_source.py')
_SCRIPT = os.path.normpath(_SCRIPT)

# Installed path (after install.sh copies scripts/)
_SCRIPT_INSTALLED = '/etc/eas_monitor/scripts/usrp_source.py'


class USRPNodeSource(object):
    """Receive audio from a USRP node in Asterisk via UDP."""

    needs_usrp = False

    def __init__(self, config):
        self.rx_port    = config.getint(
            'source_usrp_node', 'rx_port', fallback=34001)
        self.private_node = config.get(
            'source_usrp_node', 'node', fallback='')
        self.radio_node   = config.get(
            'settings', 'local_node', fallback='')

    def get_process(self):
        script = (_SCRIPT_INSTALLED
                  if os.path.exists(_SCRIPT_INSTALLED) else _SCRIPT)
        if not os.path.exists(script):
            raise RuntimeError(
                "USRP source script not found: %s\n"
                "Re-run: sudo ./install.sh" % script
            )
        return subprocess.Popen(
            [sys.executable, script, str(self.rx_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

    def describe(self):
        return "USRP Node (private node %s <- radio node %s, UDP port %d)" % (
            self.private_node, self.radio_node, self.rx_port
        )

    @property
    def private_node_config(self):
        """Return the rpt.conf stanza needed for the private USRP node."""
        return (
            '; EAS Monitor private listener node\n'
            '; Add this stanza to /etc/asterisk/rpt.conf\n'
            '[{node}]\n'
            'rxchannel=USRP/127.0.0.1:{rx}:{tx}\n'
            'duplex=0\n'
            'scheduler=rpt-sched\n'
        ).format(
            node=self.private_node,
            rx=self.rx_port,
            tx=self.rx_port + 1
        )
