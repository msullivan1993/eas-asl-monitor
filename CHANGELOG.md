# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

## [0.1.0] — Initial release

### Added
- Four audio sources: USB Shared (ALSA dsnoop), USB Direct, RTL-SDR, Internet Stream
- SAME/EAS header parsing with deduplication
- FIPS code → ASL node mapping with state wildcard support
- ilink 3 (transceive) and ilink 8 (local monitor) connection modes
- Alert audio recording to rotating ulaw buffer with DTMF playback
- USRP UDP audio injection with PTT control
- Wideband RTL-SDR mode (numpy FM demod, multi-frequency)
- Interactive whiptail setup wizard with live Census API ZIP lookup
- udev stable device naming for USB audio and RTL-SDR dongles
- rtl_eeprom serial write with replug detection
- HamVoIP (Arch Linux ARM) and AllStarLink 3 (Debian) installer
- systemd service with SIGTERM-safe shutdown (PTT release before exit)
- AMI retry logic for Asterisk reload windows
- select()-based audio source watchdog (detects hung/disconnected devices)
- DVB kernel driver conflict detection and blacklist
- Disk space pre-check before recording
- Audio conversion dependency validation at startup (audioop / numpy)
- Wideband processing lag warning for Pi 3B
- rtl_sdr USB buffer overflow detection via stderr monitoring
