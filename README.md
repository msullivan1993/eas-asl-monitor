# EAS/SAME AllStarLink Monitor

Monitors NOAA Weather Radio for SAME/EAS alerts, decodes FIPS codes,
and automatically connects AllStarLink nodes for the affected area.
Disconnects cleanly on End-of-Message or purge time expiry.
Records alerts to a rotating audio buffer for DTMF playback.

---

## Supported Platforms

| Platform | OS | Status |
|---|---|---|
| HamVoIP | Arch Linux ARM (Raspberry Pi) | ✅ Supported |
| AllStarLink 3 | Debian / Raspberry Pi OS | ✅ Supported |
| Standard ASL | Debian / Ubuntu | ✅ Supported |

---

## Audio Sources

| Source | Description | Hardware Needed |
|---|---|---|
| `usb_shared` | Existing RIM-Lite / radio interface | None (already present) |
| `usb_direct` | Dedicated weather radio + USB dongle | ~$35-50 |
| `rtlsdr` | RTL-SDR USB dongle | ~$25 |
| `stream` | Internet stream (Broadcastify or free) | None |

---

## Quick Install

```bash
git clone https://github.com/msullivan1993/eas-asl-monitor.git
cd eas-asl-monitor
sudo ./install.sh
```

The installer:
1. Detects HamVoIP vs Debian and installs dependencies
2. Builds `multimon-ng` from source on HamVoIP (not in pacman)
3. Downloads FIPS ZIP-to-county reference data from Census Bureau
4. Downloads a SAME test audio sample
5. Runs the interactive setup wizard
6. Installs and enables the systemd service

### Update

```bash
cd eas-asl-monitor && git pull
sudo ./install.sh --update
```

---

## Setup Wizard

The wizard walks through every configuration step interactively:

```
1. Node number and AMI credentials  (tested live)
2. Audio source selection
3. Source-specific configuration    (device, frequency, stream URL)
4. Private USRP nodes               (if needed for your source)
5. Coverage area via ZIP code       (Census API lookup → county checklist)
   or manual FIPS entry
6. FIPS → ASL node mapping          (one entry per county)
7. Alert event types                (warnings, watches, tests)
8. Alert behavior                   (propagate network-wide or local only)
9. Alert recording                  (rotating buffer, DTMF playback)
10. Review → Apply → Test → Done
```

Run the wizard at any time:
```bash
sudo python3 /etc/eas_monitor/setup_wizard.py
```

---

## How It Works

```
[NOAA WX Radio]
      │
      │ Audio
      ▼
[Audio Source] ──────────────────────────────────────────────┐
  usb_shared:  ALSA dsnoop tap on existing node              │
  usb_direct:  Direct ALSA capture from dedicated dongle     │
  rtlsdr:      rtl_fm (single freq) or numpy wideband demod  │
  stream:      ffmpeg decoding Icecast/HTTP stream            │
                                                              │
      │ Raw PCM 22050Hz                                       │
      ▼                                                       │
[multimon-ng] ──► SAME header decoded ──► [Alert Handler]    │
                                                │             │
                         ┌──────────────────────┘             │
                         │                                    │
                         ▼                                    │
                  [Link Manager]                              │
                  ilink 3 (propagate)                         │
                  ilink 8 (local only)                        │
                         │                                    │
                         ▼                                    │
                  [AMI → Asterisk]                            │
                         │                                    │
              Remote ASL nodes connected                      │
                                                              │
      │ Raw PCM 22050Hz (during alert only)                   │
      └──► [Alert Recorder] → ulaw files → DTMF playback      │
      └──► [USRP Sink] → UDP → Private ASL node (SDR/stream)──┘
```

**For `usb_shared`:** The existing node already carries audio. The monitor
only controls which nodes to connect via AMI.

**For `usb_direct`, `rtlsdr`, `stream`:** A private local USRP node receives
the demodulated audio. PTT is controlled by the monitor — the node is silent
between alerts, preventing continuous weather broadcasts from reaching
connected nodes.

---

## Alert Behavior

### Propagation Modes

| Mode | ilink command | Effect |
|---|---|---|
| `propagate` | ilink 3 (transceive) | Alert audio reaches destination node's entire connected network |
| `local` | ilink 8 (local monitor) | Alert heard on your node only — not retransmitted |
| `skip` | none | No connection made for this event type |

Configure per event type in `/etc/eas_monitor/fips_nodes.conf`:
```ini
[alert_behavior]
TOR = propagate   # Tornado Warning — reach whole network
SVA = local       # Severe T-Storm Watch — local only
RMT = skip        # Monthly test — don't connect
```

### Event Types

**Warnings (act_on_warnings = true):** TOR, SVR, FFW, EWW, HUW, SMW, SQW, DSW, BZW, WSW, CEM

**Watches (act_on_watches = false):** TOA, SVA, FFA, HUA, WSA, BZA

**National:** EAN, EAT, NIC (always act — presidential/national)

**Tests:** RMT, RWT, NPT (act_on_tests = false by default)

---

## Alert Recording & Playback

Each alert is recorded as an 8kHz mono ulaw file (Asterisk native format).
The last N alerts are kept (configurable, default 5).

**DTMF playback on your node:**
```
*91  = most recent alert
*92  = second most recent
*93  = third
*94  = fourth
*95  = fifth (oldest)
```

Recordings stored at: `/var/lib/eas_monitor/recordings/`

---

## Service Management

```bash
# Start
systemctl start eas-monitor

# Stop
systemctl stop eas-monitor

# Status
systemctl status eas-monitor

# Live logs
journalctl -u eas-monitor -f

# Log file
tail -f /var/log/eas_monitor.log
```

---

## RTL-SDR Setup

### Single frequency (recommended for Pi 3B)
```ini
[source_rtlsdr]
frequencies    = 162550000   # Your local NOAA transmitter
gain           = 40
ppm_correction = 0           # Calibrate with: rtl_test -t
```
CPU: ~5% on Pi 3B.

### Multiple frequencies (Pi 4 recommended for 3+)
```ini
[source_rtlsdr]
frequencies = 162550000, 162400000, 162475000
gain        = 40
```
Uses numpy wideband demodulation. CPU: ~20-35% on Pi 3B per added channel.

### PPM Calibration
```bash
rtl_test -t
# Note the "estimated error" and enter it as ppm_correction
```

---

## Broadcastify Stream Setup

1. Get a RadioReference.com Premium subscription (~$30/year)
2. Find your NOAA WX feed at `broadcastify.com/search/?q=noaa+weather+radio`
3. Note the Feed ID (number at end of URL)
4. Configure:

```ini
[source_stream]
broadcastify_feed_id = 34002
broadcastify_username = your_rr_username
broadcastify_password = your_rr_password
```

Credentials are stored with file permissions 600 and never logged.

---

## Troubleshooting

**Service won't start:**
```bash
journalctl -u eas-monitor -n 50 --no-pager
```

**No SAME headers decoded:**
```bash
# Test multimon-ng manually
sox /etc/eas_monitor/test/same_test.wav -t raw -r 22050 -e signed -b 16 -c 1 - | \
    multimon-ng -t raw -a EAS -
# Should show: EAS: ZCZC-WXR-RWT-...
```

**AMI connection refused:**
```bash
# Check Asterisk is running
systemctl status asterisk
# Test AMI manually
asterisk -rx "manager show connected"
```

**USB audio device not found:**
```bash
arecord -l    # List capture devices
aplay -l      # List playback devices
```

**RTL-SDR not detected:**
```bash
rtl_test -t   # Should list device(s)
lsusb         # Check USB enumeration
```

---

## Files Modified by Installer

| File | When |
|---|---|
| `/etc/eas_monitor/fips_nodes.conf` | Always |
| `/etc/asound.conf` | usb_shared source only |
| `/etc/modules-load.d/snd-aloop.conf` | usb_shared (loopback fallback) |
| `/etc/asterisk/modules.conf` | usb_direct, rtlsdr, stream |
| `/etc/asterisk/rpt.conf` | usb_direct, rtlsdr, stream |
| `/etc/asterisk/extensions.conf` | usb_direct, rtlsdr, stream |
| `/etc/systemd/system/eas-monitor.service` | Always |

---

## License

MIT — see LICENSE file.

## Contributing

Pull requests welcome. To add a new audio source:
1. Create `eas_monitor/sources/yoursource.py` with `needs_usrp` and `get_process()`
2. Add it to `sources/__init__.py`
3. Add config section to `fips_nodes.conf.example`
4. Add wizard screen to `setup_wizard.py`
