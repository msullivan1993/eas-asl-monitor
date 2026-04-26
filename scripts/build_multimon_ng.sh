#!/bin/bash
# =============================================================================
#  build_multimon_ng.sh
#  Builds multimon-ng from source on HamVoIP/Arch Linux.
#
#  HamVoIP ships GCC 5.3 which is too old to build current multimon-ng.
#  We upgrade GCC first, then build. If the upgrade doesn't help we fall
#  back to a compat header workaround so the build always succeeds.
# =============================================================================
set -e

REPO_URL="https://github.com/EliasOenal/multimon-ng.git"
BUILD_DIR="/tmp/multimon-ng-build"
PIN_TAG="1.2.0"
COMPAT_HEADER="/tmp/uint128_compat.h"

echo ""
echo "  Building multimon-ng ${PIN_TAG} from source..."
echo ""

# ── Step 1: Ensure all build dependencies are present and up to date ─────────
echo "  Updating package databases..."
pacman -Sy 2>/dev/null || {
    echo "  [ERROR] pacman -Sy failed — check network connection"
    exit 1
}

echo "  Installing/upgrading build dependencies..."
# Explicitly upgrade gcc rather than --needed (which skips existing installs).
# HamVoIP ships GCC 5.3; Arch ARM repos have newer versions that fix the
# __uint128_t issue in ARM kernel headers.
pacman -S --noconfirm gcc make cmake git 2>/dev/null || {
    echo "  [ERROR] dependency install failed"
    exit 1
}

GCC_VER=$(gcc --version | head -1)
CMAKE_VER=$(cmake --version | head -1)
echo "  GCC:   ${GCC_VER}"
echo "  CMake: ${CMAKE_VER}"

# ── Step 2: Test whether __uint128_t is available ────────────────────────────
echo "  Checking __uint128_t support..."
NEEDS_COMPAT=false
if ! echo 'typedef unsigned __int128 __uint128_t; int main(){return 0;}' \
     | gcc -x c - -o /dev/null 2>/dev/null; then
    echo "  __uint128_t not available in this GCC — will use compat header"
    NEEDS_COMPAT=true
else
    echo "  __uint128_t OK"
fi

# ── Step 3: Write compat header if needed ────────────────────────────────────
EXTRA_CFLAGS=""
if [[ "$NEEDS_COMPAT" == "true" ]]; then
    cat > "${COMPAT_HEADER}" << 'HEOF'
#ifndef __UINT128_COMPAT_H
#define __UINT128_COMPAT_H
/* Compat shim for GCC versions that don't expose __uint128_t in user-space.
 * Defines it as a 16-byte struct matching the ARM NEON register size so
 * asm/sigcontext.h compiles correctly without needing real 128-bit support. */
#ifndef __uint128_t
typedef struct { unsigned long long lo, hi; } __uint128_t;
#endif
#endif
HEOF
    EXTRA_CFLAGS="-include ${COMPAT_HEADER}"
    echo "  Compat header written: ${COMPAT_HEADER}"
fi

# ── Step 4: Clone ─────────────────────────────────────────────────────────────
rm -rf "${BUILD_DIR}"
echo "  Cloning repository (tag ${PIN_TAG})..."
git clone --branch "${PIN_TAG}" --depth 1 "${REPO_URL}" "${BUILD_DIR}" 2>&1 \
    || { echo "  [ERROR] git clone failed — check network connection"; exit 1; }
cd "${BUILD_DIR}"

# ── Step 5: Patch CMake minimum version ──────────────────────────────────────
echo "  Patching CMakeLists.txt..."
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
print('  ' + patched.splitlines()[0])
"

# ── Step 6: Build ─────────────────────────────────────────────────────────────
mkdir build && cd build

echo "  Configuring..."
cmake .. -DCMAKE_BUILD_TYPE=Release \
    ${EXTRA_CFLAGS:+-DCMAKE_C_FLAGS="${EXTRA_CFLAGS}"} \
    || { echo "  [ERROR] cmake configure failed"; exit 1; }

echo "  Building ($(nproc) cores)..."
make -j"$(nproc)" \
    || { echo "  [ERROR] make failed — see errors above"; exit 1; }

# ── Step 7: Install ───────────────────────────────────────────────────────────
echo "  Installing..."
make install || { echo "  [ERROR] make install failed"; exit 1; }

rm -f "${COMPAT_HEADER}"
rm -rf "${BUILD_DIR}"

if command -v multimon-ng &>/dev/null; then
    echo ""
    echo "  ✓ multimon-ng installed: $(multimon-ng -h 2>&1 | head -1)"
else
    echo "  [ERROR] Installation failed — binary not found after make install"
    exit 1
fi