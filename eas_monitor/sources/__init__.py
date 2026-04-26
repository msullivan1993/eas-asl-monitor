"""
sources/__init__.py - Audio Source Factory
Python 3.5 compatible.
"""
import configparser


def get_source(config):
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
            "Unknown audio_source: '%s'. "
            "Valid options: usb_shared, usb_direct, rtlsdr, stream"
            % source_type
        )
