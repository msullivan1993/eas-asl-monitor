#!/usr/bin/env python3
"""
eas_monitor.py - EAS/SAME AllStarLink Monitor — Main Entry Point
=================================================================
Hardening:
  - SIGTERM/SIGHUP handler releases PTT and disconnects all nodes before exit
  - PTT key_down() at startup in case a previous crash left it keyed
  - Audio conversion dependency check at startup — hard fail with clear
    message rather than silently producing corrupt ulaw recordings
  - chan_usrp.so presence check for sources that need it
  - RTL-SDR DVB kernel driver conflict detection
  - All restart-loop iterations catch exceptions independently so one
    bad restart cycle never kills the service permanently
"""

import argparse
import configparser
import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from ami           import AsteriskAMI
from alert_handler import AlertHandler
from link_manager  import LinkManager
from recorder      import AlertRecorder, CONVERSION_AVAILABLE, CONVERSION_METHOD
from usrp_sink     import USRPSink
from pipeline      import SimplePipeline
from sources       import get_source

DEFAULT_CONFIG = '/etc/eas_monitor/fips_nodes.conf'
DEFAULT_LOG    = '/var/log/eas_monitor.log'
RESTART_DELAY  = 15   # seconds between pipeline restarts


# ── Logging ────────────────────────────────────────────────────────────────

def setup_logging(log_file: str, verbose: bool = False) -> logging.Logger:
    level   = logging.DEBUG if verbose else logging.INFO
    fmt     = '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except PermissionError:
        pass
    logging.basicConfig(
        level=level, format=fmt, datefmt=datefmt,
        handlers=handlers, force=True
    )
    return logging.getLogger('eas_monitor')


# ── Config ─────────────────────────────────────────────────────────────────

def load_config(path: str) -> configparser.ConfigParser:
    if not os.path.exists(path):
        print(f"[ERROR] Config not found: {path}")
        print(f"        Run: sudo python3 setup_wizard.py")
        sys.exit(1)
    config = configparser.ConfigParser()
    config.read(path)
    if 'settings' not in config:
        print(f"[ERROR] Config missing [settings] section: {path}")
        sys.exit(1)
    return config


# ── USRP sinks ─────────────────────────────────────────────────────────────

def build_usrp_sinks(config: configparser.ConfigParser,
                     log: logging.Logger) -> dict:
    source_type = config.get('settings', 'audio_source', fallback='').lower()
    if source_type == 'usb_shared':
        return {}
    if 'usrp_nodes' not in config:
        log.warning("No [usrp_nodes] section — USRP audio injection disabled")
        return {}
    sinks = {}
    for key, value in config['usrp_nodes'].items():
        try:
            tx, rx = (int(p.strip()) for p in value.split(':'))
            sinks[key] = USRPSink(tx_port=tx, rx_port=rx, log=log)
            log.debug(f"USRP sink: {key}  TX:{tx}  RX:{rx}")
        except Exception as e:
            log.error(f"Invalid [usrp_nodes] entry '{key} = {value}': {e}")
    return sinks


# ── Startup checks ─────────────────────────────────────────────────────────

def check_dependencies(source_type: str, ami: AsteriskAMI,
                       log: logging.Logger) -> bool:
    """
    Run pre-flight checks. Returns False if a fatal problem is found.
    Logs warnings for non-fatal issues.
    """
    ok = True

    # Audio conversion library
    if not CONVERSION_AVAILABLE:
        log.error(
            "FATAL: Audio recording disabled — neither audioop nor numpy "
            "is available. Install numpy: "
            "pip install numpy --break-system-packages\n"
            "Recording will not work. Set recording.enabled = false to "
            "suppress this warning."
        )
        # Not fatal for monitoring itself, just recording
        # ok = False  # uncomment to make it fatal

    else:
        log.info(f"Audio conversion: {CONVERSION_METHOD}")

    # chan_usrp required for non-USB-shared sources
    if source_type not in ('usb_shared', 'stream'):
        if not ami.is_module_loaded('chan_usrp'):
            log.error(
                "chan_usrp.so is not loaded in Asterisk. "
                "Add 'load => chan_usrp.so' to /etc/asterisk/modules.conf "
                "and reload Asterisk. "
                "Alert audio will not reach connected nodes."
            )
            # Not fatal — monitoring still works, just no audio injection
        else:
            log.info("chan_usrp.so: loaded OK")

    # RTL-SDR DVB driver conflict
    if source_type == 'rtlsdr':
        _check_dvb_conflict(log)

    return ok


def _check_dvb_conflict(log: logging.Logger):
    """
    The kernel module dvb_usb_rtl28xxu auto-claims RTL-SDR devices on some
    systems, preventing rtl_fm from opening them (usb_claim_interface error -6).
    Check for the module and warn if it's loaded.
    """
    try:
        with open('/proc/modules', 'r') as f:
            modules = f.read()
        if 'dvb_usb_rtl28xxu' in modules or 'rtl2832' in modules:
            log.warning(
                "DVB kernel driver is loaded and may conflict with rtl_fm. "
                "If rtl_fm fails to open the device, run:\n"
                "  sudo modprobe -r dvb_usb_rtl28xxu rtl2832\n"
                "To permanently blacklist it, the installer creates:\n"
                "  /etc/modprobe.d/rtlsdr-blacklist.conf"
            )
    except Exception:
        pass


# ── Signal handling ────────────────────────────────────────────────────────

class _ShutdownState:
    """
    Holds references to components that need cleanup on shutdown.
    Populated after all components are built so the signal handler
    can safely reference them.
    """
    link_mgr   = None
    usrp_sinks = []
    recorder   = None
    pipeline   = None
    log        = None

_shutdown = _ShutdownState()


def _handle_signal(signum, frame):
    """
    SIGTERM / SIGHUP handler.
    Releases PTT, disconnects all nodes, then exits cleanly.
    systemd waits up to TimeoutStopSec before sending SIGKILL.
    """
    sig_name = {signal.SIGTERM: 'SIGTERM', signal.SIGHUP: 'SIGHUP'}.get(
        signum, str(signum)
    )
    if _shutdown.log:
        _shutdown.log.info(f"Received {sig_name} — shutting down cleanly")

    # Release PTT first — most important, prevents stuck keyed node
    for sink in _shutdown.usrp_sinks:
        try:
            sink.key_down()
        except Exception:
            pass

    # Disconnect all linked nodes
    if _shutdown.link_mgr:
        try:
            _shutdown.link_mgr.disconnect_all()
        except Exception:
            pass

    # Stop any in-progress recording
    if _shutdown.recorder and _shutdown.recorder.is_active:
        try:
            _shutdown.recorder.discard()
        except Exception:
            pass

    # Terminate pipeline processes
    if _shutdown.pipeline:
        try:
            _shutdown.pipeline.terminate()
        except Exception:
            pass

    sys.exit(0)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='EAS/SAME AllStarLink Monitor')
    parser.add_argument('--config',  default=DEFAULT_CONFIG)
    parser.add_argument('--dry-run', action='store_true',
                        help='Log actions without connecting nodes')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    config      = load_config(args.config)
    settings    = config['settings']
    source_type = settings.get('audio_source', '').lower()
    local_node  = settings.get('local_node', '')
    public_node = settings.get('public_node', local_node)
    max_link    = settings.getint('max_link_duration', 3600)
    dry_run     = args.dry_run or settings.getboolean('dry_run', fallback=False)

    log = setup_logging(settings.get('log_file', DEFAULT_LOG), args.verbose)

    log.info("=" * 60)
    log.info("EAS/SAME AllStarLink Monitor starting")
    log.info(f"  Config       : {args.config}")
    log.info(f"  Audio source : {source_type}")
    log.info(f"  Local node   : {local_node}")
    log.info(f"  Public node  : {public_node}")
    log.info(f"  Dry run      : {dry_run}")
    log.info("=" * 60)

    _shutdown.log = log

    # Register signal handlers before building components
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP,  _handle_signal)

    # ── Build components ──────────────────────────────────────────────────

    ami = AsteriskAMI(
        host    = config.get('ami', 'host',   fallback='127.0.0.1'),
        port    = config.getint('ami', 'port', fallback=5038),
        user    = config.get('ami', 'user',   fallback='eas_monitor'),
        secret  = config.get('ami', 'pass',   fallback=''),
        dry_run = dry_run,
        log     = log
    )

    if not ami.test_connection() and not dry_run:
        log.error(
            "Cannot connect to Asterisk AMI. "
            "Verify Asterisk is running and manager.conf has the "
            "[eas_monitor] user with correct credentials."
        )
        sys.exit(1)

    log.info("AMI connection OK")

    # Run pre-flight checks (warnings only — nothing here kills startup)
    check_dependencies(source_type, ami, log)

    usrp_sinks   = build_usrp_sinks(config, log)
    primary_usrp = next(iter(usrp_sinks.values())) if usrp_sinks else None

    # Register USRP sinks for signal handler cleanup
    _shutdown.usrp_sinks = list(usrp_sinks.values())

    # SAFETY: release PTT now in case a previous crash left it keyed.
    # This runs before any alert processing begins.
    if primary_usrp:
        try:
            primary_usrp.key_down()
            log.info("PTT released (startup safety check)")
        except Exception as e:
            log.warning(f"Could not release PTT at startup: {e}")

    recorder = None
    if config.getboolean('recording', 'enabled', fallback=True):
        if CONVERSION_AVAILABLE:
            recorder = AlertRecorder(
                directory      = config.get(
                    'recording', 'directory',
                    fallback=AlertRecorder.DEFAULT_DIR),
                max_recordings = config.getint(
                    'recording', 'max_recordings',
                    fallback=AlertRecorder.DEFAULT_MAX),
                log = log
            )
        else:
            log.warning(
                "Recording enabled in config but no conversion library "
                "available — recording will be silently skipped. "
                "Install numpy to enable recording."
            )
    _shutdown.recorder = recorder

    link_mgr = LinkManager(
        ami          = ami,
        local_node   = local_node,
        public_node  = public_node,
        max_link_sec = max_link,
        log          = log
    )
    _shutdown.link_mgr = link_mgr

    handler = AlertHandler(
        config    = config,
        link_mgr  = link_mgr,
        recorder  = recorder,
        usrp_sink = primary_usrp,
        log       = log
    )

    # ── Pipeline restart loop ─────────────────────────────────────────────

    source = get_source(config)
    log.info(f"Source: {source.describe()}")

    consecutive_fast_failures = 0

    while True:
        audio_proc = None
        pipeline   = None
        start_time = time.time()

        try:
            if source_type == 'rtlsdr' and source.is_wideband:
                pipeline = source.get_wideband_pipeline(
                    usrp_sinks = usrp_sinks,
                    recorder   = recorder,
                    log        = log
                )
                _shutdown.pipeline = pipeline
                log.info("Wideband pipeline running")
                pipeline.run(handler)

            else:
                audio_proc = source.get_process()
                pipeline   = SimplePipeline(
                    source_proc = audio_proc,
                    usrp_sink   = primary_usrp,
                    recorder    = recorder,
                    log         = log
                )
                _shutdown.pipeline = pipeline
                log.info("Pipeline running — listening for SAME headers")
                pipeline.run(handler)

            log.warning("Pipeline exited cleanly — restarting")
            consecutive_fast_failures = 0

        except KeyboardInterrupt:
            log.info("Keyboard interrupt — shutting down")
            _handle_signal(signal.SIGTERM, None)

        except RuntimeError as e:
            # Configuration-level errors (missing binary, bad config)
            log.error(f"Configuration error: {e}")
            log.error("Waiting 60s before retry (fix the problem above)")
            _cleanup_after_failure(link_mgr, primary_usrp, recorder, log)
            time.sleep(60)
            continue

        except Exception as e:
            log.error(f"Pipeline error: {e}")

        # Detect tight restart loops (source fails immediately every time)
        run_duration = time.time() - start_time
        if run_duration < 5:
            consecutive_fast_failures += 1
            if consecutive_fast_failures >= 3:
                log.error(
                    f"Pipeline has failed {consecutive_fast_failures} times "
                    f"in rapid succession — backing off 60s. "
                    f"Check hardware and logs."
                )
                _cleanup_after_failure(link_mgr, primary_usrp, recorder, log)
                time.sleep(60)
                consecutive_fast_failures = 0
                continue
        else:
            consecutive_fast_failures = 0

        _cleanup_after_failure(link_mgr, primary_usrp, recorder, log)
        log.info(f"Restarting pipeline in {RESTART_DELAY}s")
        time.sleep(RESTART_DELAY)


def _cleanup_after_failure(link_mgr, primary_usrp, recorder, log):
    """
    Best-effort cleanup after any pipeline failure.
    Runs before every restart. Must not raise.
    """
    try:
        link_mgr.disconnect_all()
    except Exception as e:
        log.debug(f"disconnect_all on restart: {e}")

    if primary_usrp:
        try:
            primary_usrp.key_down()
        except Exception as e:
            log.debug(f"key_down on restart: {e}")

    if recorder and recorder.is_active:
        try:
            recorder.discard()
        except Exception as e:
            log.debug(f"recorder discard on restart: {e}")


if __name__ == '__main__':
    main()
