#!/usr/bin/env python3
"""
setup_wizard.py - EAS/SAME AllStarLink Monitor Setup Wizard
=============================================================
Interactive whiptail-based configuration wizard.

Run as root:
  sudo python3 setup_wizard.py
  sudo python3 setup_wizard.py --update   # Re-run without clobbering FIPS map

Produces /etc/eas_monitor/fips_nodes.conf and applies all necessary
system configuration changes.
"""

import argparse
import configparser
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from io import StringIO
from pathlib import Path


# ── Constants ──────────────────────────────────────────────────────────────

INSTALL_DIR    = '/etc/eas_monitor'
CONFIG_FILE    = '/etc/eas_monitor/fips_nodes.conf'
FIPS_DATA_FILE = '/var/lib/eas_monitor/zcta_county.txt'
RECORDING_DIR  = '/var/lib/eas_monitor/recordings'
LOG_FILE       = '/var/log/eas_monitor.log'
SERVICE_FILE   = '/etc/systemd/system/eas-monitor.service'
ASOUND_CONF    = '/etc/asound.conf'

FIPS_URL = (
    'https://www2.census.gov/geo/docs/maps-data/data/'
    'rel2020/zcta520/tab20_zcta520_county20_natl.txt'
)

CENSUS_API = (
    'https://geocoding.geo.census.gov/geocoder/geographies/address'
    '?zip={zip}&benchmark=Public_AR_Current'
    '&vintage=Current_Current&layers=Counties&format=json'
)

NOAA_FREQUENCIES = {
    '162.400 MHz': '162400000',
    '162.425 MHz': '162425000',
    '162.450 MHz': '162450000',
    '162.475 MHz': '162475000',
    '162.500 MHz': '162500000',
    '162.525 MHz': '162525000',
    '162.550 MHz': '162550000',
}

WARNING_EVENTS = {
    'TOR': 'Tornado Warning',
    'SVR': 'Severe Thunderstorm Warning',
    'FFW': 'Flash Flood Warning',
    'EWW': 'Extreme Wind Warning',
    'HUW': 'Hurricane Warning',
    'SMW': 'Special Marine Warning',
    'SQW': 'Snow Squall Warning',
    'DSW': 'Dust Storm Warning',
    'BZW': 'Blizzard Warning',
    'WSW': 'Winter Storm Warning',
    'CEM': 'Civil Emergency Message',
}

WATCH_EVENTS = {
    'TOA': 'Tornado Watch',
    'SVA': 'Severe Thunderstorm Watch',
    'FFA': 'Flash Flood Watch',
    'HUA': 'Hurricane Watch',
    'WSA': 'Winter Storm Watch',
    'BZA': 'Blizzard Watch',
}

TEST_EVENTS = {
    'RMT': 'Required Monthly Test',
    'RWT': 'Required Weekly Test',
    'NPT': 'National Periodic Test',
}

SCREEN_WIDTH  = 76
SCREEN_HEIGHT = 20


# ── whiptail helpers ────────────────────────────────────────────────────────

class WTail:
    """Thin wrapper around whiptail for cleaner call syntax."""

    BACKTITLE = "EAS/SAME AllStarLink Monitor — Setup Wizard"

    @staticmethod
    def _run(args) -> tuple[int, str]:
        """Run whiptail, return (exit_code, output)."""
        full = ['whiptail', '--backtitle', WTail.BACKTITLE] + args
        try:
            result = subprocess.run(
                full,
                capture_output=True, text=True
            )
            return result.returncode, result.stderr.strip()
        except FileNotFoundError:
            print("ERROR: whiptail not found. Install libnewt (Arch) or whiptail (Debian).")
            sys.exit(1)

    @staticmethod
    def msgbox(text, title: str = '', height: int = 10) -> None:
        args = ['--title', title, '--msgbox', text,
                str(height), str(SCREEN_WIDTH)]
        WTail._run(args)

    @staticmethod
    def infobox(text, title: str = '') -> None:
        args = ['--title', title, '--infobox', text, '5', str(SCREEN_WIDTH)]
        WTail._run(args)

    @staticmethod
    def yesno(text, title: str = '',
              default_yes: bool = True,
              yes_btn: str = 'Yes', no_btn: str = 'No') -> bool:
        args = [
            '--title', title,
            '--yes-button', yes_btn,
            '--no-button', no_btn,
        ]
        if not default_yes:
            args.append('--defaultno')
        args += ['--yesno', text, '8', str(SCREEN_WIDTH)]
        rc, _ = WTail._run(args)
        return rc == 0

    @staticmethod
    def inputbox(text, default: str = '',
                 title: str = '', height: int = 8):
        args = ['--title', title, '--inputbox', text,
                str(height), str(SCREEN_WIDTH), default]
        rc, out = WTail._run(args)
        return out if rc == 0 else None

    @staticmethod
    def passwordbox(text, title: str = ''):
        args = ['--title', title, '--passwordbox', text,
                '8', str(SCREEN_WIDTH), '']
        rc, out = WTail._run(args)
        return out if rc == 0 else None

    @staticmethod
    def menu(text, items,
             title: str = '', height: int = None):
        """items = list of (tag, description) tuples."""
        n = len(items)
        h = height or min(SCREEN_HEIGHT, n + 8)
        args = ['--title', title, '--menu', text,
                str(h), str(SCREEN_WIDTH), str(n)]
        for tag, desc in items:
            args += [str(tag), str(desc)]
        rc, out = WTail._run(args)
        return out if rc == 0 else None

    @staticmethod
    def radiolist(text, items,
                  title: str = ''):
        """items = list of (tag, description, selected_bool) tuples."""
        n = len(items)
        h = min(SCREEN_HEIGHT, n + 8)
        args = ['--title', title, '--radiolist', text,
                str(h), str(SCREEN_WIDTH), str(n)]
        for tag, desc, selected in items:
            args += [str(tag), str(desc), 'ON' if selected else 'OFF']
        rc, out = WTail._run(args)
        return out if rc == 0 else None

    @staticmethod
    def checklist(text, items,
                  title: str = ''):
        """items = list of (tag, description, checked_bool) tuples."""
        n = len(items)
        h = min(SCREEN_HEIGHT, n + 8)
        args = [
            '--title', title,
            '--separate-output',
            '--checklist', text,
            str(h), str(SCREEN_WIDTH), str(n)
        ]
        for tag, desc, checked in items:
            args += [str(tag), str(desc), 'ON' if checked else 'OFF']
        rc, out = WTail._run(args)
        if rc != 0:
            return None
        return [line for line in out.splitlines() if line]

    @staticmethod
    def textbox(text, title: str = '') -> None:
        """Display scrollable text."""
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                         delete=False) as f:
            f.write(text)
            fname = f.name
        args = ['--title', title, '--scrolltext',
                '--textbox', fname, str(SCREEN_HEIGHT), str(SCREEN_WIDTH)]
        WTail._run(args)
        os.unlink(fname)

    @staticmethod
    def gauge(text, steps) -> None:
        """
        Show a progress gauge while executing steps.
        steps = list of (description, callable) tuples.
        """
        total = len(steps)
        proc  = subprocess.Popen(
            ['whiptail', '--backtitle', WTail.BACKTITLE,
             '--gauge', text, '8', str(SCREEN_WIDTH), '0'],
            stdin=subprocess.PIPE, text=True
        )
        for i, (desc, fn) in enumerate(steps):
            pct = int(i / total * 100)
            try:
                proc.stdin.write("XXX\n{}\n{}\nXXX\n".format(pct, desc))
                proc.stdin.flush()
            except Exception:
                pass
            try:
                fn()
            except Exception as e:
                pass  # Errors are logged by the callables themselves

        try:
            proc.stdin.write("XXX\n100\nDone.\nXXX\n")
            proc.stdin.flush()
        except Exception:
            pass
        proc.communicate()


# ── System detection helpers ────────────────────────────────────────────────

def detect_distro() -> str:
    try:
        content = Path('/etc/os-release').read_text()
        if 'hamvoip' in content.lower() or 'arch' in content.lower():
            return 'arch'
        if any(d in content.lower() for d in ('debian', 'ubuntu', 'raspbian')):
            return 'debian'
    except Exception:
        pass
    return 'unknown'


def detect_alsa_capture_devices() -> list:
    """Returns list of (hw_string, name) tuples."""
    try:
        out = subprocess.run(['arecord', '-l'],
                             capture_output=True, text=True)
        devices = []
        for line in out.stdout.splitlines():
            m = re.match(r'card (\d+): .+\[(.+?)\].*device (\d+):', line)
            if m:
                hw   = "hw:{},{}".format(m.group(1), m.group(3))
                name = m.group(2).strip()
                devices.append((hw, name))
        return devices
    except Exception:
        return []


def detect_rtlsdr_devices() -> list:
    """Returns list of (index, name) tuples."""
    try:
        out = subprocess.run(['rtl_test', '-t'],
                             capture_output=True, text=True, timeout=4)
        devices = []
        for line in (out.stdout + out.stderr).splitlines():
            m = re.match(r'\s*(\d+):\s+(.+)', line)
            if m:
                devices.append((m.group(1), m.group(2).strip()))
        return devices
    except Exception:
        return []


def test_ami(host, port, user, secret) -> bool:
    """Try to log in to AMI. Returns True on success."""
    import socket
    try:
        s = socket.socket()
        s.settimeout(4)
        s.connect((host, port))
        s.recv(1024)
        s.sendall(
            "Action: Login\r\nUsername: {}\r\nSecret: {}\r\n\r\n".format(user, secret)
            .encode()
        )
        resp = s.recv(1024).decode(errors='replace')
        s.close()
        return 'Success' in resp
    except Exception:
        return False


def check_dsnoop(device_hw) -> bool:
    """Test if dsnoop can open the given ALSA device."""
    try:
        proc = subprocess.run(
            ['arecord', '-D', 'dsnoop:{}'.format(device_hw.replace("hw:","")),
             '-r', '22050', '-f', 'S16_LE', '-c', '1',
             '-d', '1', '-t', 'raw', '/dev/null'],
            capture_output=True, timeout=5
        )
        return proc.returncode == 0
    except Exception:
        return False


# ── Device identification — udev rules and RTL-SDR EEPROM ───────────────────

# RTL-SDR serials that ship as factory defaults — not unique, not usable
GENERIC_RTL_SERIALS = {'00000001', '0', '00000000', '1', '', 'rtlsdr', 'RTLSDR'}

# USB audio serials that are commonly absent or non-unique
GENERIC_USB_SERIALS = {'', '000000', '0000000000000000'}

UDEV_RULES_FILE = '/etc/udev/rules.d/99-eas-monitor.rules'


def read_rtlsdr_eeprom(device_index) -> dict:
    """
    Read EEPROM info from an RTL-SDR dongle.
    Returns dict with keys: serial, vendor_id, product_id, manufacturer, product.
    """
    result = {'serial': '', 'vendor_id': '', 'product_id': '',
              'manufacturer': '', 'product': ''}
    try:
        out = subprocess.run(
            ['rtl_eeprom', '-d', str(device_index)],
            capture_output=True, text=True, timeout=6
        )
        text = out.stdout + out.stderr
        for line in text.splitlines():
            ll = line.lower()
            if 'serial number' in ll or ('serial' in ll and ':' in ll):
                m = re.search(r':\s*(\S+)', line)
                if m:
                    result['serial'] = m.group(1).strip()
            elif 'vendor id' in ll:
                m = re.search(r'0x([0-9a-fA-F]+)', line)
                if m:
                    result['vendor_id'] = m.group(1).lower()
            elif 'product id' in ll:
                m = re.search(r'0x([0-9a-fA-F]+)', line)
                if m:
                    result['product_id'] = m.group(1).lower()
            elif 'manufacturer' in ll and ':' in line:
                m = re.search(r':\s*(.+)', line)
                if m:
                    result['manufacturer'] = m.group(1).strip()
            elif 'product' in ll and ':' in line and 'id' not in ll:
                m = re.search(r':\s*(.+)', line)
                if m:
                    result['product'] = m.group(1).strip()
    except FileNotFoundError:
        pass  # rtl_eeprom not installed
    except Exception:
        pass
    return result


def write_rtlsdr_serial(device_index, new_serial) -> bool:
    """
    Write a new serial number to an RTL-SDR dongle's EEPROM.
    rtl_eeprom prompts for confirmation — we pipe 'y' to stdin.
    The dongle must be unplugged and replugged to take effect.
    """
    try:
        proc = subprocess.run(
            ['rtl_eeprom', '-d', str(device_index), '-s', new_serial],
            input='y\n',
            capture_output=True, text=True, timeout=10
        )
        # Success if return code 0 and no error about write failure
        return proc.returncode == 0 and 'failed' not in proc.stdout.lower()
    except Exception:
        return False


def wait_for_rtlsdr_serial(expected_serial,
                            timeout: int = 30) -> bool:
    """
    Poll for a dongle with the given serial to appear.
    Returns True when found, False on timeout.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ['rtl_eeprom', '-d', expected_serial],
                capture_output=True, text=True, timeout=4
            )
            if expected_serial in (out.stdout + out.stderr):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def get_alsa_udev_attrs(hw_string) -> dict:
    """
    Probe USB device attributes for an ALSA card via udevadm.
    hw_string: 'hw:1,0' or 'hw:2,0' etc.
    Returns dict with: vendor, product, serial, card_num.
    """
    result = {'vendor': '', 'product': '', 'serial': '', 'card_num': ''}
    m = re.match(r'hw:(\d+)', hw_string)
    if not m:
        return result
    card_num = m.group(1)
    result['card_num'] = card_num

    try:
        out = subprocess.run(
            ['udevadm', 'info', '-a',
             '/sys/class/sound/card{}'.format(card_num)],
            capture_output=True, text=True, timeout=5
        )
        text = out.stdout
        # udevadm -a walks up the device tree; we want the first USB parent
        # that has idVendor (the USB device, not the sound class entry itself)
        vendor = product = serial = ''
        in_usb_section = False
        for line in text.splitlines():
            # Each parent device is introduced by a "looking at parent" line
            if 'looking at parent' in line.lower():
                # Once we have all three, stop searching higher up the tree
                if vendor and product:
                    break
                in_usb_section = False
            if 'ATTRS{idVendor}' in line and not vendor:
                m2 = re.search(r'"([0-9a-fA-F]{4})"', line)
                if m2:
                    vendor = m2.group(1).lower()
                    in_usb_section = True
            if 'ATTRS{idProduct}' in line and not product and in_usb_section:
                m2 = re.search(r'"([0-9a-fA-F]{4})"', line)
                if m2:
                    product = m2.group(1).lower()
            if 'ATTRS{serial}' in line and not serial and in_usb_section:
                m2 = re.search(r'"(.+)"', line)
                if m2:
                    serial = m2.group(1).strip()

        result.update({'vendor': vendor, 'product': product, 'serial': serial})
    except Exception:
        pass
    return result


def build_udev_rule(symlink_name, vendor, product,
                    serial: str = '', subsystem: str = 'sound') -> tuple[str, str]:
    """
    Build a udev rule string and a plain-English description of what it matches.
    Returns (rule_line, description).
    """
    if subsystem == 'sound':
        # Sound device symlink — matches the ALSA card
        attrs = (
            'ATTRS{{idVendor}}=="{}", ATTRS{{idProduct}}=="{}"'.format(vendor, product)
        )
        if serial and serial not in GENERIC_USB_SERIALS:
            attrs += ', ATTRS{{serial}}=="{}"'.format(serial)
            match_desc = "vendor={} product={} serial={}".format(vendor, product, serial)
        else:
            match_desc = "vendor={} product={} (no unique serial)".format(vendor, product)
        rule = (
            'SUBSYSTEM=="sound", SUBSYSTEMS=="usb", {}, SYMLINK+="snd/{}"'.format(attrs, symlink_name)
        )
    else:
        # USB device for RTL-SDR — no sound subsystem
        attrs = (
            'ATTRS{{idVendor}}=="{}", ATTRS{{idProduct}}=="{}"'.format(vendor, product)
        )
        if serial and serial not in GENERIC_USB_SERIALS:
            attrs += ', ATTRS{{serial}}=="{}"'.format(serial)
            match_desc = "vendor={} product={} serial={}".format(vendor, product, serial)
        else:
            match_desc = "vendor={} product={} (no unique serial)".format(vendor, product)
        rule = (
            'SUBSYSTEM=="usb", {}, SYMLINK+="rtl_sdr_{}"'.format(attrs, symlink_name)
        )
    return rule, match_desc


def write_udev_rules(rules) -> bool:
    """
    Write a list of rule lines to the udev rules file and reload.
    rules: list of strings (one rule per line).
    """
    header = (
        "# EAS Monitor — generated by setup_wizard.py\n"
        "# Do not edit manually — re-run setup_wizard.py to update\n\n"
    )
    content = header + '\n'.join(rules) + '\n'
    try:
        Path(UDEV_RULES_FILE).write_text(content)
        subprocess.run(['udevadm', 'control', '--reload'],
                       capture_output=True, timeout=5)
        subprocess.run(['udevadm', 'trigger'],
                       capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def generate_wx_serial(index: int = 1) -> str:
    """Generate a human-readable serial for a WX monitor dongle."""
    return "WXMON{:02d}".format(index)


# ── FIPS lookup ─────────────────────────────────────────────────────────────

def download_fips_data() -> bool:
    """Download ZCTA-to-county relationship file from Census Bureau."""
    Path(FIPS_DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
    try:
        WTail.infobox("Downloading FIPS reference data from Census Bureau...")
        urllib.request.urlretrieve(FIPS_URL, FIPS_DATA_FILE)
        return True
    except Exception:
        return False


def zip_to_fips_api(zipcode) -> list:
    """Live Census API lookup. Returns list of {fips, county, state} dicts."""
    try:
        url = CENSUS_API.format(zip=zipcode)
        req = urllib.request.Request(url, headers={'User-Agent': 'eas-monitor/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        counties = data.get('result', {}).get('geographies', {}).get('Counties', [])
        return [
            {
                'fips':   c['STATE'] + c['COUNTY'],
                'county': c['NAME'],
                'state':  c['STATE']
            }
            for c in counties
        ]
    except Exception:
        return []


def zip_to_fips_local(zipcode) -> list:
    """Local ZCTA file lookup. Returns list of {fips, county, state} dicts."""
    if not Path(FIPS_DATA_FILE).exists():
        return []
    try:
        results = []
        with open(FIPS_DATA_FILE, 'r') as f:
            reader = csv.reader(f, delimiter='|')
            for row in reader:
                if len(row) >= 2 and row[0].strip() == zipcode:
                    state_fips  = row[1].strip()[:2]
                    county_fips = row[1].strip()
                    county_name = row[2].strip() if len(row) > 2 else county_fips
                    results.append({
                        'fips':   county_fips,
                        'county': county_name,
                        'state':  state_fips
                    })
        return results
    except Exception:
        return []


def lookup_fips(zipcode) -> list:
    """Try API first, fall back to local file."""
    results = zip_to_fips_api(zipcode)
    if not results:
        results = zip_to_fips_local(zipcode)
    return results


# ── State/county FIPS name lookup ───────────────────────────────────────────

STATE_FIPS = {
    '01':'AL','02':'AK','04':'AZ','05':'AR','06':'CA','08':'CO','09':'CT',
    '10':'DE','11':'DC','12':'FL','13':'GA','15':'HI','16':'ID','17':'IL',
    '18':'IN','19':'IA','20':'KS','21':'KY','22':'LA','23':'ME','24':'MD',
    '25':'MA','26':'MI','27':'MN','28':'MS','29':'MO','30':'MT','31':'NE',
    '32':'NV','33':'NH','34':'NJ','35':'NM','36':'NY','37':'NC','38':'ND',
    '39':'OH','40':'OK','41':'OR','42':'PA','44':'RI','45':'SC','46':'SD',
    '47':'TN','48':'TX','49':'UT','50':'VT','51':'VA','53':'WA','54':'WV',
    '55':'WI','56':'WY'
}


# ── Config writing ──────────────────────────────────────────────────────────

def write_config(cfg) -> None:
    """Write fips_nodes.conf from collected wizard data."""
    Path(INSTALL_DIR).mkdir(parents=True, exist_ok=True)
    c = configparser.ConfigParser()

    # [settings]
    c['settings'] = {
        'local_node':       cfg['local_node'],
        'public_node':      cfg.get('public_node', cfg['local_node']),
        'audio_source':     cfg['audio_source'],
        'log_file':         LOG_FILE,
        'max_link_duration': '3600',
        'act_on_warnings':  'true',
        'act_on_watches':   'true' if cfg.get('act_watches') else 'false',
        'act_on_tests':     'true' if cfg.get('act_tests') else 'false',
    }

    # [ami]
    c['ami'] = {
        'host': cfg.get('ami_host', '127.0.0.1'),
        'port': str(cfg.get('ami_port', 5038)),
        'user': cfg.get('ami_user', 'eas_monitor'),
        'pass': cfg.get('ami_pass', ''),
    }

    # Source-specific section
    src = cfg['audio_source']
    if src == 'usb_shared':
        c['source_usb_shared'] = {
            'device':      cfg.get('device', 'eas_tap'),
            'sample_rate': '22050',
        }
    elif src == 'usb_direct':
        c['source_usb_direct'] = {
            'device':      cfg.get('device', 'hw:1,0'),
            'sample_rate': '22050',
        }
    elif src == 'rtlsdr':
        c['source_rtlsdr'] = {
            'frequencies':    ','.join(str(f) for f in cfg.get('frequencies', ['162550000'])),
            'device_index':   str(cfg.get('rtl_device', 0)
                                  if str(cfg.get('rtl_device', 0)).isdigit()
                                  else 0),
            'device_serial':  cfg.get('rtl_serial', ''),
            'gain':           str(cfg.get('gain', 40)),
            'ppm_correction': str(cfg.get('ppm', 0)),
        }
    elif src == 'stream':
        if cfg.get('broadcastify_feed_id'):
            c['source_stream'] = {
                'broadcastify_feed_id': cfg['broadcastify_feed_id'],
                'broadcastify_username': cfg.get('broadcastify_user', ''),
                'broadcastify_password': cfg.get('broadcastify_pass', ''),
                'sample_rate': '22050',
                'reconnect_delay': '30',
            }
        else:
            c['source_stream'] = {
                'url': cfg.get('stream_url', ''),
                'sample_rate': '22050',
                'reconnect_delay': '30',
            }

    # [usrp_nodes] — for sources that need audio injection
    if cfg.get('usrp_nodes'):
        c['usrp_nodes'] = {}
        for key, (tx, rx) in cfg['usrp_nodes'].items():
            c['usrp_nodes'][str(key)] = "{}:{}".format(tx, rx)

    # [alert_behavior]
    c['alert_behavior'] = {}
    for event, behavior in cfg.get('alert_behavior', {}).items():
        c['alert_behavior'][event] = behavior

    # [recording]
    c['recording'] = {
        'enabled':        'true',
        'directory':      RECORDING_DIR,
        'max_recordings': str(cfg.get('max_recordings', 5)),
    }

    # [fips_map]
    if cfg.get('fips_map'):
        c['fips_map'] = cfg['fips_map']
    else:
        c['fips_map'] = {}

    # [event_node_override] — national alerts
    c['event_node_override'] = {}

    with open(CONFIG_FILE, 'w') as f:
        c.write(f)

    # Secure permissions (config may contain stream credentials)
    os.chmod(CONFIG_FILE, 0o600)


# ── System configuration helpers ────────────────────────────────────────────

def setup_dsnoop(device_hw) -> bool:
    """Write ALSA dsnoop stanza to /etc/asound.conf."""
    card_dev = device_hw.replace('hw:', '')
    stanza = """
# EAS Monitor dsnoop tap — added by setup_wizard.py
pcm.eas_tap {{
    type dsnoop
    ipc_key 2468
    slave.pcm "{}"
}}
""".format(device_hw)
    try:
        existing = ''
        if Path(ASOUND_CONF).exists():
            existing = Path(ASOUND_CONF).read_text()
        if 'eas_tap' in existing:
            return True  # Already configured
        with open(ASOUND_CONF, 'a') as f:
            f.write(stanza)
        return True
    except Exception:
        return False


def setup_snd_aloop() -> bool:
    """Enable snd-aloop kernel module (for usb_shared fallback if dsnoop fails)."""
    try:
        subprocess.run(['modprobe', 'snd-aloop'], check=True,
                       capture_output=True)
        # Persist
        dist = detect_distro()
        if dist == 'arch':
            Path('/etc/modules-load.d/snd-aloop.conf').write_text('snd-aloop\n')
        else:
            modules = Path('/etc/modules')
            content = modules.read_text() if modules.exists() else ''
            if 'snd-aloop' not in content:
                with open('/etc/modules', 'a') as f:
                    f.write('\nsnd-aloop\n')
        return True
    except Exception:
        return False


def enable_chan_usrp() -> bool:
    """Enable chan_usrp.so in /etc/asterisk/modules.conf."""
    modules_conf = Path('/etc/asterisk/modules.conf')
    if not modules_conf.exists():
        return False
    try:
        content = modules_conf.read_text()
        if 'load => chan_usrp.so' in content:
            return True  # Already enabled
        # Replace noload with load
        new_content = re.sub(
            r'noload\s*=>\s*chan_usrp\.so',
            'load => chan_usrp.so',
            content
        )
        if new_content == content:
            # Not found as noload — append load line
            new_content += '\nload => chan_usrp.so\n'
        modules_conf.write_text(new_content)
        return True
    except Exception:
        return False


def add_usrp_node_to_asterisk(node_num, tx_port,
                               rx_port) -> bool:
    """Add a USRP private node to rpt.conf and extensions.conf."""
    rpt_conf = Path('/etc/asterisk/rpt.conf')
    ext_conf = Path('/etc/asterisk/extensions.conf')

    rpt_stanza = """
[{}]
rxchannel = USRP/127.0.0.1:{}:{}
duplex = 0
hangtime = 0
althangtime = 0
holdofftelem = 1
telemdefault = 0
telemdynamic = 0
nounkeyct = 1
idrecording = |i
idtime = 99999999
; EAS Monitor private receive node — do not modify
""".format(node_num, tx_port, rx_port)
    ext_entry = "exten => {},1,rpt,{}\n".format(node_num, node_num)
    nodes_entry = "{} = radio@127.0.0.1/{},NONE\n".format(node_num, node_num)

    try:
        # rpt.conf
        if rpt_conf.exists():
            content = rpt_conf.read_text()
            if '[{}]'.format(node_num) not in content:
                with open(rpt_conf, 'a') as f:
                    f.write(rpt_stanza)

        # extensions.conf
        if ext_conf.exists():
            content = ext_conf.read_text()
            if ext_entry.strip() not in content:
                # Add to radio-secure context if it exists
                if '[radio-secure]' in content:
                    content = content.replace(
                        '[radio-secure][radio-secure]\n{}'.format(ext_entry)
                    )
                    ext_conf.write_text(content)

        # [nodes] in rpt.conf
        if rpt_conf.exists():
            content = rpt_conf.read_text()
            if '{} = radio@'.format(node_num) not in content:
                content = re.sub(
                    r'(\[nodes\])',
                    '\\1\n{}'.format(nodes_entry),
                    content
                )
                rpt_conf.write_text(content)

        return True
    except Exception:
        return False


def add_dtmf_playback_commands(public_node, max_recs) -> bool:
    """Add DTMF playback commands to rpt.conf [functions] stanza."""
    rpt_conf = Path('/etc/asterisk/rpt.conf')
    if not rpt_conf.exists():
        return False

    commands = '\n; EAS Monitor — alert playback (*91 = most recent)\n'
    for i in range(1, max_recs + 1):
        path = "{}/recent_{}".format(RECORDING_DIR, i)
        commands += (
            "9{} = cop,14,{} ; Play recording #{} (most recent = 1)\n".format(i, path, i)
        )

    try:
        content = rpt_conf.read_text()
        if '; EAS Monitor — alert playback' in content:
            return True  # Already added
        # Add to [functions] stanza
        if '[functions]' in content:
            content = content.replace('[functions]',
                                      '[functions]\n{}'.format(commands))
        else:
            content += '\n[functions]\n{}'.format(commands)
        rpt_conf.write_text(content)
        return True
    except Exception:
        return False


def reload_asterisk() -> bool:
    """Reload Asterisk configuration."""
    try:
        subprocess.run(['asterisk', '-rx', 'reload'],
                       capture_output=True, timeout=15)
        return True
    except Exception:
        return False


def install_service(install_dir) -> bool:
    """Install and enable the systemd service."""
    src = Path(install_dir) / 'systemd' / 'eas-monitor.service'
    if not src.exists():
        return False
    try:
        import shutil
        shutil.copy2(src, SERVICE_FILE)
        subprocess.run(['systemctl', 'daemon-reload'], capture_output=True)
        subprocess.run(['systemctl', 'enable', 'eas-monitor'],
                       capture_output=True)
        return True
    except Exception:
        return False


# ── Main Wizard Class ───────────────────────────────────────────────────────

class EASWizard:

    def __init__(self, update_mode: bool = False):
        self.cfg          = {}
        self.update_mode  = update_mode
        self._install_dir = str(Path(__file__).parent.parent.resolve())

    def run(self):
        """Run the complete wizard flow."""
        if os.geteuid() != 0:
            print("This wizard must be run as root: sudo python3 setup_wizard.py")
            sys.exit(1)

        # Download FIPS data early (non-blocking — happens in background)
        if not Path(FIPS_DATA_FILE).exists():
            download_fips_data()

        screens = [
            self.screen_welcome,
            self.screen_node_config,
            self.screen_source_select,
            self.screen_source_config,
            self.screen_device_id,        # udev/EEPROM device identification
            self.screen_usrp_nodes,
            self.screen_fips_setup,
            self.screen_node_mapping,
            self.screen_alert_types,
            self.screen_alert_behavior,
            self.screen_recording,
            self.screen_review,
            self.screen_apply,
            self.screen_test,
            self.screen_done,
        ]

        i = 0
        while i < len(screens):
            result = screens[i]()
            if result is False:
                # User pressed Back
                i = max(0, i - 1)
            elif result is None:
                # User cancelled — confirm exit
                if WTail.yesno("Exit wizard? Configuration has NOT been saved.",
                               title="Confirm Exit", default_yes=False):
                    sys.exit(0)
            else:
                i += 1

    # ── Screen methods ─────────────────────────────────────────────────────

    def screen_welcome(self):
        text = (
            "Welcome to the EAS/SAME AllStarLink Monitor Setup Wizard.\n\n"
            "This wizard will configure your node to:\n"
            "  • Monitor NOAA Weather Radio for SAME/EAS alerts\n"
            "  • Automatically connect nodes based on FIPS county codes\n"
            "  • Record and replay recent alerts\n\n"
            "Prerequisites:\n"
            "  • AllStarLink node running (Asterisk/app_rpt)\n"
            "  • Internet connection for FIPS data download\n"
            "  • Audio hardware (USB dongle, RTL-SDR, or stream URL)\n\n"
            "Press OK to continue or Cancel to exit."
        )
        WTail.msgbox(text, title="EAS Monitor Setup", height=18)
        return True

    def screen_node_config(self):
        # Local node number
        node = WTail.inputbox(
            "Enter your AllStarLink node number:",
            default=self.cfg.get('local_node', ''),
            title="Node Configuration"
        )
        if node is None:
            return None
        if not node.strip().isdigit():
            WTail.msgbox("Node number must be numeric.", title="Invalid Input")
            return self.screen_node_config()
        self.cfg['local_node'] = node.strip()

        # AMI credentials
        ami_user = WTail.inputbox(
            "AMI username (from /etc/asterisk/manager.conf):",
            default=self.cfg.get('ami_user', 'eas_monitor'),
            title="AMI Credentials"
        )
        if ami_user is None:
            return False
        self.cfg['ami_user'] = ami_user.strip()

        ami_pass = WTail.passwordbox(
            "AMI password (from /etc/asterisk/manager.conf):",
            title="AMI Credentials"
        )
        if ami_pass is None:
            return False
        self.cfg['ami_pass'] = ami_pass

        # Test AMI
        WTail.infobox("Testing AMI connection...")
        ok = test_ami('127.0.0.1', 5038,
                      self.cfg['ami_user'], self.cfg['ami_pass'])
        if ok:
            WTail.msgbox(
                "AMI connection successful.",
                title="Connection Test"
            )
        else:
            if not WTail.yesno(
                "Could not connect to Asterisk AMI.\n\n"
                "This may be because:\n"
                "  • Asterisk is not running\n"
                "  • AMI user/password is incorrect\n"
                "  • AMI is not enabled in manager.conf\n\n"
                "Continue anyway? (You can fix this later)",
                title="AMI Connection Failed",
                default_yes=False
            ):
                return False

        self.cfg['ami_host'] = '127.0.0.1'
        self.cfg['ami_port'] = 5038
        return True

    def screen_source_select(self):
        items = [
            ('usb_shared',
             'Existing RIM-Lite or radio interface node (ALSA tap)'),
            ('usb_direct',
             'Dedicated weather radio + USB audio dongle'),
            ('rtlsdr',
             'RTL-SDR dongle (~$25, software defined radio)'),
            ('stream',
             'Internet stream — Broadcastify or free URL (no hardware)'),
        ]
        choice = WTail.menu(
            "Select your audio source:\n\n"
            "This determines how the EAS monitor receives NOAA WX audio.",
            items,
            title="Audio Source Selection"
        )
        if choice is None:
            return None
        self.cfg['audio_source'] = choice
        return True

    def screen_source_config(self):
        src = self.cfg.get('audio_source', '')
        if src == 'usb_shared':
            return self._config_usb_shared()
        elif src == 'usb_direct':
            return self._config_usb_direct()
        elif src == 'rtlsdr':
            return self._config_rtlsdr()
        elif src == 'stream':
            return self._config_stream()
        return True

    def _config_usb_shared(self):
        devices = detect_alsa_capture_devices()
        if not devices:
            WTail.msgbox(
                "No ALSA capture devices found.\n\n"
                "Ensure your RIM-Lite is plugged in and try again.",
                title="No Devices Found"
            )
            return False

        items = [(hw, name) for hw, name in devices]
        choice = WTail.menu(
            "Select your RIM-Lite or USB audio device:\n"
            "(This is the device currently used by your ASL node)",
            items,
            title="USB Shared — Select Device"
        )
        if choice is None:
            return False
        self.cfg['device'] = choice

        # Test dsnoop
        WTail.infobox("Testing ALSA dsnoop tap...")
        if check_dsnoop(choice):
            WTail.msgbox(
                "dsnoop tap works on {}.\n\nAudio will be shared between Asterisk and the EAS monitor.\nNo changes to your Asterisk configuration are needed.".format(choice),
                title="dsnoop Test Passed"
            )
            self.cfg['dsnoop_ok'] = True
        else:
            WTail.msgbox(
                "dsnoop could not open {}.\n\nThis is usually because the device driver doesn't support\nshared capture. The installer will set up an ALSA loopback\nas an alternative. Your Asterisk config will need a small\nupdate — the installer will guide you.".format(choice),
                title="dsnoop Not Available"
            )
            self.cfg['dsnoop_ok'] = False
        return True

    def _config_usb_direct(self):
        devices = detect_alsa_capture_devices()
        if not devices:
            WTail.msgbox(
                "No ALSA capture devices found.\n\n"
                "Ensure your USB audio dongle is plugged in.",
                title="No Devices Found"
            )
            return False

        items = [(hw, name) for hw, name in devices]
        choice = WTail.menu(
            "Select the USB audio dongle connected to your weather radio:\n"
            "(NOT the device used by your ASL node — a separate dongle)",
            items,
            title="USB Direct — Select Device"
        )
        if choice is None:
            return False
        self.cfg['device'] = choice
        return True

    def _config_rtlsdr(self):
        devices = detect_rtlsdr_devices()
        if not devices:
            WTail.msgbox(
                "No RTL-SDR devices found.\n\n"
                "Ensure your dongle is plugged in and rtl-sdr is installed.",
                title="No RTL-SDR Found"
            )
            return False

        if len(devices) > 1:
            items = [(idx, name) for idx, name in devices]
            dev = WTail.menu("Select your RTL-SDR dongle:",
                             items, title="RTL-SDR Device")
            if dev is None:
                return False
            self.cfg['rtl_device'] = int(dev)
        else:
            self.cfg['rtl_device'] = 0

        # Frequency selection
        freq_items = [
            (hz, "{} — {}".format(label, 'KIH39, KEC79...' if '550' in label else label),
             label == '162.550 MHz')
            for label, hz in NOAA_FREQUENCIES.items()
        ]
        selected = WTail.checklist(
            "Select NOAA WX frequencies to monitor:\n\n"
            "Select ALL frequencies from transmitters that cover your area.\n"
            "Multiple selections require numpy (Pi 3B: max 2-3 recommended).",
            freq_items,
            title="Frequency Selection"
        )
        if selected is None or not selected:
            WTail.msgbox("At least one frequency must be selected.",
                         title="No Frequency Selected")
            return self._config_rtlsdr()
        self.cfg['frequencies'] = selected

        # Gain
        gain = WTail.inputbox(
            "Tuner gain (0 = automatic, typical range 20-50):\n\n"
            "Start with 40 and adjust if decode quality is poor.",
            default=str(self.cfg.get('gain', 40)),
            title="RTL-SDR Gain"
        )
        if gain is None:
            return False
        self.cfg['gain'] = int(gain) if gain.strip().isdigit() else 40

        # PPM calibration
        ppm = WTail.inputbox(
            "PPM frequency correction for your dongle (usually 0-50):\n\n"
            "Run 'rtl_test -t' to measure. Set 0 if unsure.",
            default=str(self.cfg.get('ppm', 0)),
            title="PPM Calibration"
        )
        if ppm is None:
            return False
        self.cfg['ppm'] = int(ppm) if ppm.lstrip('-').isdigit() else 0
        return True

    def _config_stream(self):
        choice = WTail.menu(
            "Select stream type:",
            [
                ('broadcastify',
                 'Broadcastify premium (RadioReference.com account)'),
                ('free',
                 'Free stream URL (noaaweatherradio.org, weatherusa.net)'),
            ],
            title="Stream Source"
        )
        if choice is None:
            return False

        if choice == 'broadcastify':
            WTail.msgbox(
                "You need a RadioReference.com Premium subscription.\n\n"
                "To find your feed ID:\n"
                "  1. Go to: broadcastify.com/search/?q=noaa+weather+radio\n"
                "  2. Find your local NOAA WX transmitter\n"
                "  3. The Feed ID is the number at the end of the URL:\n"
                "     broadcastify.com/listen/feed/[THIS NUMBER]\n\n"
                "Your RadioReference.com username and password are used.",
                title="Broadcastify Setup",
                height=16
            )
            feed_id = WTail.inputbox(
                "Broadcastify Feed ID (number only):",
                title="Broadcastify"
            )
            if not feed_id:
                return False
            self.cfg['broadcastify_feed_id'] = feed_id.strip()

            user = WTail.inputbox(
                "RadioReference.com username:",
                default=self.cfg.get('broadcastify_user', ''),
                title="Broadcastify Credentials"
            )
            if user is None:
                return False
            self.cfg['broadcastify_user'] = user.strip()

            pw = WTail.passwordbox(
                "RadioReference.com password:",
                title="Broadcastify Credentials"
            )
            if pw is None:
                return False
            self.cfg['broadcastify_pass'] = pw

            # Test connection
            WTail.infobox("Testing Broadcastify stream...")
            from urllib.parse import quote
            u = quote(user.strip(), safe='')
            p = quote(pw, safe='')
            test_url = (
                "https://{}:{}@audio.broadcastify.com/{}.mp3".format(u, p, feed_id.strip())
            )
            ok = self._test_stream(test_url)
            if not ok:
                WTail.msgbox(
                    "Could not connect to stream.\n\n"
                    "Check your feed ID and credentials.\n"
                    "You can update these later in the config file.",
                    title="Stream Test Failed"
                )
        else:
            WTail.msgbox(
                "Find free NOAA WX streams at:\n\n"
                "  • https://www.noaaweatherradio.org/\n"
                "  • https://www.weatherusa.net/radio\n\n"
                "Copy the stream URL and paste it below.",
                title="Free Stream Sources",
                height=12
            )
            url = WTail.inputbox(
                "Stream URL (MP3, OGG, or other ffmpeg-compatible format):",
                default=self.cfg.get('stream_url', 'http://'),
                title="Stream URL",
                height=9
            )
            if not url:
                return False
            self.cfg['stream_url'] = url.strip()

            WTail.infobox("Testing stream connection...")
            ok = self._test_stream(url.strip())
            if not ok:
                WTail.msgbox(
                    "Stream test failed.\n\n"
                    "The URL may be incorrect or the stream offline.\n"
                    "You can update the URL later in the config file.",
                    title="Stream Test Failed"
                )
        return True

    def _test_stream(self, url, timeout: int = 8) -> bool:
        """Test stream reachability with ffmpeg."""
        try:
            proc = subprocess.run(
                ['ffmpeg', '-i', url, '-t', '3', '-f', 'null', '-',
                 '-loglevel', 'error'],
                capture_output=True, timeout=timeout
            )
            return proc.returncode == 0
        except Exception:
            return False

    def screen_device_id(self):
        """
        Pin the audio device to a stable hardware identifier so the
        EAS monitor finds the right device regardless of which USB port
        it is plugged into.

        USB audio dongles:  udev SYMLINK rule → hw:CARD=wx_radio
        RTL-SDR dongles:    rtl_eeprom serial → -d WXMON01
        USB Shared:         udev rule for the existing node device
        Stream:             No hardware to pin — skip this screen
        """
        src = self.cfg.get('audio_source', '')

        if src == 'stream':
            return True   # No hardware device

        if src == 'rtlsdr':
            return self._device_id_rtlsdr()
        else:
            return self._device_id_usb()

    def _device_id_usb(self):
        """Handle device identification for USB audio sources."""
        hw = self.cfg.get('device', '')
        if not hw:
            return True

        src  = self.cfg.get('audio_source', '')
        label = 'weather radio dongle' if src == 'usb_direct' else 'RIM-Lite / node interface'

        WTail.infobox("Probing USB attributes for {}...".format(hw))
        attrs = get_alsa_udev_attrs(hw)

        if not attrs.get('vendor'):
            WTail.msgbox(
                "Could not read USB attributes for {}.\n\nThe device will be addressed by its current ALSA card number\nwhich may change if USB devices are reordered at boot.\n\nIf this causes problems, unplug and replug the device,\nthen re-run the wizard.".format(hw),
                title="Device Probe Failed"
            )
            return True

        vendor  = attrs['vendor']
        product = attrs['product']
        serial  = attrs['serial']

        has_unique_serial = (serial and serial not in GENERIC_USB_SERIALS)

        if has_unique_serial:
            match_desc = "vendor:product:serial ({}:{}:{})".format(vendor, product, serial)
            quality    = "Excellent — unique per physical device, port-independent"
        else:
            match_desc = "vendor:product only ({}:{})".format(vendor, product)
            quality    = (
                "Good — works if only one device of this model is present.\n"
                "  ⚠  If you add another identical USB audio chip, both\n"
                "      will match and the wrong device may be used."
            )

        if not WTail.yesno(
            "Create a stable udev rule for your {}?\n\n  Device:  {}\n  Matches: {}\n  Quality: {}\n\nThis makes the device path permanent regardless of USB port.\nThe config will use 'hw:CARD=wx_radio' instead of 'hw:X,Y'.".format(label, hw, match_desc, quality),
            title="Stable Device Identification",
            yes_btn="Create Rule",
            no_btn="Skip"
        ):
            return True

        rule, desc = build_udev_rule(
            symlink_name = 'wx_radio',
            vendor  = vendor,
            product = product,
            serial  = serial,
            subsystem = 'sound'
        )

        # Accumulate rules — RTL-SDR may also add one
        self.cfg.setdefault('udev_rules', []).append(rule)
        self.cfg['device']       = 'hw:CARD=wx_radio'
        self.cfg['udev_written'] = True

        WTail.msgbox(
            "udev rule staged:\n\n  {}{}\n\nRule will be written to:\n  {}\n\nThe config will reference 'hw:CARD=wx_radio'.\nChanges take effect after the wizard applies configuration.".format(rule[:70], '...' if len(rule) > 70 else '', UDEV_RULES_FILE),
            title="udev Rule Ready",
            height=14
        )
        return True

    def _device_id_rtlsdr(self):
        """Handle RTL-SDR serial identification, with optional EEPROM write."""
        dev_index = self.cfg.get('rtl_device', 0)

        WTail.infobox("Reading RTL-SDR EEPROM (device {})...".format(dev_index))
        info = read_rtlsdr_eeprom(dev_index)

        if not info.get('vendor_id') and not info.get('serial') and not info.get('product'):
            WTail.msgbox(
                "Could not read RTL-SDR EEPROM.\n\n"
                "Ensure rtl_eeprom is installed (it comes with the rtl-sdr package).\n"
                "The device will be addressed by index number.\n\n"
                "If you have multiple dongles, this may cause the wrong one\n"
                "to be used if enumeration order changes.",
                title="EEPROM Read Failed"
            )
            return True

        current_serial = info.get('serial', '')
        is_generic     = current_serial in GENERIC_RTL_SERIALS
        suggested      = generate_wx_serial(1)

        if not is_generic:
            # Serial is already unique — just confirm and use it
            WTail.msgbox(
                "Your RTL-SDR dongle has a unique serial number:\n\n  Serial: {}\n\nThis will be used to identify the device instead of its\nUSB port number. You can now plug it into any USB port.".format(current_serial),
                title="Unique Serial Found"
            )
            self.cfg['rtl_serial']  = current_serial
            self.cfg['rtl_device']  = current_serial  # use serial as -d arg
            return True

        # Generic or absent serial — offer to write a new one
        choice = WTail.menu(
            "Your RTL-SDR has a generic serial number: '{}'\n\nThis is the factory default shared by most dongles.\nWithout a unique serial, the dongle is addressed by USB port\nnumber — moving it to a different port will break the config.\n\nWhat would you like to do?".format(current_serial),
            [
                ('write',    'Write a unique serial ({}) to the dongle\'s EEPROM'.format(suggested)),
                ('custom',   'Write a custom serial string I specify'),
                ('skip',     'Skip — use device index (port-dependent)'),
            ],
            title="RTL-SDR Serial Number"
        )

        if choice is None or choice == 'skip':
            WTail.msgbox(
                "Device will be addressed as index {}.\n\nNote: If this dongle is moved to a different USB port,\nupdate device_index in /etc/eas_monitor/fips_nodes.conf.".format(dev_index),
                title="Using Device Index"
            )
            return True

        if choice == 'custom':
            new_serial = WTail.inputbox(
                "Enter a serial string for this dongle:\n\n"
                "  • Up to 8 characters, letters and numbers only\n"
                "  • Must be unique among your dongles\n"
                "  • Examples: WXMON01, KXYZ01, WX1",
                default=suggested,
                title="Custom Serial"
            )
            if not new_serial:
                return False
            new_serial = new_serial.strip().upper()
            if not re.match(r'^[A-Z0-9]{1,8}$', new_serial):
                WTail.msgbox(
                    "Invalid serial. Use 1-8 alphanumeric characters.",
                    title="Invalid Input"
                )
                return self._device_id_rtlsdr()
        else:
            new_serial = suggested

        # Confirm before writing
        if not WTail.yesno(
            "Write serial '{}' to dongle EEPROM?\n\n  Dongle index : {}\n  New serial   : {}\n  Old serial   : {}\n\n⚠  After writing, you must UNPLUG and REPLUG the dongle.\n   The wizard will wait for you to do this.".format(new_serial, dev_index, new_serial, current_serial or '(empty)'),
            title="Confirm EEPROM Write",
            yes_btn="Write Serial",
            no_btn="Cancel"
        ):
            return True

        WTail.infobox(
            "Writing serial '{}' to RTL-SDR EEPROM...".format(new_serial)
        )
        ok = write_rtlsdr_serial(dev_index, new_serial)

        if not ok:
            WTail.msgbox(
                "EEPROM write failed.\n\n"
                "Possible causes:\n"
                "  • rtl_eeprom not installed\n"
                "  • Device is in use by another process\n"
                "  • Permission denied (run as root)\n\n"
                "Device will be addressed by index number.",
                title="Write Failed"
            )
            return True

        # Prompt to replug
        WTail.msgbox(
            "Serial '{}' written successfully.\n\nUNPLUG the dongle now, wait 2 seconds, then plug it back in.\n\nPress OK after replugging.".format(new_serial),
            title="Replug Dongle Required",
            height=12
        )

        # Wait for the new serial to appear
        WTail.infobox(
            "Waiting for dongle with serial '{}' to appear...".format(new_serial)
        )
        found = wait_for_rtlsdr_serial(new_serial, timeout=30)

        if found:
            WTail.msgbox(
                "Dongle detected with serial '{}'.\n\nThe config will use this serial as the device identifier.\nThe dongle can now be used in any USB port.".format(new_serial),
                title="Serial Confirmed"
            )
            self.cfg['rtl_serial'] = new_serial
            self.cfg['rtl_device'] = new_serial   # use serial as -d arg
        else:
            WTail.msgbox(
                "Dongle with serial '{}' not detected within 30s.\n\nThe serial was written — try replugging manually.\nDevice will be addressed by index until the next wizard run.".format(new_serial),
                title="Detection Timeout"
            )
            # Still store it — it'll work next time
            self.cfg['rtl_serial'] = new_serial
            self.cfg['rtl_device'] = dev_index  # fall back to index for now

        return True

    def screen_usrp_nodes(self):
        """Configure private USRP node numbers (skipped for usb_shared)."""
        src = self.cfg.get('audio_source', '')
        if src == 'usb_shared':
            # No USRP node needed — existing node carries audio
            self.cfg['usrp_nodes'] = {}
            return True

        frequencies = self.cfg.get('frequencies', ['162550000'])
        num_nodes   = len(frequencies) if src == 'rtlsdr' else 1

        WTail.msgbox(
            "You need {} private local node number(s) for audio routing.\n\nPrivate nodes are local-only (not registered with AllStarLink).\nUse numbers NOT in the public node directory — numbers like\n29901, 29902, etc. work well for private use.\n\nThese nodes will be added to your rpt.conf automatically.".format(num_nodes),
            title="Private USRP Nodes",
            height=14
        )

        usrp_nodes = {}
        tx_port = 34001
        rx_port = 32001

        if src == 'rtlsdr' and num_nodes > 1:
            for i, freq in enumerate(frequencies):
                node = WTail.inputbox(
                    "Private node number for {:.3f} MHz:".format(int(freq)/1e6),
                    default=str(29901 + i),
                    title="USRP Node {}/{}".format(i+1, num_nodes)
                )
                if node is None:
                    return False
                usrp_nodes[freq] = (tx_port + i*2, rx_port + i*2)
                # Store node number too
                usrp_nodes['{}_nodenum'.format(freq)] = node.strip()
        else:
            node = WTail.inputbox(
                "Private node number for EAS audio source:",
                default=str(self.cfg.get('usrp_node', '29901')),
                title="Private USRP Node"
            )
            if node is None:
                return False
            usrp_nodes['default'] = (tx_port, rx_port)
            usrp_nodes['default_nodenum'] = node.strip()

        self.cfg['usrp_nodes']  = usrp_nodes
        self.cfg['public_node'] = self.cfg['local_node']
        return True

    def screen_fips_setup(self):
        choice = WTail.menu(
            "How would you like to set up your coverage area?\n\n"
            "FIPS codes identify the counties your NOAA transmitter covers.",
            [
                ('zip',   'Enter ZIP code(s) — easiest, looks up counties'),
                ('fips',  'Enter FIPS codes directly — for advanced users'),
            ],
            title="Coverage Area Setup"
        )
        if choice is None:
            return None

        if choice == 'zip':
            return self._fips_from_zip()
        else:
            return self._fips_manual()

    def _fips_from_zip(self):
        zips_input = WTail.inputbox(
            "Enter ZIP code(s) for your coverage area:\n\n"
            "Separate multiple ZIP codes with commas.\n"
            "Example: 41101, 41102, 41105",
            default=self.cfg.get('zip_input', ''),
            title="ZIP Code Lookup"
        )
        if zips_input is None:
            return False

        self.cfg['zip_input'] = zips_input
        zips = [z.strip() for z in zips_input.split(',') if z.strip()]

        if not zips:
            WTail.msgbox("Please enter at least one ZIP code.", title="Error")
            return self._fips_from_zip()

        WTail.infobox("Looking up counties for {} ZIP code(s)...".format(len(zips)))

        all_counties = {}
        for z in zips:
            results = lookup_fips(z)
            for r in results:
                fips = r['fips']
                if fips not in all_counties:
                    state = STATE_FIPS.get(fips[:2], fips[:2])
                    all_counties[fips] = "{}, {}".format(r['county'], state)

        if not all_counties:
            WTail.msgbox(
                "Could not find counties for the ZIP code(s) entered.\n\n"
                "This may happen if:\n"
                "  • ZIP code is invalid\n"
                "  • No internet connection\n"
                "  • FIPS data file not downloaded yet\n\n"
                "Try entering FIPS codes manually instead.",
                title="Lookup Failed"
            )
            return self.screen_fips_setup()

        # Present checklist of found counties
        items = [
            (fips, name, True)
            for fips, name in sorted(all_counties.items())
        ]
        selected = WTail.checklist(
            "Confirm the counties to monitor:\n\n"
            "These are the counties that will trigger node connections\n"
            "when a SAME alert is received. Deselect counties you don't\n"
            "want to monitor.",
            items,
            title="Confirm Coverage Counties"
        )
        if selected is None:
            return False
        if not selected:
            WTail.msgbox("Select at least one county.", title="No Selection")
            return self._fips_from_zip()

        self.cfg['selected_fips'] = {
            fips: all_counties[fips] for fips in selected
        }
        return True

    def _fips_manual(self):
        WTail.msgbox(
            "FIPS code format: SSSCCC\n\n"
            "  SSS = 3-digit state FIPS code  (e.g. 021 = Kentucky)\n"
            "  CCC = 3-digit county FIPS code (e.g. 019 = Boyd County)\n\n"
            "Examples:\n"
            "  021019 = Boyd County, KY\n"
            "  039035 = Cuyahoga County, OH\n\n"
            "Find codes at: weather.gov/pimar/PubZoneFIPS",
            title="FIPS Code Reference",
            height=16
        )
        fips_input = WTail.inputbox(
            "Enter FIPS codes separated by commas:\n"
            "Example: 021019, 021089, 021127",
            default=self.cfg.get('fips_input', ''),
            title="Manual FIPS Entry"
        )
        if fips_input is None:
            return False

        fips_list = [
            f.strip() for f in fips_input.split(',')
            if re.match(r'^\d{6}$', f.strip())
        ]
        if not fips_list:
            WTail.msgbox(
                "No valid FIPS codes found.\n"
                "Each code must be exactly 6 digits.",
                title="Invalid Input"
            )
            return self._fips_manual()

        self.cfg['selected_fips'] = {
            fips: fips for fips in fips_list
        }
        self.cfg['fips_input'] = fips_input
        return True

    def screen_node_mapping(self):
        """Map each selected FIPS county to an ASL node number."""
        fips_map = self.cfg.get('selected_fips', {})
        if not fips_map:
            return True

        node_mapping = self.cfg.get('fips_map', {})

        WTail.msgbox(
            "You have {} county/counties selected.\n\nFor each county, enter the AllStarLink node number to connect\nwhen an alert is received for that county.\n\nMultiple counties can map to the same node number.".format(len(fips_map)),
            title="Node Mapping",
            height=12
        )

        for fips, name in sorted(fips_map.items()):
            existing = node_mapping.get(fips, '')
            node = WTail.inputbox(
                "ASL node to connect for:\n\n  {}\n  FIPS: {}\n\nEnter 0 to skip this county.".format(name, fips),
                default=existing,
                title="Node Mapping"
            )
            if node is None:
                return False
            if node.strip() and node.strip() != '0':
                node_mapping[fips] = node.strip()

        self.cfg['fips_map'] = node_mapping
        return True

    def screen_alert_types(self):
        warn_items = [
            (code, desc, True) for code, desc in sorted(WARNING_EVENTS.items())
        ]
        watch_items = [
            (code, desc, False) for code, desc in sorted(WATCH_EVENTS.items())
        ]
        test_items = [
            (code, desc, False) for code, desc in sorted(TEST_EVENTS.items())
        ]

        # Warnings
        w_sel = WTail.checklist(
            "Select WARNING event types to act on:\n"
            "(Imminent threat — recommended to keep all selected)",
            warn_items,
            title="Alert Types — Warnings"
        )
        if w_sel is None:
            return False

        # Watches
        wa_sel = WTail.checklist(
            "Select WATCH event types to act on:\n"
            "(Conditions favorable — connect nodes for these?)",
            watch_items,
            title="Alert Types — Watches"
        )
        if wa_sel is None:
            return False

        # Tests
        t_sel = WTail.checklist(
            "Select TEST event types to act on:\n"
            "(Usually leave unchecked in production)",
            test_items,
            title="Alert Types — Tests"
        )
        if t_sel is None:
            return False

        self.cfg['selected_warnings'] = w_sel or []
        self.cfg['selected_watches']  = wa_sel or []
        self.cfg['selected_tests']    = t_sel or []
        self.cfg['act_watches']       = bool(wa_sel)
        self.cfg['act_tests']         = bool(t_sel)
        return True

    def screen_alert_behavior(self):
        mode = WTail.radiolist(
            "How should alerts connect to nodes?\n\n"
            "Propagate: Audio reaches ALL nodes connected to your node\n"
            "           (repeater, linked nets, etc.)\n\n"
            "Local only: Audio heard on YOUR node only — not propagated",
            [
                ('propagate', 'Propagate — alert heard across connected network',
                 True),
                ('local',     'Local only — alert heard on this node only',
                 False),
                ('per_event', 'Per-event — configure each type separately',
                 False),
            ],
            title="Alert Behavior"
        )
        if mode is None:
            return False

        behavior = {}
        all_events = (
            self.cfg.get('selected_warnings', []) +
            self.cfg.get('selected_watches', []) +
            self.cfg.get('selected_tests', [])
        )

        if mode == 'per_event':
            for event in all_events:
                label = (
                    WARNING_EVENTS.get(event) or
                    WATCH_EVENTS.get(event) or
                    TEST_EVENTS.get(event, event)
                )
                m = WTail.radiolist(
                    "Behavior for {} ({}):".format(event, label),
                    [
                        ('propagate', 'Propagate to connected network', True),
                        ('local',     'Local node only', False),
                        ('skip',      'Skip — do not connect nodes', False),
                    ],
                    title="Behavior: {}".format(event)
                )
                behavior[event] = m or 'propagate'
        else:
            for event in all_events:
                behavior[event] = mode

        # National events always propagate
        for event in ('EAN', 'EAT', 'NIC'):
            behavior[event] = 'propagate'

        # Tests default to local if selected
        for event in self.cfg.get('selected_tests', []):
            if mode != 'per_event':
                behavior[event] = 'local'

        self.cfg['alert_behavior'] = behavior
        return True

    def screen_recording(self):
        if not WTail.yesno(
            "Enable alert audio recording?\n\n"
            "Records each alert to a .ulaw file for later playback.\n"
            "Playback via DTMF: *91 = most recent, *92 = previous, etc.",
            title="Alert Recording",
            default_yes=True
        ):
            self.cfg['recording_enabled'] = False
            self.cfg['max_recordings'] = 0
            return True

        n = WTail.inputbox(
            "How many past alerts to keep? (1–10, default 5):",
            default=str(self.cfg.get('max_recordings', 5)),
            title="Recording Buffer Size"
        )
        if n is None:
            return False
        try:
            n = max(1, min(10, int(n)))
        except ValueError:
            n = 5

        self.cfg['recording_enabled'] = True
        self.cfg['max_recordings']    = n
        return True

    def screen_review(self):
        src_labels = {
            'usb_shared': 'USB Shared (existing RIM-Lite node)',
            'usb_direct': 'USB Direct (dedicated weather radio + dongle)',
            'rtlsdr':     'RTL-SDR dongle',
            'stream':     'Internet stream',
        }

        fips_lines = '\n'.join(
            "  {} → node {}".format(fips, node)
            for fips, node in sorted(self.cfg.get('fips_map', {}).items())
        ) or "  (none configured)"

        behavior_lines = '\n'.join(
            "  {}: {}".format(event, mode)
            for event, mode in sorted(
                self.cfg.get('alert_behavior', {}).items()
            )
        ) or "  (default: propagate)"

        udev_lines = ''
        if self.cfg.get('udev_rules'):
            udev_lines = '\nudev rules to write:\n'
            for r in self.cfg['udev_rules']:
                udev_lines += "  {}{}\n".format(r[:72], '...' if len(r)>72 else '')

        summary = """EAS Monitor Configuration Summary
{}

Node:          {}
Audio source:  {}
AMI user:      {}

FIPS → Node mappings:
{}

Alert behavior:
{}

Recording: {}
{}
Files to be modified:
  • {}
  • {} (if USB shared)
  • /etc/asterisk/modules.conf (if not USB shared)
  • /etc/asterisk/rpt.conf (if not USB shared)
  • /etc/asterisk/extensions.conf (if not USB shared)
  • {}
""".format('='*40, self.cfg.get('local_node', '?'), src_labels.get(self.cfg.get('audio_source', ''), '?'), self.cfg.get('ami_user', '?'), fips_lines, behavior_lines, 'Enabled (' + str(self.cfg.get('max_recordings', 5)) + ' max)' if self.cfg.get('recording_enabled', True) else 'Disabled', udev_lines, CONFIG_FILE, ASOUND_CONF, SERVICE_FILE)
        WTail.textbox(summary, title="Configuration Review")

        return WTail.yesno(
            "Apply this configuration?\n\n"
            "All changes listed above will be made to your system.",
            title="Confirm",
            yes_btn="Apply",
            no_btn="Back"
        )

    def screen_apply(self):
        steps = [
            ("Creating directories...",
             lambda: Path(INSTALL_DIR).mkdir(parents=True, exist_ok=True)),

            ("Creating recording directory...",
             lambda: Path(RECORDING_DIR).mkdir(parents=True, exist_ok=True)),

            ("Writing configuration file...",
             lambda: write_config(self.cfg)),

            ("Setting config file permissions...",
             lambda: os.chmod(CONFIG_FILE, 0o600)),
        ]

        # Write udev rules if any were staged during device identification
        if self.cfg.get('udev_rules'):
            steps.append((
                "Writing udev rules to {}...".format(UDEV_RULES_FILE),
                lambda: write_udev_rules(self.cfg['udev_rules'])
            ))

        src = self.cfg.get('audio_source', '')

        if src == 'usb_shared':
            if self.cfg.get('dsnoop_ok', False):
                steps.append((
                    "Configuring ALSA dsnoop tap...",
                    lambda: setup_dsnoop(self.cfg.get('device', ''))
                ))
            else:
                steps.append((
                    "Loading snd-aloop kernel module...",
                    lambda: setup_snd_aloop()
                ))
        else:
            steps.append((
                "Enabling chan_usrp in modules.conf...",
                lambda: enable_chan_usrp()
            ))

            # Add USRP nodes to Asterisk
            usrp_nodes = self.cfg.get('usrp_nodes', {})
            for key, ports in usrp_nodes.items():
                if key.endswith('_nodenum'):
                    continue
                nodenum_key = '{}_nodenum'.format(key)
                nodenum = usrp_nodes.get(nodenum_key, '29901')
                if isinstance(ports, tuple):
                    tx, rx = ports
                    steps.append((
                        "Adding USRP node {} to rpt.conf...".format(nodenum),
                        lambda nn=nodenum, t=tx, r=rx:
                            add_usrp_node_to_asterisk(nn, t, r)
                    ))

        if self.cfg.get('recording_enabled', True):
            steps.append((
                "Adding DTMF playback commands to rpt.conf...",
                lambda: add_dtmf_playback_commands(
                    self.cfg.get('public_node', self.cfg['local_node']),
                    self.cfg.get('max_recordings', 5)
                )
            ))

        steps += [
            ("Reloading Asterisk...",
             lambda: reload_asterisk()),

            ("Installing systemd service...",
             lambda: install_service(self._install_dir)),
        ]

        WTail.gauge("Applying configuration...", steps)

        WTail.msgbox(
            "Configuration applied successfully.\n\n"
            "Review any errors above before continuing.",
            title="Apply Complete"
        )
        return True

    def screen_test(self):
        test_sample = Path(INSTALL_DIR) / 'test' / 'same_test.wav'

        if not test_sample.exists():
            if WTail.yesno(
                "No test sample found.\n\n"
                "Skip the decode test and proceed to completion?",
                title="Test Sample Missing",
                yes_btn="Skip",
                no_btn="Back"
            ):
                return True
            return False

        if not WTail.yesno(
            "Run a SAME decode test using the bundled test sample?\n\n"
            "This verifies that multimon-ng can decode SAME headers\n"
            "from your audio pipeline.",
            title="Decode Test",
            yes_btn="Run Test",
            no_btn="Skip"
        ):
            return True

        WTail.infobox("Running decode test...")
        try:
            proc = subprocess.run(
                "sox {} -t raw -r 22050 -e signed -b 16 -c 1 - | multimon-ng -t raw -a EAS -".format(test_sample),
                shell=True, capture_output=True, text=True, timeout=15
            )
            output = proc.stdout + proc.stderr
            if 'ZCZC' in output:
                WTail.msgbox(
                    "SAME decode test PASSED.\n\n"
                    "multimon-ng successfully decoded a SAME header.\n"
                    "Your installation is ready.",
                    title="Test Passed"
                )
            else:
                WTail.msgbox(
                    "SAME decode test FAILED.\n\n"
                    "multimon-ng did not detect a SAME header.\n\n"
                    "Common causes:\n"
                    "  • multimon-ng not installed\n"
                    "  • sox not installed\n"
                    "  • Test sample file is corrupt\n\n"
                    "Check: sudo journalctl -u eas-monitor -f",
                    title="Test Failed",
                    height=16
                )
        except Exception as e:
            WTail.msgbox(
                "Test error: {}\n\nCheck that sox and multimon-ng are installed.".format(e),
                title="Test Error"
            )
        return True

    def screen_done(self):
        WTail.msgbox(
            "Setup complete!\n\nNext steps:\n\n  1. Start the service:\n     systemctl start eas-monitor\n\n  2. Watch the logs:\n     journalctl -u eas-monitor -f\n\n  3. Edit FIPS map or alert settings:\n     nano {}\n\n  4. Test alert playback (after first real alert):\n     DTMF *91 on your node = most recent alert\n\nThank you for using EAS/SAME AllStarLink Monitor.".format(CONFIG_FILE),
            title="Setup Complete",
            height=20
        )
        return True


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='EAS/SAME AllStarLink Monitor Setup Wizard'
    )
    parser.add_argument(
        '--update', action='store_true',
        help='Update mode — preserves existing FIPS map'
    )
    args = parser.parse_args()

    wizard = EASWizard(update_mode=args.update)
    wizard.run()


if __name__ == '__main__':
    main()
