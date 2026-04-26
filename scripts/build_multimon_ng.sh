#!/bin/bash
# =============================================================================
#  build_multimon_ng.sh
#  Builds multimon-ng from source on HamVoIP/Arch Linux.
#  Called automatically by install.sh when multimon-ng is not in pacman.
# =============================================================================
set -e

REPO_URL="https://github.com/EliasOenal/multimon-ng.git"
BUILD_DIR="/tmp/multimon-ng-build"

echo ""
echo "  Building multimon-ng from source..."
echo "  Source: ${REPO_URL}"
echo ""

echo "  Installing build dependencies..."
pacman -Sy --noconfirm --needed git cmake make gcc 2>/dev/null || {
    echo "  [ERROR] pacman failed"
    exit 1
}

if [[ -d "${BUILD_DIR}" ]]; then
    echo "  Updating existing source..."
    cd "${BUILD_DIR}" && git pull
else
    echo "  Cloning repository..."
    git clone "${REPO_URL}" "${BUILD_DIR}"
    cd "${BUILD_DIR}"
fi

echo "  Building ($(nproc) cores)..."
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"

echo "  Installing..."
make install

if command -v multimon-ng &>/dev/null; then
    echo "  ✓ multimon-ng installed: $(multimon-ng --version 2>&1 | head -1)"
    rm -rf "${BUILD_DIR}"
else
    echo "  [ERROR] Installation failed"
    exit 1
fi
