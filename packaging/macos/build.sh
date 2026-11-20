#!/bin/bash
# Build CineHaven.app for macOS (uses Homebrew's GTK, mpv and libtorrent; offers to install them).
# Output: dist/CineHaven-<version>-macos.zip
set -euo pipefail
cd "$(dirname "$0")/../.."
VER=$(python3 -c "import re;print(re.search(r'APP_VERSION = \"(.+?)\"', open('cinehaven/__init__.py').read()).group(1))")
APPDIR="dist/CineHaven.app"
rm -rf "$APPDIR"; mkdir -p "$APPDIR/Contents/MacOS" "$APPDIR/Contents/Resources/app"
cp -r cinehaven cinehaven-launcher "$APPDIR/Contents/Resources/app/"
mkdir -p "$APPDIR/Contents/Resources/app/packaging" && cp packaging/smoke_test.py "$APPDIR/Contents/Resources/app/packaging/"
find "$APPDIR" -name __pycache__ -prune -exec rm -rf {} +
cp packaging/macos/CineHaven "$APPDIR/Contents/MacOS/CineHaven"
chmod +x "$APPDIR/Contents/MacOS/CineHaven"

# icon
ICONSET=$(mktemp -d)/CineHaven.iconset; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  rsvg-convert -w $s -h $s data/io.github.cinehaven.CineHaven.svg -o "$ICONSET/icon_${s}x${s}.png"
  rsvg-convert -w $((s*2)) -h $((s*2)) data/io.github.cinehaven.CineHaven.svg -o "$ICONSET/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$ICONSET" -o "$APPDIR/Contents/Resources/CineHaven.icns"

cat > "$APPDIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>CineHaven</string>
  <key>CFBundleDisplayName</key><string>CineHaven</string>
  <key>CFBundleIdentifier</key><string>io.github.cinehaven.CineHaven</string>
  <key>CFBundleVersion</key><string>$VER</string>
  <key>CFBundleShortVersionString</key><string>$VER</string>
  <key>CFBundleExecutable</key><string>CineHaven</string>
  <key>CFBundleIconFile</key><string>CineHaven</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
# ad-hoc signature so Apple Silicon will run it (not notarized: first launch needs right-click → Open)
codesign --force --deep --sign - "$APPDIR" || true
(cd dist && ditto -c -k --keepParent CineHaven.app "CineHaven-$VER-macos.zip")
ls -la "dist/CineHaven-$VER-macos.zip"
