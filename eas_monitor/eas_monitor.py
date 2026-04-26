#!/usr/bin/env python3
"""
eas_monitor.py - EAS/SAME AllStarLink Monitor
Python 3.5 compatible.
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
RESTART_DELAY  = 15


def setup_logging(log_file, verbose=False):
    level   = logging.DEBUG if verbose else logging.INFO
    fmt     = '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except PermissionError:
        pass
    # force=True is Python 3.8+ -- remove existing handlers manually
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    logging.basicConfig(
        level=level, format=fmt, datefmt=datefmt,
        handlers=handlers
    )
    return logging.getLogger('eas_monitor')


def load_config(path):
    if not os.path.exists(path):
        print("[ERROR] Config not found: %s" % path)
        print("        Run: sudo python3 setup_wizard.py")
        sys.exit(1)
    config = configparser.ConfigParser()
    config.read(path)
    if 'settings' not in config:
        print("[ERROR] Config missing [settings] section: %s" % path)
        sys.exit(1)
    return config


def build_usrp_sinks(config, log):
    source_type = config.get('settings', 'audio_source', fallback='').lower()
    if source_type == 'usb_shared':
        return {}
    if 'usrp_nodes' not in config:
        log.warning("No [usrp_nodes] section -- USRP audio injection disabled")
        return {}
    sinks = {}
    for key, value in config['usrp_nodes'].items():
        try:
            tx, rx = (int(p.strip()) for p in value.split(':'))
            sinks[key] = USRPSink(tx_port=tx, rx_port=rx, log=log)
            log.debug("USRP sink: %s  TX:%d  RX:%d", key, tx, rx)
        except Exception as e:
            log.error("Invalid [usrp_nodes] entry '%s = %s': %s",
                      key, value, e)
    return sinks


def check_dependencies(source_type, ami, log):
    if not CONVERSION_AVAILABLE:
        log.error(
            "Audio recording disabled -- neither audioop nor numpy available. "
            "Install numpy: pip install numpy --break-system-packages"
        )
    else:
        log.info("Audio conversion: %s", CONVERSION_METHOD)

    if source_type not in ('usb_shared', 'stream'):
        if not ami.is_module_loaded('chan_usrp'):
            log.error(
                "chan_usrp.so is not loaded in Asterisk. "
                "Add 'load => chan_usrp.so' to /etc/asterisk/modules.conf "
                "and reload Asterisk."
            )
        else:
            log.info("chan_usrp.so: loaded OK")

    if source_type == 'rtlsdr':
        _check_dvb_conflict(log)


def _check_dvb_conflict(log):
    try:
        with open('/proc/modules', 'r') as f:
            modules = f.read()
        if 'dvb_usb_rtl28xxu' in modules or 'rtl2832' in modules:
            log.warning(
                "DVB kernel driver loaded -- may conflict with rtl_fm. "
                "If rtl_fm fails: sudo modprobe -r dvb_usb_rtl28xxu rtl2832"
            )
    except Exception:
        pass


class _ShutdownState(object):
    link_mgr   = None
    usrp_sinks = []
    recorder   = None
    pipeline   = None
    log        = None


_shutdown = _ShutdownState()


def _handle_signal(signum, frame):
    sig_names = {signal.SIGTERM: 'SIGTERM', signal.SIGHUP: 'SIGHUP'}
    sig_name  = sig_names.get(signum, str(signum))
    if _shutdown.log:
        _shutdown.log.info("Received %s -- shutting down cleanly", sig_name)

    for sink in _shutdown.usrp_sinks:
        try:
            sink.key_down()
        except Exception:
            pass

    if _shutdown.link_mgr:
        try:
            _shutdown.link_mgr.disconnect_all()
        except Exception:
            pass

    if _shutdown.recorder and _shutdown.recorder.is_active:
        try:
            _shutdown.recorder.discard()
        except Exception:
            pass

    if _shutdown.pipeline:
        try:
            _shutdown.pipeline.terminate()
        except Exception:
            pass

    sys.exit(0)


def _cleanup_after_failure(link_mgr, primary_usrp, recorder, log):
    try:
        link_mgr.disconnect_all()
    except Exception as e:
        log.debug("disconnect_all on restart: %s", e)
    if primary_usrp:
        try:
            primary_usrp.key_down()
        except Exception as e:
            log.debug("key_down on restart: %s", e)
    if recorder and recorder.is_active:
        try:
            recorder.discard()
        except Exception as e:
            log.debug("recorder discard on restart: %s", e)


def main():
    parser = argparse.ArgumentParser(
        description='EAS/SAME AllStarLink Monitor'
    )
    parser.add_argument('--config',  default=DEFAULT_CONFIG)
    parser.add_argument('--dry-run', action='store_true')
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
    log.info("  Config       : %s", args.config)
    log.info("  Audio source : %s", source_type)
    log.info("  Local node   : %s", local_node)
    log.info("  Public node  : %s", public_node)
    log.info("  Dry run      : %s", dry_run)
    log.info("=" * 60)

    _shutdown.log = log

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP,  _handle_signal)

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
            "Verify Asterisk is running and manager.conf has "
            "the [eas_monitor] user with correct credentials."
        )
        sys.exit(1)

    log.info("AMI connection OK")

    check_dependencies(source_type, ami, log)

    usrp_sinks   = build_usrp_sinks(config, log)
    primary_usrp = next(iter(usrp_sinks.values())) if usrp_sinks else None

    _shutdown.usrp_sinks = list(usrp_sinks.values())

    if primary_usrp:
        try:
            primary_usrp.key_down()
            log.info("PTT released (startup safety check)")
        except Exception as e:
            log.warning("Could not release PTT at startup: %s", e)

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
                "available -- recording will be skipped."
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

    source = get_source(config)
    log.info("Source: %s", source.describe())

    # For USRP node source: connect the radio node to the private
    # USRP listener node in local-monitor mode so audio flows through.
    # This is additive -- does not change any existing node connections.
    if source_type == "usrp_node" and not dry_run:
        private_node = config.get("source_usrp_node", "node", fallback="")
        if private_node and local_node:
            log.info(
                "Connecting radio node %s -> USRP listener %s (ilink 8)",
                local_node, private_node
            )
            # ilink_connect_local_monitor(listener, source)
            # = rpt cmd listener ilink 8 source
            # We want: rpt cmd radio_node(42266) ilink 8 private_node(1998)
            # so radio_node is the "listener" that connects to private_node
            ami.ilink_connect_local_monitor(local_node, private_node)

    consecutive_fast = 0

    while True:
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
                log.info("Pipeline running -- listening for SAME headers")
                pipeline.run(handler)

            log.warning("Pipeline exited cleanly -- restarting")
            consecutive_fast = 0

        except KeyboardInterrupt:
            log.info("Keyboard interrupt -- shutting down")
            _handle_signal(signal.SIGTERM, None)

        except RuntimeError as e:
            log.error("Configuration error: %s", e)
            log.error("Waiting 60s before retry")
            _cleanup_after_failure(link_mgr, primary_usrp, recorder, log)
            time.sleep(60)
            continue

        except Exception as e:
            log.error("Pipeline error: %s", e)

        run_duration = time.time() - start_time
        if run_duration < 5:
            consecutive_fast += 1
            if consecutive_fast >= 3:
                log.error(
                    "Pipeline failed %d times rapidly -- backing off 60s",
                    consecutive_fast
                )
                _cleanup_after_failure(link_mgr, primary_usrp, recorder, log)
                time.sleep(60)
                consecutive_fast = 0
                continue
        else:
            consecutive_fast = 0

        _cleanup_after_failure(link_mgr, primary_usrp, recorder, log)
        log.info("Restarting pipeline in %ds", RESTART_DELAY)
        time.sleep(RESTART_DELAY)


if __name__ == '__main__':
    main()
