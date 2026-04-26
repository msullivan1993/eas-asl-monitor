"""
ami.py - Asterisk Manager Interface
====================================
Hardened AMI client with retry on transient failures (Asterisk reload,
brief unavailability). Asterisk takes 2-10s to reload — without retry,
an alert during a reload window silently connects no nodes.

Retry policy:
  - 3 attempts, 2s apart
  - Only retries on connection failure; a successful login with a bad
    command response is logged but not retried (command may have run)
  - dry_run mode logs all commands without touching Asterisk
"""

import logging
import socket
import time

# How many times to attempt an AMI command before giving up
AMI_RETRIES    = 3
AMI_RETRY_WAIT = 2.0   # seconds between retries


class AsteriskAMI:
    """
    Lightweight AMI client. Opens a new connection per command to avoid
    session state issues on long-running processes.
    """

    def __init__(self, host='127.0.0.1', port=5038,
                 user='eas_monitor', secret='',
                 dry_run=False, log=None):
        self.host    = host
        self.port    = port
        self.user    = user
        self.secret  = secret
        self.dry_run = dry_run
        self.log     = log or logging.getLogger('eas_monitor.ami')

    # ── Low-level ──────────────────────────────────────────────────────────

    def _connect(self):
        """Open and authenticate an AMI socket. Returns socket or None."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((self.host, self.port))
            s.recv(1024)   # consume banner

            login = (
                f"Action: Login\r\n"
                f"Username: {self.user}\r\n"
                f"Secret: {self.secret}\r\n\r\n"
            )
            s.sendall(login.encode())
            resp = s.recv(1024).decode(errors='replace')

            if 'Success' not in resp:
                self.log.warning(f"AMI login rejected: {resp[:80]}")
                s.close()
                return None
            return s

        except ConnectionRefusedError:
            self.log.warning(
                f"AMI connection refused — Asterisk may be reloading"
            )
        except socket.timeout:
            self.log.warning(
                f"AMI connection timed out ({self.host}:{self.port})"
            )
        except Exception as e:
            self.log.warning(f"AMI connect error: {e}")

        return None

    def _send(self, action: str) -> str:
        """
        Send an AMI action with automatic retry on transient failures.
        Returns the response string, or '' if all attempts failed.
        """
        if self.dry_run:
            self.log.info(f"[DRY RUN] AMI: {action.strip()[:80]}")
            return "Response: Success"

        for attempt in range(1, AMI_RETRIES + 1):
            s = self._connect()
            if s is None:
                if attempt < AMI_RETRIES:
                    self.log.warning(
                        f"AMI not available — retry {attempt}/{AMI_RETRIES} "
                        f"in {AMI_RETRY_WAIT}s"
                    )
                    time.sleep(AMI_RETRY_WAIT)
                else:
                    self.log.error(
                        f"AMI unavailable after {AMI_RETRIES} attempts. "
                        f"Alert will not connect nodes."
                    )
                continue

            try:
                s.sendall(action.encode())
                time.sleep(0.1)
                resp = s.recv(4096).decode(errors='replace')
                return resp

            except Exception as e:
                self.log.warning(
                    f"AMI send error (attempt {attempt}): {e}"
                )
                if attempt < AMI_RETRIES:
                    time.sleep(AMI_RETRY_WAIT)

            finally:
                try:
                    s.close()
                except Exception:
                    pass

        return ''

    # ── Public interface ───────────────────────────────────────────────────

    def rpt_cmd(self, node: str, command: str) -> bool:
        """Issue an rpt command to a local node via AMI."""
        action = (
            f"Action: Command\r\n"
            f"Command: rpt cmd {node} {command}\r\n\r\n"
        )
        resp = self._send(action)
        ok   = 'Response: Success' in resp or self.dry_run
        if not ok and resp:
            self.log.warning(
                f"rpt cmd {node} {command} may have failed: "
                f"{resp.strip()[:80]}"
            )
        elif not ok:
            self.log.warning(
                f"rpt cmd {node} {command} — no response (Asterisk down?)"
            )
        return ok

    def ilink_connect_transceive(self, local_node: str,
                                 remote_node: str) -> bool:
        """
        ilink 3 — connect local_node to remote_node in transceive mode.
        Audio propagates through remote_node's entire connected network.
        """
        self.log.info(
            f"AMI: ilink 3  {local_node} → {remote_node}  (transceive)"
        )
        return self.rpt_cmd(local_node, f"ilink 3 {remote_node}")

    def ilink_connect_local_monitor(self, listener_node: str,
                                    source_node: str) -> bool:
        """
        ilink 8 — listener_node monitors source_node locally.
        Audio NOT retransmitted to listener's RF or linked network.
        Both nodes must be on this Asterisk instance.
        """
        self.log.info(
            f"AMI: ilink 8  {listener_node} ← {source_node}  (local monitor)"
        )
        return self.rpt_cmd(listener_node, f"ilink 8 {source_node}")

    def ilink_disconnect(self, node_a: str, node_b: str) -> bool:
        """ilink 1 — disconnect a specific link between two nodes."""
        self.log.info(f"AMI: ilink 1  disconnect {node_a} ↔ {node_b}")
        return self.rpt_cmd(node_a, f"ilink 1 {node_b}")

    def localplay(self, node: str, filepath: str) -> bool:
        """Play a file on a local node (does not propagate)."""
        self.log.info(f"AMI: localplay {node}  {filepath}")
        return self.rpt_cmd(node, f"localplay {filepath}")

    def test_connection(self) -> bool:
        """Returns True if AMI login succeeds."""
        if self.dry_run:
            return True
        s = self._connect()
        if s:
            s.close()
            return True
        return False

    def is_module_loaded(self, module_name: str) -> bool:
        """
        Check whether an Asterisk module is currently loaded.
        Used at startup to verify chan_usrp.so is present.
        """
        if self.dry_run:
            return True
        action = (
            f"Action: Command\r\n"
            f"Command: module show like {module_name}\r\n\r\n"
        )
        resp = self._send(action)
        return module_name in resp
