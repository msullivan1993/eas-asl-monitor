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

echo "  CMake version: $(cmake --version | head -1)"

# Always clean clone — avoids stale state from previous failed builds
rm -rf "${BUILD_DIR}"
echo "  Cloning repository..."
git clone "${REPO_URL}" "${BUILD_DIR}"
cd "${BUILD_DIR}"

# Rewrite line 1 of CMakeLists.txt entirely — replaces whatever version
# declaration is there (including range syntax like 3.15...3.30) with
# a plain 3.5 minimum that matches what HamVoIP ships.
echo "  Patching CMakeLists.txt..."
echo "  Before: $(head -1 CMakeLists.txt)"
# Use Python to rewrite the line — avoids any sed escaping/in-place issues
python3 -c "
import re, sys
with open('CMakeLists.txt', 'r') as f:
    content = f.read()
patched = re.sub(
    r'cmake_minimum_required\s*\([^)]*\)',
    'cmake_minimum_required(VERSION 3.5)',
    content,
    count=1
)
with open('CMakeLists.txt', 'w') as f:
    f.write(patched)
print('  After: ' + patched.splitlines()[0])
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