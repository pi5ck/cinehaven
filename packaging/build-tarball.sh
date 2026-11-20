#!/bin/bash
# Builds dist/cinehaven-<ver>.tar.gz (used by install.sh and the self-updater) + SHA256SUMS
set -e
cd "$(dirname "$0")/.."
VER=$(python3 -c "import re;print(re.search(r'APP_VERSION = \"(.+?)\"', open('cinehaven/__init__.py').read())[1])")
mkdir -p dist
T=$(mktemp -d)/cinehaven-$VER
mkdir -p "$T"
cp -r cinehaven cinehaven-launcher install.sh README.md LICENSE data "$T/"
find "$T" -name __pycache__ -prune -exec rm -rf {} +
tar -C "$(dirname "$T")" --owner=0 --group=0 -czf "dist/cinehaven-$VER.tar.gz" "cinehaven-$VER"
(cd dist && sha256sum cinehaven-*.tar.gz *.deb 2>/dev/null > SHA256SUMS)
echo "Built: dist/cinehaven-$VER.tar.gz"
