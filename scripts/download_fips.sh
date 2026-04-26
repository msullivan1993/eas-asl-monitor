#!/bin/bash
# Download FIPS ZIP-to-county reference data from Census Bureau
FIPS_DIR="/var/lib/eas_monitor"
FIPS_FILE="${FIPS_DIR}/zcta_county.txt"
FIPS_URL="https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"

mkdir -p "$FIPS_DIR"
echo "Downloading FIPS data..."
if command -v wget &>/dev/null; then
    wget -q --show-progress "$FIPS_URL" -O "$FIPS_FILE" \
        && echo "Done: $FIPS_FILE ($(du -sh "$FIPS_FILE" | cut -f1))" \
        || echo "ERROR: download failed"
elif command -v curl &>/dev/null; then
    curl -L "$FIPS_URL" -o "$FIPS_FILE" \
        && echo "Done: $FIPS_FILE" \
        || echo "ERROR: download failed"
else
    echo "ERROR: neither wget nor curl found"
    exit 1
fi
