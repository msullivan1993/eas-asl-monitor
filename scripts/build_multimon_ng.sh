#!/bin/bash
# =============================================================================
#  build_multimon_ng.sh
#  Builds multimon-ng from source on HamVoIP/Arch Linux.
#  Called automatically by install.sh when multimon-ng is not in pacman.
#
#  Fix: multimon-ng CMakeLists.txt declares cmake_minimum_required 3.15 but
#  HamVoIP ships CMake 3.5. The build itself doesn't use any 3.15-specific
#  features — the declaration is just a guard. We patch it down after clone.
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
    echo "  [ERROR] pacman failed — check your network connection"
    exit 1
}

CMAKE_VER=$(cmake --version 2>/dev/null | head -1 | grep -oP '\d+\.\d+\.\d+' || echo "0.0.0")
echo "  CMake version: ${CMAKE_VER}"

if [[ -d "${BUILD_DIR}" ]]; then
    echo "  Updating existing source..."
    cd "${BUILD_DIR}" && git pull
else
    echo "  Cloning repository..."
    git clone "${REPO_URL}" "${BUILD_DIR}"
fi

cd "${BUILD_DIR}"

# Patch cmake_minimum_required if the installed CMake is older than what
# the repo declares. The build itself is compatible with CMake 3.5+.
DECLARED_MIN=$(grep -oP '(?<=cmake_minimum_required\(VERSION )\S+' CMakeLists.txt \
               | head -1 | cut -d. -f1-2 || echo "0.0")
CMAKE_MAJOR=$(echo "$CMAKE_VER" | cut -d. -f1)
CMAKE_MINOR=$(echo "$CMAKE_VER" | cut -d. -f2)
DECL_MAJOR=$(echo "$DECLARED_MIN" | cut -d. -f1)
DECL_MINOR=$(echo "$DECLARED_MIN" | cut -d. -f2 | cut -d. -f1)

if (( CMAKE_MAJOR < DECL_MAJOR )) || \
   (( CMAKE_MAJOR == DECL_MAJOR && CMAKE_MINOR < DECL_MINOR )); then
    echo "  Patching CMakeLists.txt: declared minimum ${DECLARED_MIN}" \
         "→ ${CMAKE_VER} (installed)"
    # Handles both plain (VERSION 3.15) and range syntax (VERSION 3.15...3.30)
    sed -i "s/cmake_minimum_required(VERSION [^)]*)/cmake_minimum_required(VERSION ${CMAKE_VER})/" \
        CMakeLists.txt
    echo "  Patch applied."
fi

echo "  Building ($(nproc) cores)..."
rm -rf build && mkdir build && cd build
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