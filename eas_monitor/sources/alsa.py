"""
sources/alsa.py
ALSA audio source — reads from an ALSA capture device.

Intended for use with a RIM-Lite (or similar USB audio interface) connected
to an AllStarLink node, with audio tapped via an ALSA loopback device
(snd-aloop kernel module).

Config section: [source_alsa]
    device      = hw:Loopback,1,0   # ALSA capture device name
    sample_rate = 22050             # Hz
    channels    = 1                 # Mono
"""

import configparser
import subprocess


def get_audio_process(config: configparser.ConfigParser) -> subprocess.Popen:
    """
    Spawn arecord reading from the configured ALSA device.
    Returns a Popen process whose stdout is raw signed 16-bit PCM.
    """
    device      = config.get('source_alsa', 'device',      fallback='hw:Loopback,1,0')
    sample_rate = config.get('source_alsa', 'sample_rate',  fallback='22050')
    channels    = config.get('source_alsa', 'channels',     fallback='1')

    cmd = [
        'arecord',
        '-D', device,           # ALSA device
        '-r', sample_rate,      # Sample rate
        '-f', 'S16_LE',         # Signed 16-bit little-endian
        '-c', channels,         # Channels (1 = mono)
        '-t', 'raw',            # Raw PCM output (no WAV header)
        '-q',                   # Suppress arecord progress messages
        '-',                    # Write to stdout
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )
