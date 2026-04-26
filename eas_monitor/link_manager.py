"""
link_manager.py - AllStarLink Node Link Manager
Python 3.5 compatible.
"""

import logging
import time


class LinkManager(object):

    def __init__(self, ami, local_node, public_node,
                 max_link_sec=3600, log=None):
        self.ami          = ami
        self.local_node   = local_node
        self.public_node  = public_node
        self.max_link_sec = max_link_sec
        self.log          = log or logging.getLogger('eas_monitor.links')
        self._active      = {}

    @property
    def active_nodes(self):
        return list(self._active.keys())

    @property
    def has_active_links(self):
        return bool(self._active)

    def connect(self, remote_node, purge_secs, mode='propagate', event=''):
        if mode == 'local':
            return self._connect_local(purge_secs, event)
        return self._connect_propagate(remote_node, purge_secs, event)

    def _connect_propagate(self, remote_node, purge_secs, event):
        if remote_node in self._active:
            new_expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
            if new_expiry > self._active[remote_node]['expiry']:
                self._active[remote_node]['expiry'] = new_expiry
                self.log.debug("Extended link to %s purge timer", remote_node)
            return True
        expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
        ok = self.ami.ilink_connect_transceive(self.public_node, remote_node)
        if ok:
            self._active[remote_node] = {
                'expiry': expiry,
                'mode':   'propagate',
                'event':  event
            }
            self.log.info(
                "Connected: %s -> %s [%s] propagate, purge %dmin",
                self.public_node, remote_node, event, purge_secs // 60
            )
        return ok

    def _connect_local(self, purge_secs, event):
        local_key = '__local__'
        if local_key in self._active:
            new_expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
            if new_expiry > self._active[local_key]['expiry']:
                self._active[local_key]['expiry'] = new_expiry
            return True
        expiry = time.time() + min(purge_secs + 30, self.max_link_sec)
        ok = self.ami.ilink_connect_local_monitor(
            self.public_node, self.local_node
        )
        if ok:
            self._active[local_key] = {
                'expiry': expiry,
                'mode':   'local',
                'event':  event
            }
            self.log.info(
                "Local monitor: %s <- %s [%s] local only, purge %dmin",
                self.public_node, self.local_node, event, purge_secs // 60
            )
        return ok

    def disconnect(self, remote_node):
        if remote_node not in self._active:
            return
        link = self._active.pop(remote_node)
        if link['mode'] == 'local':
            self.ami.ilink_disconnect(self.public_node, self.local_node)
            self.log.info("Disconnected local monitor: %s <- %s",
                          self.public_node, self.local_node)
        else:
            self.ami.ilink_disconnect(self.public_node, remote_node)
            self.log.info("Disconnected: %s <-> %s",
                          self.public_node, remote_node)

    def disconnect_all(self):
        if not self._active:
            return
        self.log.info("Disconnecting %d active link(s)", len(self._active))
        for node in list(self._active.keys()):
            self.disconnect(node)

    def check_timeouts(self):
        now = time.time()
        for node, info in list(self._active.items()):
            if now >= info['expiry']:
                self.log.warning(
                    "Node %s purge time elapsed — auto-disconnecting", node
                )
                self.disconnect(node)
