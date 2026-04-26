#!/bin/bash
# =============================================================================
#  uninstall.sh — EAS/SAME AllStarLink Monitor Uninstaller
# =============================================================================
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
ask()  { echo -en "  ${BOLD}?${NC}  $1 "; }

[[ $EUID -ne 0 ]] && { echo "Run as root: sudo ./uninstall.sh"; exit 1; }

echo ""
echo -e "${BOLD}  EAS/SAME AllStarLink Monitor — Uninstaller${NC}"
echo ""

ask "Stop and remove the eas-monitor service and files? [y/N]: "
read -r CONFIRM
[[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]] && { echo "  Aborted."; exit 0; }

ask "Keep config at /etc/eas_monitor/fips_nodes.conf? [Y/n]: "
read -r KEEP_CONF

ask "Keep recordings at /var/lib/eas_monitor/recordings? [Y/n]: "
read -r KEEP_RECS

# Stop service
systemctl stop eas-monitor    2>/dev/null && info "Service stopped"    || true
systemctl disable eas-monitor 2>/dev/null && info "Service disabled"   || true
rm -f /etc/systemd/system/eas-monitor.service
systemctl daemon-reload
info "Service removed"

# Remove symlink
rm -f /usr/local/bin/eas_monitor
info "Removed /usr/local/bin/eas_monitor"

# Remove code files
if [[ "$KEEP_CONF" == "n" || "$KEEP_CONF" == "N" ]]; then
    rm -rf /etc/eas_monitor
    info "Removed /etc/eas_monitor"
else
    rm -f  /etc/eas_monitor/eas_monitor.py
    rm -f  /etc/eas_monitor/setup_wizard.py
    rm -rf /etc/eas_monitor/sources
    info "Removed code files — config preserved"
fi

# Remove data
if [[ "$KEEP_RECS" == "n" || "$KEEP_RECS" == "N" ]]; then
    rm -rf /var/lib/eas_monitor
    info "Removed /var/lib/eas_monitor"
else
    rm -f  /var/lib/eas_monitor/zcta_county.txt
    info "Removed FIPS data — recordings preserved"
fi

rm -f /var/log/eas_monitor.log 2>/dev/null || true

echo ""
echo "  Uninstall complete."
echo ""
echo "  Note: ALSA config (/etc/asound.conf dsnoop stanza) and"
echo "  chan_usrp entries in modules.conf/rpt.conf were NOT removed."
echo "  Remove manually if no longer needed."
echo ""
