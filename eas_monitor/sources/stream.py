"""
sources/stream.py - Internet Stream Source
Python 3.5 compatible.
"""
import subprocess
from urllib.parse import quote


class StreamSource(object):
    needs_usrp = True

    def __init__(self, config):
        self.sample_rate     = config.get('source_stream', 'sample_rate',
                                          fallback='22050')
        self.reconnect_delay = config.get('source_stream', 'reconnect_delay',
                                          fallback='30')
        self._url = self._resolve_url(config)

    def _resolve_url(self, config):
        section = 'source_stream'
        if config.has_option(section, 'broadcastify_feed_id'):
            feed_id  = config.get(section, 'broadcastify_feed_id').strip()
            username = config.get(section, 'broadcastify_username', fallback='')
            password = config.get(section, 'broadcastify_password', fallback='')
            if not feed_id:
                raise ValueError(
                    "source_stream: broadcastify_feed_id is required"
                )
            if not username or not password:
                raise ValueError(
                    "source_stream: broadcastify_username and "
                    "broadcastify_password are required for Broadcastify"
                )
            user = quote(username, safe='')
            pw   = quote(password, safe='')
            return "https://%s:%s@audio.broadcastify.com/%s.mp3" % (
                user, pw, feed_id
            )
        elif config.has_option(section, 'url'):
            return config.get(section, 'url').strip()
        else:
            raise ValueError(
                "source_stream: either 'url' or 'broadcastify_feed_id' "
                "must be configured"
            )

    def get_process(self):
        return subprocess.Popen(
            [
                'ffmpeg',
                '-reconnect',          '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', self.reconnect_delay,
                '-i', self._url,
                '-f',  's16le',
                '-ar', self.sample_rate,
                '-ac', '1',
                '-',
                '-loglevel', 'error',
                '-nostats',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

    def describe(self):
        return "Internet Stream @ %sHz" % self.sample_rate
