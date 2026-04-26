"""
alert_handler.py - SAME/EAS Alert Handler
==========================================
Parses multimon-ng output, decodes SAME headers, filters events,
and orchestrates node connections and audio recording.
"""

import configparser
import logging
import re
import time


# ── EAS Event Code Definitions ────────────────────────────────────────────

WARNING_EVENTS = {
    'TOR': 'Tornado Warning',
    'SVR': 'Severe Thunderstorm Warning',
    'FFW': 'Flash Flood Warning',
    'EWW': 'Extreme Wind Warning',
    'HUW': 'Hurricane Warning',
    'HLS': 'Hurricane Local Statement',
    'SMW': 'Special Marine Warning',
    'SQW': 'Snow Squall Warning',
    'DSW': 'Dust Storm Warning',
    'BZW': 'Blizzard Warning',
    'WSW': 'Winter Storm Warning',
    'ICW': 'Ice Storm Warning',
    'FRW': 'Fire Warning',
    'VOW': 'Volcano Warning',
    'TRW': 'Tropical Storm Warning',
    'LEW': 'Law Enforcement Warning',
    'CEM': 'Civil Emergency Message',
}

WATCH_EVENTS = {
    'TOA': 'Tornado Watch',
    'SVA': 'Severe Thunderstorm Watch',
    'FFA': 'Flash Flood Watch',
    'HUA': 'Hurricane Watch',
    'TRA': 'Tropical Storm Watch',
    'BZA': 'Blizzard Watch',
    'WSA': 'Winter Storm Watch',
    'FLA': 'Flash Freeze Watch',
}

NATIONAL_EVENTS = {
    'EAN': 'Emergency Action Notification (Presidential)',
    'EAT': 'Emergency Action Termination',
    'NIC': 'National Information Center',
    'NPT': 'National Periodic Test',
}

TEST_EVENTS = {
    'RMT': 'Required Monthly Test',
    'RWT': 'Required Weekly Test',
}

ALL_EVENTS = {**WARNING_EVENTS, **WATCH_EVENTS, **NATIONAL_EVENTS, **TEST_EVENTS}

# ── SAME Header Regex ──────────────────────────────────────────────────────

SAME_RE = re.compile(
    r'ZCZC-(?P<org>\w+)-(?P<event>\w+)-(?P<fips>[\d\-]+)'
    r'\+(?P<purge>\d{4})-(?P<issued>\d{7})-(?P<callsign>[^\-\s]+)-?'
)

DEDUP_WINDOW = 120   # seconds — SAME sends header 3x, suppress dupes


def purge_to_seconds(hhmm: str) -> int:
    """Convert SAME purge time (HHMM) to seconds."""
    try:
        hh = int(hhmm[:2])
        mm = int(hhmm[2:])
        return hh * 3600 + mm * 60
    except (ValueError, IndexError):
        return 3600


def parse_same_header(line: str) -> dict | None:
    """
    Parse a SAME header line from multimon-ng output.
    multimon-ng prefix: 'EAS: ' optionally preceded by a timestamp.
    Returns a dict or None if unparseable.
    """
    # Strip multimon-ng timestamp prefix if present
    # Format: "YYYY-MM-DD HH:MM:SS:mmm -- EAS: ZCZC-..."
    clean = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d+ -- ', '', line)
    clean = clean.replace('EAS: ', '').strip()

    m = SAME_RE.search(clean)
    if not m:
        return None

    fips_codes = re.findall(r'\d{6}', m.group('fips'))
    purge_hhmm = m.group('purge')

    return {
        'org':        m.group('org'),
        'event':      m.group('event'),
        'fips_codes': fips_codes,
        'purge_hhmm': purge_hhmm,
        'purge_secs': purge_to_seconds(purge_hhmm),
        'issued':     m.group('issued'),
        'callsign':   m.group('callsign').strip(),
        'raw':        clean,
    }


class AlertHandler:
    """
    Central alert processing engine.

    Receives decoded SAME headers from the pipeline, filters them,
    deduplicates them, and orchestrates:
      - Node connections via LinkManager
      - Audio recording via AlertRecorder
      - USRP PTT control via USRPSink
    """

    def __init__(self, config: configparser.ConfigParser,
                 link_mgr,
                 recorder=None,
                 usrp_sink=None,
                 log: logging.Logger = None):
        self.link_mgr  = link_mgr
        self.recorder  = recorder
        self.usrp_sink = usrp_sink
        self.log       = log or logging.getLogger('eas_monitor.handler')

        # Load config
        self.fips_map       = dict(config['fips_map']) \
                              if 'fips_map' in config else {}
        self.event_override = dict(config['event_node_override']) \
                              if 'event_node_override' in config else {}
        self.behavior       = dict(config['alert_behavior']) \
                              if 'alert_behavior' in config else {}

        self.act_warnings = config.getboolean(
            'settings', 'act_on_warnings', fallback=True)
        self.act_watches  = config.getboolean(
            'settings', 'act_on_watches',  fallback=False)
        self.act_tests    = config.getboolean(
            'settings', 'act_on_tests',    fallback=False)

        # Deduplication: event_id → timestamp
        self._seen: dict[str, float] = {}

    # ── Deduplication ──────────────────────────────────────────────────────

    def _event_id(self, p: dict) -> str:
        """
        Unique ID for deduplication.
        Includes callsign so simultaneous alerts from different NWS offices
        serving the same area are not incorrectly suppressed.
        """
        return (
            f"{p['org']}-{p['event']}-"
            f"{'-'.join(sorted(p['fips_codes']))}-"
            f"{p['issued']}-{p['callsign']}"
        )

    def _is_duplicate(self, event_id: str) -> bool:
        now = time.time()
        if event_id in self._seen:
            if now - self._seen[event_id] < DEDUP_WINDOW:
                return True
        self._seen[event_id] = now
        # Clean expired entries
        self._seen = {
            k: v for k, v in self._seen.items()
            if now - v < DEDUP_WINDOW * 2
        }
        return False

    # ── Event filtering ─────────────────────────────────────────────────────

    def _should_act(self, event: str) -> bool:
        """Returns True if this event type should trigger node connections."""
        behavior = self.behavior.get(event, '').lower()
        if behavior == 'skip':
            return False
        if event in NATIONAL_EVENTS:
            return True
        if event in WARNING_EVENTS and self.act_warnings:
            return True
        if event in WATCH_EVENTS and self.act_watches:
            return True
        if event in TEST_EVENTS and self.act_tests:
            return True
        return False

    def _get_mode(self, event: str) -> str:
        """Return 'propagate' or 'local' for this event type."""
        behavior = self.behavior.get(event, 'propagate').lower()
        return 'local' if behavior == 'local' else 'propagate'

    # ── FIPS matching ───────────────────────────────────────────────────────

    def _resolve_nodes(self, fips_codes: list) -> set:
        """
        Resolve a list of FIPS codes to target ASL node numbers.
        Supports exact county match and state wildcard (SSS000).
        """
        nodes = set()
        for fips in fips_codes:
            # Exact county match
            if fips in self.fips_map:
                nodes.add(self.fips_map[fips])
            # State wildcard: e.g., 039000 matches all Ohio counties
            state_wild = fips[:3] + '000'
            if state_wild in self.fips_map:
                nodes.add(self.fips_map[state_wild])
        return nodes

    # ── Main event handlers ─────────────────────────────────────────────────

    def handle_header(self, line: str):
        """Process a SAME header line from multimon-ng."""
        parsed = parse_same_header(line)
        if not parsed:
            self.log.debug(f"Unparseable SAME line: {line[:80]}")
            return

        event_id = self._event_id(parsed)
        if self._is_duplicate(event_id):
            self.log.debug(f"Duplicate SAME header suppressed: {event_id[:60]}")
            return

        event = parsed['event']
        self.log.info(
            f"ALERT: {event} ({ALL_EVENTS.get(event, 'Unknown')}) | "
            f"Org: {parsed['org']} | FIPS: {parsed['fips_codes']} | "
            f"Purge: {parsed['purge_secs']//60}min | "
            f"From: {parsed['callsign']}"
        )

        if not self._should_act(event):
            self.log.info(f"Event '{event}' not in active set — no action")
            return

        mode         = self._get_mode(event)
        purge_secs   = parsed['purge_secs']

        # Start USRP audio if applicable
        if self.usrp_sink:
            self.usrp_sink.key_up()

        # Start recording
        if self.recorder:
            self.recorder.start(
                event=event,
                fips_codes=parsed['fips_codes'],
                callsign=parsed['callsign']
            )

        # National/presidential event override
        if event in self.event_override:
            target = self.event_override[event]
            self.link_mgr.connect(target, purge_secs, mode='propagate',
                                  event=event)
            return

        # Local mode — connect public node to local EAS node, no remote nodes
        if mode == 'local':
            self.link_mgr.connect(None, purge_secs, mode='local', event=event)
            return

        # Propagate mode — connect to FIPS-mapped remote nodes
        nodes = self._resolve_nodes(parsed['fips_codes'])
        if not nodes:
            self.log.info(
                f"No FIPS mapping for {parsed['fips_codes']} — "
                f"no nodes connected"
            )
            # Still key up USRP and record even if no remote nodes configured
            return

        for node in nodes:
            self.link_mgr.connect(node, purge_secs, mode='propagate',
                                  event=event)

    def handle_eom(self):
        """Process End-of-Message (NNNN) from multimon-ng."""
        self.log.info("EOM received")

        # Stop recording
        if self.recorder and self.recorder.is_active:
            path = self.recorder.stop()
            if path:
                self.log.info(f"Alert recording saved: {path}.ulaw")

        # Drop USRP PTT
        if self.usrp_sink:
            self.usrp_sink.key_down()

        # Disconnect all nodes
        self.link_mgr.disconnect_all()

    def check_timeouts(self):
        """Call periodically to disconnect expired links."""
        if self.link_mgr.has_active_links:
            self.link_mgr.check_timeouts()

        # If all links disconnected by timeout, clean up audio
        if not self.link_mgr.has_active_links:
            if self.recorder and self.recorder.is_active:
                path = self.recorder.stop()
                if path:
                    self.log.info(f"Alert recording saved (timeout): {path}.ulaw")
            if self.usrp_sink and self.usrp_sink.is_keyed:
                self.usrp_sink.key_down()
