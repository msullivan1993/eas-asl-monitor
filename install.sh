#!/bin/bash
# =============================================================================
#  install.sh — EAS/SAME AllStarLink Monitor Installer
#
#  Supports HamVoIP (Arch Linux ARM) and AllStarLink 3 (Debian/Raspberry Pi OS)
#
#  Usage:
#    sudo ./install.sh           # Fresh install — runs setup wizard
#    sudo ./install.sh --update  # Update files only, skip wizard
#    sudo ./install.sh --no-wizard # Install files without running wizard
# =============================================================================
set -e

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*"; }
error()   { echo -e "  ${RED}✗${NC}  $*"; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}── $* ──${NC}"; }

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/etc/eas_monitor"
DATA_DIR="/var/lib/eas_monitor"
BIN_LINK="/usr/local/bin/eas_monitor"
SERVICE_SRC="${REPO_DIR}/systemd/eas-monitor.service"
SERVICE_DEST="/etc/systemd/system/eas-monitor.service"

UPDATE_MODE=false
NO_WIZARD=false
for arg in "$@"; do
    [[ "$arg" == "--update"    ]] && UPDATE_MODE=true
    [[ "$arg" == "--no-wizard" ]] && NO_WIZARD=true
done

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo ./install.sh"

# ── Repo check ────────────────────────────────────────────────────────────────
[[ -f "${REPO_DIR}/eas_monitor/eas_monitor.py" ]] || \
    error "Run from the eas-asl-monitor repository root"

echo ""
echo -e "${BOLD}  EAS/SAME AllStarLink Monitor — Installer${NC}"
echo "  =========================================="
[[ "$UPDATE_MODE" == true ]] && echo "  Mode: UPDATE"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
section "Step 1: Detect System"

detect_distro() {
    if grep -qi "hamvoip\|arch" /etc/os-release 2>/dev/null; then
        echo "arch"
    elif grep -qi "debian\|ubuntu\|raspbian" /etc/os-release 2>/dev/null; then
        echo "debian"
    else
        echo "unknown"
    fi
}

DISTRO=$(detect_distro)
info "Distro: ${DISTRO}"

if command -v asterisk &>/dev/null; then
    ASL_VER=$(asterisk -V 2>/dev/null | head -1 || echo "unknown")
    info "Asterisk: ${ASL_VER}"
else
    warn "Asterisk not found — install AllStarLink before using this monitor"
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Step 2: Install Dependencies"

install_packages() {
    case "$DISTRO" in
        arch)
            info "Using pacman"
            pacman -Sy --noconfirm --needed python3 alsa-utils sox ffmpeg 2>/dev/null \
                && info "Base packages installed" \
                || warn "Some packages may have failed — check manually"

            # libnewt provides whiptail on Arch
            pacman -Sy --noconfirm --needed libnewt 2>/dev/null \
                && info "whiptail (libnewt) installed" \
                || warn "libnewt install failed"

            # multimon-ng not in pacman — build from source
            if ! command -v multimon-ng &>/dev/null; then
                warn "multimon-ng not in pacman repos — building from source..."
                bash "${REPO_DIR}/scripts/build_multimon_ng.sh"
            else
                info "multimon-ng already installed"
            fi

            # numpy for wideband RTL-SDR (optional)
            if ! python3 -c "import numpy" 2>/dev/null; then
                pacman -Sy --noconfirm --needed python-numpy 2>/dev/null && \
                    info "numpy installed" || \
                    warn "numpy not installed — wideband RTL-SDR unavailable"
            fi
            ;;

        debian)
            info "Using apt"
            apt-get update -qq
            apt-get install -y python3 multimon-ng alsa-utils sox ffmpeg \
                whiptail python3-numpy 2>/dev/null \
                && info "Dependencies installed" \
                || warn "Some packages may have failed"
            ;;

        *)
            warn "Unknown distro — attempting apt then pacman"
            if command -v apt-get &>/dev/null; then
                apt-get update -qq
                apt-get install -y python3 multimon-ng alsa-utils sox ffmpeg \
                    whiptail 2>/dev/null || true
            elif command -v pacman &>/dev/null; then
                pacman -Sy --noconfirm --needed python3 alsa-utils sox \
                    ffmpeg libnewt 2>/dev/null || true
                command -v multimon-ng &>/dev/null || \
                    bash "${REPO_DIR}/scripts/build_multimon_ng.sh"
            fi
            ;;
    esac
}

install_packages

# Verify critical dependencies
for cmd in python3 multimon-ng whiptail; do
    command -v "$cmd" &>/dev/null && info "$cmd: OK" || \
        warn "$cmd not found — some features may not work"
done

# ─────────────────────────────────────────────────────────────────────────────
section "Step 3: Install Files"

# ── RTL-SDR DVB driver blacklist ──────────────────────────────────────────
# The DVB kernel driver auto-claims RTL-SDR devices and conflicts with rtl_fm.
# Blacklisting it here prevents the conflict after next reboot.
if [[ "$DISTRO" == "arch" ]] || [[ "$DISTRO" == "debian" ]]; then
    BLACKLIST="/etc/modprobe.d/rtlsdr-blacklist.conf"
    if [[ ! -f "$BLACKLIST" ]]; then
        cat > "$BLACKLIST" << BLEOF
# RTL-SDR — prevents DVB driver from claiming the device before rtl_fm can
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
BLEOF
        info "DVB driver blacklist written: $BLACKLIST"
        # Unload now if loaded (no reboot needed)
        modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
        modprobe -r rtl2832 2>/dev/null || true
    else
        info "DVB blacklist already present"
    fi
fi

mkdir -p "${INSTALL_DIR}/sources" "${INSTALL_DIR}/config" \
         "${INSTALL_DIR}/test" "${DATA_DIR}/recordings"
info "Directories created"

# Copy source files
cp -r "${REPO_DIR}/eas_monitor/"* "${INSTALL_DIR}/"
cp "${REPO_DIR}/setup_wizard.py" "${INSTALL_DIR}/"
info "Source files installed to ${INSTALL_DIR}"

# Symlink main script to PATH
ln -sf "${INSTALL_DIR}/eas_monitor.py" "${BIN_LINK}"
chmod +x "${INSTALL_DIR}/eas_monitor.py" "${INSTALL_DIR}/setup_wizard.py"
info "Symlinks created"

# Copy example config if no config exists
if [[ ! -f "/etc/eas_monitor/fips_nodes.conf" ]]; then
    cp "${REPO_DIR}/eas_monitor/config/fips_nodes.conf.example" \
       "${INSTALL_DIR}/fips_nodes.conf.example"
    info "Config example installed"
else
    info "Existing config preserved"
fi

# Set ownership
chown -R asterisk:asterisk "${INSTALL_DIR}" "${DATA_DIR}" 2>/dev/null || \
    warn "Could not set ownership to asterisk — may need manual fix"

# Log file
touch /var/log/eas_monitor.log 2>/dev/null || true
chown asterisk:asterisk /var/log/eas_monitor.log 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
section "Step 4: Download FIPS Reference Data"

FIPS_FILE="${DATA_DIR}/zcta_county.txt"
FIPS_URL="https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"

if [[ ! -f "$FIPS_FILE" ]] || [[ "$UPDATE_MODE" == true ]]; then
    echo "  Downloading FIPS ZIP-to-county reference data..."
    if command -v wget &>/dev/null; then
        wget -q "$FIPS_URL" -O "$FIPS_FILE" 2>/dev/null && \
            info "FIPS data downloaded ($(du -sh "$FIPS_FILE" | cut -f1))" || \
            warn "FIPS download failed — wizard will use live Census API"
    elif command -v curl &>/dev/null; then
        curl -sL "$FIPS_URL" -o "$FIPS_FILE" 2>/dev/null && \
            info "FIPS data downloaded" || \
            warn "FIPS download failed — wizard will use live Census API"
    else
        warn "Neither wget nor curl found — FIPS download skipped"
    fi
else
    info "FIPS data already present"
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Step 5: Download Test Sample"

TEST_SAMPLE="${INSTALL_DIR}/test/same_test.wav"
TEST_URL="https://www.weather.gov/media/arx/sametest.wav"

if [[ ! -f "$TEST_SAMPLE" ]]; then
    echo "  Downloading SAME test audio sample..."
    if command -v wget &>/dev/null; then
        wget -q "$TEST_URL" -O "$TEST_SAMPLE" 2>/dev/null && \
            info "Test sample downloaded" || \
            warn "Test sample download failed — decode test will be skipped"
    elif command -v curl &>/dev/null; then
        curl -sL "$TEST_URL" -o "$TEST_SAMPLE" 2>/dev/null && \
            info "Test sample downloaded" || \
            warn "Test sample download failed"
    fi
else
    info "Test sample already present"
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Step 6: Install systemd Service"

cp "$SERVICE_SRC" "$SERVICE_DEST"
systemctl daemon-reload
info "Service file installed: ${SERVICE_DEST}"

if [[ "$UPDATE_MODE" == true ]]; then
    systemctl restart eas-monitor 2>/dev/null && \
        info "Service restarted" || \
        warn "Service restart failed — start manually"
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Step 7: AMI User Setup"

AMI_CONF="/etc/asterisk/manager.conf"
if [[ -f "$AMI_CONF" ]]; then
    if ! grep -q "^\[eas_monitor\]" "$AMI_CONF"; then
        echo ""
        echo "  Adding [eas_monitor] AMI user to ${AMI_CONF}..."
        echo ""
        echo -n "  Enter AMI password for eas_monitor user: "
        read -r AMI_PASS
        if [[ -n "$AMI_PASS" ]]; then
            cat >> "$AMI_CONF" << AMIEOF

[eas_monitor]
secret = ${AMI_PASS}
read = command,system
write = command,system
permit = 127.0.0.1/255.255.255.0
AMIEOF
            info "AMI user added"
            asterisk -rx "module reload manager" 2>/dev/null && \
                info "Asterisk manager reloaded" || true
        fi
    else
        info "AMI user [eas_monitor] already configured"
    fi
else
    warn "${AMI_CONF} not found — configure AMI manually"
fi

# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}  ══════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Installation complete!${NC}"
echo -e "${BOLD}  ══════════════════════════════════════════════${NC}"
echo ""

if [[ "$UPDATE_MODE" == true ]] || [[ "$NO_WIZARD" == true ]]; then
    echo "  Files updated. Restart service to apply:"
    echo "    systemctl restart eas-monitor"
    echo ""
else
    echo "  Now running the setup wizard..."
    echo "  (Run 'sudo python3 ${INSTALL_DIR}/setup_wizard.py' at any time)"
    echo ""
    sleep 2
    python3 "${INSTALL_DIR}/setup_wizard.py"
fi
