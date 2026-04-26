"""
sources/stream.py - Internet Stream Source
==========================================
Audio source from an Icecast or HTTP audio stream.
Supports any format ffmpeg can decode: MP3, AAC, OGG, Opus, etc.

SAME/EAS tones (1562.5Hz / 2062.5Hz FSK) survive compressed audio
at any reasonable bitrate (16kbps+).

Stream sources have 10-60 second inherent latency — alerts will fire
that long after the actual broadcast. This is acceptable for most uses
but should be documented clearly to users.

Two authentication modes:
  Plain URL:      Direct Icecast stream (noaaweatherradio.org, weatherusa.net)
  Broadcastify:   HTTP Basic Auth using RadioReference.com premium credentials
                  URL format: https://USER:PASS@audio.broadcastify.com/FEEDID.mp3

Requires USRP private node for audio injection into Asterisk.

Config section: [source_stream]
  # Plain URL mode:
  url = http://your.stream/mount

  # Broadcastify premium mode (credentials stored separately, not in URL):
  broadcastify_feed_id = 34002
  broadcastify_username = your_rr_username
  broadcastify_password = your_rr_password

  # Common settings:
  sample_rate      = 22050
  reconnect_delay  = 30
"""

import configparser
import subprocess
from urllib.parse import quote


class StreamSource:
    """
    Internet stream audio source via ffmpeg.
    Requires USRP node for audio injection into Asterisk.
    """

    needs_usrp = True

    def __init__(self, config: configparser.ConfigParser):
        self.sample_rate     = config.get('source_stream', 'sample_rate',
                                          fallback='22050')
        self.reconnect_delay = config.get('source_stream', 'reconnect_delay',
                                          fallback='30')

        # Resolve stream URL — build at runtime, never stored with credentials
        self._url = self._resolve_url(config)

    def _resolve_url(self, config: configparser.ConfigParser) -> str:
        """
        Build the stream URL from config.
        Broadcastify credentials are embedded at runtime only — never logged.
        """
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
            return f"https://{user}:{pw}@audio.broadcastify.com/{feed_id}.mp3"

        elif config.has_option(section, 'url'):
            return config.get(section, 'url').strip()

        else:
            raise ValueError(
                "source_stream: either 'url' or 'broadcastify_feed_id' "
                "must be configured"
            )

    def get_process(self) -> subprocess.Popen:
        """
        Returns ffmpeg subprocess decoding the stream.
        stdout = raw signed 16-bit PCM mono at sample_rate Hz.

        The -reconnect flags ensure automatic reconnection if the stream
        drops — essential for 24/7 unattended operation.

        Credentials embedded in URL are never passed to log output
        because ffmpeg is run with -loglevel error and stderr is suppressed.
        """
        return subprocess.Popen(
            [
                'ffmpeg',
                # Reconnect on stream drop
                '-reconnect',          '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', self.reconnect_delay,
                # Input stream
                '-i', self._url,
                # Output: raw signed 16-bit PCM, mono, target sample rate
                '-f',  's16le',
                '-ar', self.sample_rate,
                '-ac', '1',
                '-',
                # Suppress ffmpeg's verbose output
                '-loglevel', 'error',
                '-nostats',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL  # Never log — URL contains credentials
        )

    def describe(self) -> str:
        """Safe description that never exposes credentials."""
        section = 'source_stream'
        # We can't access config here, so just show type
        return f"Internet Stream @ {self.sample_rate}Hz"

    @staticmethod
    def find_noaa_streams(state_abbr: str = '') -> str:
        """
        Returns a help URL for finding free NOAA WX streams.
        Used by the setup wizard.
        """
        if state_abbr:
            return (
                f"https://www.noaaweatherradio.org/"
                f"?q={state_abbr}"
            )
        return "https://www.noaaweatherradio.org/"

    @staticmethod
    def broadcastify_search_url() -> str:
        """URL to search Broadcastify for NOAA feeds."""
        return "https://www.broadcastify.com/search/?q=noaa+weather+radio"
