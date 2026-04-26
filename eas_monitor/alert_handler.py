"""
alert_handler.py - SAME/EAS Alert Handler
Python 3.5 compatible.
"""

import configparser
import logging
import re
import time

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

ALL_EVENTS = {}
ALL_EVENTS.update(WARNING_EVENTS)
ALL_EVENTS.update(WATCH_EVENTS)
ALL_EVENTS.update(NATIONAL_EVENTS)
ALL_EVENTS.update(TEST_EVENTS)

SAME_RE = re.compile(
    r'ZCZC-(?P<org>\w+)-(?P<event>\w+)-(?P<fips>[\d\-]+)'
    r'\+(?P<purge>\d{4})-(?P<issued>\d{7})-(?P<callsign>[^\-\s]+)-?'
)

DEDUP_WINDOW = 120


def purge_to_seconds(hhmm):
    try:
        hh = int(hhmm[:2])
        mm = int(hhmm[2:])
        return hh * 3600 + mm * 60
    except (ValueError, IndexError):
        return 3600


def parse_same_header(line):
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


class AlertHandler(object):

    def __init__(self, config, link_mgr, recorder=None,
                 usrp_sink=None, log=None):
        self.link_mgr  = link_mgr
        self.recorder  = recorder
        self.usrp_sink = usrp_sink
        self.log       = log or logging.getLogger('eas_monitor.handler')

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

        self._seen = {}

    def _event_id(self, p):
        return "%s-%s-%s-%s-%s" % (
            p['org'], p['event'],
            '-'.join(sorted(p['fips_codes'])),
            p['issued'], p['callsign']
        )

    def _is_duplicate(self, event_id):
        now = time.time()
        if event_id in self._seen:
            if now - self._seen[event_id] < DEDUP_WINDOW:
                return True
        self._seen[event_id] = now
        self._seen = {
            k: v for k, v in self._seen.items()
            if now - v < DEDUP_WINDOW * 2
        }
        return False

    def _should_act(self, event):
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

    def _get_mode(self, event):
        behavior = self.behavior.get(event, 'propagate').lower()
        return 'local' if behavior == 'local' else 'propagate'

    def _resolve_nodes(self, fips_codes):
        nodes = set()
        for fips in fips_codes:
            for key in (fips, fips[:3] + '000'):
                if key in self.fips_map:
                    # Value may be comma-separated: "496081,496082"
                    for node in self.fips_map[key].split(','):
                        node = node.strip()
                        if node:
                            nodes.add(node)
        return nodes

    def handle_header(self, line):
        parsed = parse_same_header(line)
        if not parsed:
            self.log.debug("Unparseable SAME line: %s", line[:80])
            return

        event_id = self._event_id(parsed)
        if self._is_duplicate(event_id):
            self.log.debug("Duplicate SAME header suppressed: %s",
                           event_id[:60])
            return

        event = parsed['event']
        self.log.info(
            "ALERT: %s (%s) | Org: %s | FIPS: %s | "
            "Purge: %dmin | From: %s",
            event, ALL_EVENTS.get(event, 'Unknown'),
            parsed['org'], parsed['fips_codes'],
            parsed['purge_secs'] // 60, parsed['callsign']
        )

        if not self._should_act(event):
            self.log.info("Event '%s' not in active set -- no action", event)
            return

        mode       = self._get_mode(event)
        purge_secs = parsed['purge_secs']

        if self.usrp_sink:
            self.usrp_sink.key_up()

        if self.recorder:
            self.recorder.start(
                event=event,
                fips_codes=parsed['fips_codes'],
                callsign=parsed['callsign']
            )

        if event in self.event_override:
            target = self.event_override[event]
            self.link_mgr.connect(target, purge_secs,
                                  mode='propagate', event=event)
            return

        if mode == 'local':
            self.link_mgr.connect(None, purge_secs,
                                  mode='local', event=event)
            return

        nodes = self._resolve_nodes(parsed['fips_codes'])
        if not nodes:
            self.log.info(
                "No FIPS mapping for %s -- no nodes connected",
                parsed['fips_codes']
            )
            return

        for node in nodes:
            self.link_mgr.connect(node, purge_secs,
                                  mode='propagate', event=event)

    def handle_eom(self):
        self.log.info("EOM received")
        if self.recorder and self.recorder.is_active:
            path = self.recorder.stop()
            if path:
                self.log.info("Alert recording saved: %s.ulaw", path)
        if self.usrp_sink:
            self.usrp_sink.key_down()
        self.link_mgr.disconnect_all()

    def check_timeouts(self):
        if self.link_mgr.has_active_links:
            self.link_mgr.check_timeouts()
        if not self.link_mgr.has_active_links:
            if self.recorder and self.recorder.is_active:
                path = self.recorder.stop()
                if path:
                    self.log.info(
                        "Alert recording saved (timeout): %s.ulaw", path
                    )
            if self.usrp_sink and self.usrp_sink.is_keyed:
                self.usrp_sink.key_down()
