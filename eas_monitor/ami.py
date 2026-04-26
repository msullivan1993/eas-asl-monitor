"""
ami.py - Asterisk Manager Interface
====================================
Hardened AMI client with retry on transient failures.
Python 3.5 compatible — no f-strings, no X|Y type hints.
"""

import logging
import socket
import time

AMI_RETRIES    = 3
AMI_RETRY_WAIT = 2.0


class AsteriskAMI(object):

    def __init__(self, host='127.0.0.1', port=5038,
                 user='eas_monitor', secret='',
                 dry_run=False, log=None):
        self.host    = host
        self.port    = port
        self.user    = user
        self.secret  = secret
        self.dry_run = dry_run
        self.log     = log or logging.getLogger('eas_monitor.ami')

    def _connect(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((self.host, self.port))
            s.recv(1024)
            login = (
                "Action: Login\r\n"
                "Username: %s\r\n"
                "Secret: %s\r\n\r\n"
            ) % (self.user, self.secret)
            s.sendall(login.encode())
            resp = s.recv(1024).decode(errors='replace')
            if 'Success' not in resp:
                self.log.warning("AMI login rejected: %s", resp[:80])
                s.close()
                return None
            return s
        except ConnectionRefusedError:
            self.log.warning("AMI connection refused — Asterisk may be reloading")
        except socket.timeout:
            self.log.warning("AMI connection timed out (%s:%s)", self.host, self.port)
        except Exception as e:
            self.log.warning("AMI connect error: %s", e)
        return None

    def _send(self, action):
        if self.dry_run:
            self.log.info("[DRY RUN] AMI: %s", action.strip()[:80])
            return "Response: Success"
        for attempt in range(1, AMI_RETRIES + 1):
            s = self._connect()
            if s is None:
                if attempt < AMI_RETRIES:
                    self.log.warning(
                        "AMI not available — retry %d/%d in %ss",
                        attempt, AMI_RETRIES, AMI_RETRY_WAIT
                    )
                    time.sleep(AMI_RETRY_WAIT)
                else:
                    self.log.error(
                        "AMI unavailable after %d attempts. "
                        "Alert will not connect nodes.", AMI_RETRIES
                    )
                continue
            try:
                s.sendall(action.encode())
                time.sleep(0.1)
                resp = s.recv(4096).decode(errors='replace')
                return resp
            except Exception as e:
                self.log.warning("AMI send error (attempt %d): %s", attempt, e)
                if attempt < AMI_RETRIES:
                    time.sleep(AMI_RETRY_WAIT)
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        return ''

    def rpt_cmd(self, node, command):
        action = (
            "Action: Command\r\n"
            "Command: rpt cmd %s %s\r\n\r\n"
        ) % (node, command)
        resp = self._send(action)
        ok   = 'Response: Success' in resp or self.dry_run
        if not ok and resp:
            self.log.warning("rpt cmd %s %s may have failed: %s",
                             node, command, resp.strip()[:80])
        elif not ok:
            self.log.warning("rpt cmd %s %s — no response (Asterisk down?)",
                             node, command)
        return ok

    def ilink_connect_transceive(self, local_node, remote_node):
        self.log.info("AMI: ilink 3  %s -> %s  (transceive)",
                      local_node, remote_node)
        return self.rpt_cmd(local_node, "ilink 3 %s" % remote_node)

    def ilink_connect_local_monitor(self, listener_node, source_node):
        self.log.info("AMI: ilink 8  %s <- %s  (local monitor)",
                      listener_node, source_node)
        return self.rpt_cmd(listener_node, "ilink 8 %s" % source_node)

    def ilink_disconnect(self, node_a, node_b):
        self.log.info("AMI: ilink 1  disconnect %s <-> %s", node_a, node_b)
        return self.rpt_cmd(node_a, "ilink 1 %s" % node_b)

    def localplay(self, node, filepath):
        self.log.info("AMI: localplay %s  %s", node, filepath)
        return self.rpt_cmd(node, "localplay %s" % filepath)

    def test_connection(self):
        if self.dry_run:
            return True
        s = self._connect()
        if s:
            s.close()
            return True
        return False

    def is_module_loaded(self, module_name):
        if self.dry_run:
            return True
        action = (
            "Action: Command\r\n"
            "Command: module show like %s\r\n\r\n"
        ) % module_name
        resp = self._send(action)
        return module_name in resp
