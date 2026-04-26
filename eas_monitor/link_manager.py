"""
link_manager.py - AllStarLink Node Link Manager
=================================================
Tracks active node links and their purge times.
Handles both propagation modes:
  - transceive  : ilink 3 — audio propagates through connected network
  - local       : ilink 8 — audio heard on this node only

All nodes managed here are on the same local Asterisk instance.
"""

import logging
import time


class LinkManager:
    """
    Manages active AllStarLink node connections.

    Tracks (local_node, remote_node) pairs with their expiry times.
    Enforces:
      - No duplicate connections to the same remote node
      - Automatic disconnect on purge time expiry
      - Clean disconnect-all on EOM or process shutdown
    """

    def __init__(self, ami, local_node: str, public_node: str,
                 max_link_sec: int = 3600,
                 log: logging.Logger = None):
        """
        ami         : AsteriskAMI instance
        local_node  : The EAS/USRP source node (private, for SDR/stream sources)
                      OR same as public_node for USB Shared source
        public_node : The user's main public ASL node
        max_link_sec: Hard cap on link duration regardless of purge time
        """
        self.ami          = ami
        self.local_node   = local_node
        self.public_node  = public_node
        self.max_link_sec = max_link_sec
        self.log          = log or logging.getLogger('eas_monitor.links')

        # {remote_node: {'expiry': float, 'mode': str, 'event': str}}
        self._active: dict = {}

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def active_nodes(self) -> list:
        return list(self._active.keys())

    @property
    def has_active_links(self) -> bool:
        return bool(self._active)

    # ── Connection ─────────────────────────────────────────────────────────

    def connect(self, remote_node: str, purge_secs: int,
                mode: str = 'propagate', event: str = '') -> bool:
        """
        Connect to a remote node for an alert.

        mode='propagate' : public_node → remote_node via ilink 3 (transceive)
                           Audio reaches remote's entire connected network.

        mode='local'     : public_node monitors local_node via ilink 8
                           Audio heard only on public_node — not propagated.
                           remote_node is ignored in local mode.

        Returns True if connection was established.
        """
        if mode == 'local':
            return self._connect_local(purge_secs, event)
        else:
            return self._connect_propagate(remote_node, purge_secs, event)

    def _connect_propagate(self, remote_node: str, purge_secs: int,
                           event: str) -> bool:
        """ilink 3: public_node → remote_node (transceive)."""
        if remote_node in self._active:
            # Extend purge time if already connected
            new_expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
            if new_expiry > self._active[remote_node]['expiry']:
                self._active[remote_node]['expiry'] = new_expiry
                self.log.debug(
                    f"Extended link to {remote_node} purge timer"
                )
            return True

        expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
        ok     = self.ami.ilink_connect_transceive(
            self.public_node, remote_node
        )
        if ok:
            self._active[remote_node] = {
                'expiry': expiry,
                'mode':   'propagate',
                'event':  event
            }
            self.log.info(
                f"Connected: {self.public_node} → {remote_node} "
                f"[{event}] propagate, purge {purge_secs//60}min"
            )
        return ok

    def _connect_local(self, purge_secs: int, event: str) -> bool:
        """
        ilink 8: public_node monitors local_node (local only, no propagation).
        Uses '__local__' as the key since we're not connecting to a remote node.
        """
        local_key = '__local__'
        if local_key in self._active:
            new_expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
            if new_expiry > self._active[local_key]['expiry']:
                self._active[local_key]['expiry'] = new_expiry
            return True

        expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
        ok     = self.ami.ilink_connect_local_monitor(
            self.public_node, self.local_node
        )
        if ok:
            self._active[local_key] = {
                'expiry': expiry,
                'mode':   'local',
                'event':  event
            }
            self.log.info(
                f"Local monitor: {self.public_node} ← {self.local_node} "
                f"[{event}] local only, purge {purge_secs//60}min"
            )
        return ok

    # ── Disconnection ───────────────────────────────────────────────────────

    def disconnect(self, remote_node: str):
        """Disconnect a specific remote node."""
        if remote_node not in self._active:
            return

        link = self._active.pop(remote_node)

        if link['mode'] == 'local':
            self.ami.ilink_disconnect(self.public_node, self.local_node)
            self.log.info(
                f"Disconnected local monitor: "
                f"{self.public_node} ← {self.local_node}"
            )
        else:
            self.ami.ilink_disconnect(self.public_node, remote_node)
            self.log.info(
                f"Disconnected: {self.public_node} ↔ {remote_node}"
            )

    def disconnect_all(self):
        """Disconnect all active links. Called on EOM or shutdown."""
        if not self._active:
            return
        self.log.info(
            f"Disconnecting {len(self._active)} active link(s)"
        )
        for node in list(self._active.keys()):
            self.disconnect(node)

    # ── Timeout watchdog ────────────────────────────────────────────────────

    def check_timeouts(self):
        """
        Disconnect any nodes that have exceeded their purge time.
        Call periodically from the main loop.
        """
        now = time.time()
        for node, info in list(self._active.items()):
            if now >= info['expiry']:
                self.log.warning(
                    f"Node {node} purge time elapsed — auto-disconnecting"
                )
                self.disconnect(node)
