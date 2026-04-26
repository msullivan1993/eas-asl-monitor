#!/bin/bash
# =============================================================================
#  build_multimon_ng.sh
#  Builds multimon-ng from source on HamVoIP/Arch Linux.
# =============================================================================
set -e

REPO_URL="https://github.com/EliasOenal/multimon-ng.git"
BUILD_DIR="/tmp/multimon-ng-build"

echo ""
echo "  Building multimon-ng from source..."
echo ""

echo "  Installing build dependencies..."
pacman -Sy --noconfirm --needed git cmake make gcc 2>/dev/null || {
    echo "  [ERROR] pacman failed — check your network connection"
    exit 1
}

CMAKE_VER=$(cmake --version 2>/dev/null | head -1 | sed 's/[^0-9.]//g')
echo "  CMake version: ${CMAKE_VER}"

# Always clean clone — avoids stale state from previous failed builds
rm -rf "${BUILD_DIR}"
echo "  Cloning repository..."
git clone "${REPO_URL}" "${BUILD_DIR}"
cd "${BUILD_DIR}"

# Always patch cmake_minimum_required — multimon-ng declares 3.15 but the
# build itself is compatible with 3.5. We unconditionally set the minimum
# to whatever is actually installed rather than doing version arithmetic
# that may fail on systems without grep -P support.
echo "  Patching CMakeLists.txt cmake_minimum_required → ${CMAKE_VER}..."
sed -i "s/cmake_minimum_required(VERSION [^)]*)/cmake_minimum_required(VERSION ${CMAKE_VER})/" \
    CMakeLists.txt
echo "  $(grep cmake_minimum_required CMakeLists.txt | head -1 | xargs)"

echo "  Building ($(nproc) cores)..."
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"

echo "  Installing..."
make install

if command -v multimon-ng &>/dev/null; then
    echo "  ✓ multimon-ng installed: $(multimon-ng --version 2>&1 | head -1)"
    rm -rf "${BUILD_DIR}"
else
    echo "  [ERROR] Installation failed — binary not found after make install"
    exit 1
fi