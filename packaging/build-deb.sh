#!/bin/bash
# Builds dist/cinehaven_<ver>_all.deb – no root needed to build.
set -e
cd "$(dirname "$0")/.."
VER=$(python3 -c "import re;print(re.search(r'APP_VERSION = \"(.+?)\"', open('cinehaven/__init__.py').read())[1])")
B=$(mktemp -d)/cinehaven_${VER}_all
mkdir -p "$B/DEBIAN" "$B/usr/share/cinehaven" "$B/usr/bin" "$B/usr/share/applications" \
         "$B/usr/share/icons/hicolor/scalable/apps" "$B/usr/share/doc/cinehaven"
cp -r cinehaven "$B/usr/share/cinehaven/"
find "$B" -name __pycache__ -prune -exec rm -rf {} +
install -m755 cinehaven-launcher "$B/usr/bin/cinehaven"
cp data/io.github.cinehaven.CineHaven.desktop "$B/usr/share/applications/"
cp data/io.github.cinehaven.CineHaven.svg "$B/usr/share/icons/hicolor/scalable/apps/"
cp README.md "$B/usr/share/doc/cinehaven/"
cat > "$B/DEBIAN/control" <<CTL
Package: cinehaven
Version: $VER
Section: video
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), python3-libtorrent (>= 2.0), python3-mpv, libmpv2 | libmpv1
Recommends: python3-pychromecast, python3-zeroconf, python3-cryptography, ffmpeg
Maintainer: CineHaven contributors <noreply@cinehaven.invalid>
Description: Stream free movies while they download
 CineHaven searches the Internet Archive's public-domain and open film
 library, checks each title's rights and each file's checksum, and starts
 playing while the torrent downloads. Plays every format via mpv/FFmpeg.
CTL
mkdir -p dist
dpkg-deb --root-owner-group --build "$B" dist/ >/dev/null
echo "Built: dist/cinehaven_${VER}_all.deb"
