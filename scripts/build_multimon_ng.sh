#!/bin/bash
# =============================================================================
#  build_multimon_ng.sh
#  Builds multimon-ng from source on HamVoIP/Arch Linux.
#
#  Pins to tag 1.2.0 — the current HEAD requires GCC 6+ (__uint128_t in ARM
#  kernel headers) which is newer than HamVoIP's GCC 5.3.0 toolchain.
# =============================================================================
set -e

REPO_URL="https://github.com/EliasOenal/multimon-ng.git"
BUILD_DIR="/tmp/multimon-ng-build"
PIN_TAG="1.2.0"

echo ""
echo "  Building multimon-ng ${PIN_TAG} from source..."
echo ""

echo "  Installing build dependencies..."
pacman -Sy --noconfirm --needed git cmake make gcc 2>/dev/null || {
    echo "  [ERROR] pacman failed — check your network connection"
    exit 1
}

echo "  CMake version: $(cmake --version | head -1)"
echo "  GCC version:   $(gcc --version | head -1)"

rm -rf "${BUILD_DIR}"
echo "  Cloning repository (tag ${PIN_TAG})..."
git clone --branch "${PIN_TAG}" --depth 1 "${REPO_URL}" "${BUILD_DIR}"
cd "${BUILD_DIR}"

echo "  Patching CMakeLists.txt..."
echo "  Before: $(head -1 CMakeLists.txt)"
python3 -c "
import re
with open('CMakeLists.txt', 'r') as f:
    content = f.read()
patched = re.sub(
    r'cmake_minimum_required\s*\([^)]*\)',
    'cmake_minimum_required(VERSION 3.5)',
    content, count=1
)
with open('CMakeLists.txt', 'w') as f:
    f.write(patched)
print('  After:  ' + patched.splitlines()[0])
"

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