"""
sources/file.py
File audio source — converts a .wav file to raw PCM for testing.

Useful for verifying SAME decoding and node connection logic without
live radio hardware. Feed it a recorded SAME header.

Requires: sox

Config section: [source_file]
    path        = /etc/eas_monitor/test/same_test.wav
    sample_rate = 22050
    loop        = false    # Repeat the file (useful for long decode tests)
"""

import configparser
import subprocess


def get_audio_process(config: configparser.ConfigParser) -> subprocess.Popen:
    """
    Convert a .wav file to raw signed 16-bit PCM via sox.
    Returns a Popen process whose stdout is raw PCM.
    """
    path = config.get('source_file', 'path',
                      fallback='/etc/eas_monitor/test/same_test.wav')
    rate = config.get('source_file', 'sample_rate', fallback='22050')
    loop = config.getboolean('source_file', 'loop', fallback=False)

    # Build the sox command
    # Input: .wav file
    # Output: raw signed 16-bit little-endian mono at target sample rate
    cmd = ['sox']

    if loop:
        # Sox doesn't natively loop, use repeat
        cmd += ['--combine', 'sequence']
        cmd += [path, path, path, path, path]  # repeat a few times
    else:
        cmd += [path]

    cmd += [
        '-t', 'raw',        # Raw output format
        '-r', rate,         # Sample rate
        '-e', 'signed',     # Signed integer encoding
        '-b', '16',         # 16-bit
        '-c', '1',          # Mono
        '-',                # Write to stdout
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )
