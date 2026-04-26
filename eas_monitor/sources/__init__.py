"""
sources/__init__.py - Audio Source Factory
============================================
Returns the appropriate source object based on config.

Source types:
  usb_shared  - Existing ASL node hardware (RIM-Lite etc.) via ALSA dsnoop
  usb_direct  - Dedicated weather radio + USB dongle, owned directly
  rtlsdr      - RTL-SDR dongle, single or multi-frequency
  stream      - Internet Icecast/HTTP stream (Broadcastify or free)
"""

import configparser


def get_source(config: configparser.ConfigParser):
    """
    Factory function. Returns a source object appropriate for the config.

    For rtlsdr with multiple frequencies, returns a WidebandSource
    that manages its own pipeline internally.

    All other sources return a SimpleSource whose get_process() method
    returns a subprocess.Popen with raw 22050Hz PCM on stdout.
    """
    source_type = config.get('settings', 'audio_source', fallback='').lower()

    if source_type == 'usb_shared':
        from .usb_shared import USBSharedSource
        return USBSharedSource(config)

    elif source_type == 'usb_direct':
        from .usb_direct import USBDirectSource
        return USBDirectSource(config)

    elif source_type == 'rtlsdr':
        from .rtlsdr import RTLSDRSource
        return RTLSDRSource(config)

    elif source_type == 'stream':
        from .stream import StreamSource
        return StreamSource(config)

    else:
        raise ValueError(
            f"Unknown audio_source: '{source_type}'. "
            f"Valid options: usb_shared, usb_direct, rtlsdr, stream"
        )
