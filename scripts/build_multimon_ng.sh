#!/bin/bash
# =============================================================================
#  build_multimon_ng.sh
#  Builds multimon-ng from source on HamVoIP/Arch Linux.
#
#  Known issue on HamVoIP: GCC 5.3 + newer kernel headers causes a
#  __uint128_t compile error in asm/sigcontext.h. We work around it by
#  defining the type as a dummy that satisfies the struct layout.
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

echo "  Patching CMakeLists.txt minimum version..."
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

mkdir build && cd build

echo "  Configuring..."
# First attempt — plain build
if cmake .. -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -3; then
    :
else
    echo "  [WARN] cmake configure failed — unexpected"
    exit 1
fi

echo "  Building ($(nproc) cores)..."
# First build attempt
if make -j"$(nproc)" 2>&1; then
    echo "  Build succeeded."
else
    echo ""
    echo "  Build failed — likely GCC 5.3 + kernel header __uint128_t mismatch."
    echo "  Retrying with workaround compiler flag..."
    echo ""

    # Clean and retry with the workaround:
    # Define __uint128_t as a 16-byte aligned char array — satisfies the
    # struct layout in asm/sigcontext.h without requiring actual 128-bit
    # integer support. multimon-ng never uses this type itself.
    cd ..
    rm -rf build && mkdir build && cd build
    cmake .. \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_FLAGS="-D'__uint128_t=__attribute__((aligned(16))) unsigned char[16]'" \
        2>&1 | tail -3

    make -j"$(nproc)" 2>&1 || {
        echo ""
        echo "  [ERROR] Both build attempts failed."
        echo "  Try updating GCC: pacman -Sy gcc"
        echo "  Then re-run: sudo bash scripts/build_multimon_ng.sh"
        exit 1
    }
    echo "  Build succeeded with workaround."
fi

echo "  Installing..."
make install

if command -v multimon-ng &>/dev/null; then
    echo ""
    echo "  ✓ multimon-ng installed: $(multimon-ng --version 2>&1 | head -1)"
    rm -rf "${BUILD_DIR}"
else
    echo "  [ERROR] make install ran but binary not found"
    exit 1
fi