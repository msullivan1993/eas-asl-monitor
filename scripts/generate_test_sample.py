#!/usr/bin/env python3
"""
generate_test_sample.py -- EAS/SAME Test Audio Generator
=========================================================
Generates a synthetic SAME/EAS audio file using the FIPS codes and settings
from your installed configuration, or from command-line arguments.

The output follows the real SAME broadcast pattern:
  1. SAME header (preamble + ZCZC header string) x3, 1s apart
  2. 8-second silence (represents the 1050 Hz attention tone gap)
  3. EOM (preamble + NNNN) x3, 1s apart

Audio format: 16-bit signed PCM WAV, mono, 22050 Hz
FSK encoding: mark=2083.3 Hz (1), space=1562.5 Hz (0), 520.83 baud

Usage:
  sudo python3 /etc/eas_monitor/scripts/generate_test_sample.py
  sudo python3 /etc/eas_monitor/scripts/generate_test_sample.py --fips 021019,021089
  sudo python3 /etc/eas_monitor/scripts/generate_test_sample.py --output /tmp/mytest.wav
  sudo python3 /etc/eas_monitor/scripts/generate_test_sample.py --list-events
"""

import argparse
import configparser
import math
import os
import struct
import sys
import wave

# ── Configuration ────────────────────────────────────────────────────────────
CONFIG_FILE  = '/etc/eas_monitor/fips_nodes.conf'
DEFAULT_OUT  = '/etc/eas_monitor/test/same_test.wav'
CALLSIGN     = 'KCBS/FM-'   # 8-char sender ID (padded to 8)

SAMPLE_RATE  = 22050
BAUD         = 520.83
MARK_FREQ    = 2083.3   # bit 1
SPACE_FREQ   = 1562.5   # bit 0
PREAMBLE_LEN = 16       # 16 bytes of 0xAB per SAME spec
PREAMBLE_BYTE = 0xAB

# SAME attention tone duration (silence in our synthetic version)
ATTENTION_SECS = 8

# ── Event code table ─────────────────────────────────────────────────────────
EVENTS = {
    # Tests
    'RWT': 'Required Weekly Test',
    'RMT': 'Required Monthly Test',
    'NPT': 'National Periodic Test',
    # National
    'EAN': 'Emergency Action Notification (Presidential)',
    'EAT': 'Emergency Action Termination',
    # Warnings
    'TOR': 'Tornado Warning',
    'SVR': 'Severe Thunderstorm Warning',
    'FFW': 'Flash Flood Warning',
    'EWW': 'Extreme Wind Warning',
    'HUW': 'Hurricane Warning',
    'SMW': 'Special Marine Warning',
    'BZW': 'Blizzard Warning',
    'WSW': 'Winter Storm Warning',
    'ICW': 'Ice Storm Warning',
    'LEW': 'Law Enforcement Warning',
    'CEM': 'Civil Emergency Message',
    # Watches
    'TOA': 'Tornado Watch',
    'SVA': 'Severe Thunderstorm Watch',
    'FFA': 'Flash Flood Watch',
    'HUA': 'Hurricane Watch',
}

ORGS = {
    'WXR': 'National Weather Service',
    'CIV': 'Civil Authorities',
    'EAS': 'EAS Participant',
    'PEP': 'Primary Entry Point System',
}

# ── FSK encoder ──────────────────────────────────────────────────────────────

def byte_to_bits(b):
    """8N1 serial: start bit (0), D0..D7 LSB first, stop bit (1)."""
    bits = [0]
    for i in range(8):
        bits.append((b >> i) & 1)
    bits.append(1)
    return bits

def encode_bytes(data):
    bits = []
    for b in (data if isinstance(data, (bytes, bytearray))
              else data.encode('ascii')):
        bits.extend(byte_to_bits(b if isinstance(b, int) else ord(b)))
    return bits

def preamble_bits():
    bits = []
    for _ in range(PREAMBLE_LEN):
        bits.extend(byte_to_bits(PREAMBLE_BYTE))
    return bits

def fsk_samples(bits, phase=0.0):
    """Convert bit stream to PCM samples using phase-continuous FSK."""
    samples = []
    t_acc   = 0.0
    spr     = SAMPLE_RATE / BAUD   # samples per bit (fractional)
    for bit in bits:
        freq  = MARK_FREQ if bit else SPACE_FREQ
        t_end = t_acc + spr
        while int(t_acc) < int(t_end):
            samples.append(math.sin(phase))
            phase += 2.0 * math.pi * freq / SAMPLE_RATE
            t_acc += 1
        t_acc = t_end
    return samples, phase

def silence_samples(secs):
    return [0.0] * int(SAMPLE_RATE * secs)

# ── SAME header builder ───────────────────────────────────────────────────────

def build_header(org, event, fips_list, purge_hhmm, issued_jjjhhmm, callsign):
    """
    Build a SAME header string.
    Format: ZCZC-ORG-EVT-PSSCCC-PSSCCC+HHMM-JJJHHMM-CCCCCCCC-
    """
    fips_str = '-'.join(fips_list)
    # Pad/truncate callsign to 8 chars with trailing -
    cs = (callsign.rstrip('-') + '--------')[:8]
    return 'ZCZC-{}-{}-{}+{}-{}-{}-'.format(
        org, event, fips_str, purge_hhmm, issued_jjjhhmm, cs
    )

def issued_timestamp():
    """Return current UTC time as JJJHHMM (day-of-year + HHMM)."""
    import time
    t    = time.gmtime()
    jjj  = t.tm_yday
    hhmm = t.tm_hour * 100 + t.tm_min
    return '%03d%04d' % (jjj, hhmm)

# ── WAV writer ────────────────────────────────────────────────────────────────

def write_wav(path, samples):
    peak = max(abs(x) for x in samples) or 1.0
    pcm  = [max(-32767, min(32767, int(x / peak * 28000))) for x in samples]
    with wave.open(path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(struct.pack('<%dh' % len(pcm), *pcm))

# ── Config reader ─────────────────────────────────────────────────────────────

def read_fips_from_config(config_path):
    """Read FIPS codes from [fips_map] section of config file."""
    if not os.path.exists(config_path):
        return []
    config = configparser.ConfigParser()
    config.read(config_path)
    if 'fips_map' not in config:
        return []
    return list(config['fips_map'].keys())

# ── Main ──────────────────────────────────────────────────────────────────────

def generate(fips_list, event, org, purge_hhmm, output_path, verbose=True):
    header = build_header(
        org          = org,
        event        = event,
        fips_list    = fips_list,
        purge_hhmm   = purge_hhmm,
        issued_jjjhhmm = issued_timestamp(),
        callsign     = CALLSIGN
    )

    if verbose:
        print('Header:  %s' % header)
        print('Event:   %s (%s)' % (event, EVENTS.get(event, 'Unknown')))
        print('FIPS:    %s' % ', '.join(fips_list))
        print('Output:  %s' % output_path)

    header_bits = preamble_bits() + encode_bytes(header)
    eom_bits    = preamble_bits() + encode_bytes('NNNN')

    samples = []
    phase   = 0.0

    # Header sent 3 times, ~1 second apart (per SAME spec)
    for i in range(3):
        s, phase = fsk_samples(header_bits, phase)
        samples.extend(s)
        if i < 2:
            samples.extend(silence_samples(1.0))

    # Gap representing attention tone (1050 Hz in real broadcast)
    samples.extend(silence_samples(ATTENTION_SECS))

    # EOM sent 3 times
    for i in range(3):
        s, phase = fsk_samples(eom_bits, phase)
        samples.extend(s)
        if i < 2:
            samples.extend(silence_samples(1.0))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    write_wav(output_path, samples)

    duration = len(samples) / SAMPLE_RATE
    size     = os.path.getsize(output_path)
    if verbose:
        print('Duration: %.1fs  |  Size: %d bytes' % (duration, size))
        print()
        print('Test with:')
        print('  sox %s -t raw -r 22050 -e signed -b 16 -c 1 - | multimon-ng -t raw -a EAS -' % output_path)

    return header


def main():
    parser = argparse.ArgumentParser(
        description='Generate a synthetic SAME/EAS test audio file.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--config',  default=CONFIG_FILE,
                        help='Config file to read FIPS codes from (default: %s)' % CONFIG_FILE)
    parser.add_argument('--fips',    default=None,
                        help='Comma-separated FIPS codes, e.g. 021019,021089  '
                             '(overrides config)')
    # Event code is always RWT for safety -- test samples must never
    # use real alert codes (Tornado Warning etc.) to avoid false alerts
    parser.add_argument('--org',     default='WXR',
                        help='Originator code: WXR, CIV, EAS, PEP (default: WXR)')
    parser.add_argument('--purge',   default='0030',
                        help='Purge time HHMM, e.g. 0030 = 30 min (default: 0030)')
    parser.add_argument('--output',  default=DEFAULT_OUT,
                        help='Output WAV file path (default: %s)' % DEFAULT_OUT)
    parser.add_argument('--list-events', action='store_true',
                        help='List available event codes and exit')

    args = parser.parse_args()

    if args.list_events:
        print('\nAvailable event codes:\n')
        for code, desc in sorted(EVENTS.items()):
            print('  %-6s  %s' % (code, desc))
        print()
        return

    # Resolve FIPS codes
    if args.fips:
        fips_list = [f.strip() for f in args.fips.split(',') if f.strip()]
    else:
        fips_list = read_fips_from_config(args.config)
        if not fips_list:
            print('ERROR: No FIPS codes found in %s' % args.config)
            print('       Run the setup wizard first, or use --fips 021019')
            sys.exit(1)
        print('Read %d FIPS code(s) from config' % len(fips_list))

    if args.event not in EVENTS:
        print('WARNING: Unknown event code "%s". Known codes:' % args.event)
        for code in sorted(EVENTS):
            print('  %s' % code)

    generate(
        fips_list   = fips_list,
        event       = 'RWT',  # always RWT for safety
        org         = args.org,
        purge_hhmm  = args.purge,
        output_path = args.output,
    )


if __name__ == '__main__':
    main()
