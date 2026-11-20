#!/bin/bash
# Build a self-contained CineHaven for Windows from an MSYS2 UCRT64 environment.
# Output: dist/CineHaven-<version>-windows/ (portable) – then makensis turns it into an installer.
set -euo pipefail
cd "$(dirname "$0")/../.."
VER=$(python -c "import re;print(re.search(r'APP_VERSION = \"(.+?)\"', open('cinehaven/__init__.py').read()).group(1))")
PREFIX=/ucrt64
OUT="dist/CineHaven-$VER-windows"
PYVER=$(python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
rm -rf "$OUT"; mkdir -p "$OUT"/{bin,lib,share/icons,share/glib-2.0,app}

echo "==> Python $PYVER"
cp "$PREFIX/bin/python.exe" "$PREFIX/bin/pythonw.exe" "$OUT/bin/"
cp -r "$PREFIX/lib/python$PYVER" "$OUT/lib/"
( cd "$OUT/lib/python$PYVER" && rm -rf test idlelib tkinter turtledemo lib2to3 \
    && find . -name __pycache__ -prune -exec rm -rf {} + )

echo "==> GTK runtime data"
cp -r "$PREFIX/lib/girepository-1.0" "$PREFIX/lib/gdk-pixbuf-2.0" "$OUT/lib/"
cp -r "$PREFIX/share/glib-2.0/schemas" "$OUT/share/glib-2.0/"
cp -r "$PREFIX/share/icons/Adwaita" "$PREFIX/share/icons/hicolor" "$OUT/share/icons/"
[ -d "$PREFIX/lib/gio" ] && cp -r "$PREFIX/lib/gio" "$OUT/lib/"

echo "==> Programs"
cp "$PREFIX/bin/ffmpeg.exe" "$PREFIX/bin/ffprobe.exe" "$OUT/bin/"

echo "==> DLLs (everything these need, resolved recursively)"
roots=("$PREFIX/bin/python.exe" "$PREFIX/bin/ffmpeg.exe" "$PREFIX/bin/ffprobe.exe")
for d in libgtk-4-1 libadwaita-1-0 libmpv-2 libgirepository-1.0-1 libgirepository-2.0-0 libgraphene-1.0-0 \
         libpangocairo-1.0-0 libpangowin32-1.0-0 libharfbuzz-gobject-0 libcairo-gobject-2 libgdk_pixbuf-2.0-0 \
         librsvg-2-2 libtorrent-rasterbar; do
  for f in "$PREFIX/bin/$d"*.dll; do [ -f "$f" ] && roots+=("$f"); done
done
while IFS= read -r f; do roots+=("$f"); done < <(find "$OUT/lib" -name "*.pyd" -o -name "*.dll")
cp "$PREFIX"/bin/libtorrent*.dll "$OUT/bin/" 2>/dev/null || true
for f in "${roots[@]}"; do
  ntldd -R "$f" 2>/dev/null | grep -io '[a-z]:\\[^ ]*ucrt64\\bin\\[^ ]*\.dll' || true
done | sort -u | while IFS= read -r dll; do cp -n "$(cygpath -u "$dll")" "$OUT/bin/"; done
for f in "${roots[@]}"; do case "$f" in *.dll) cp -n "$f" "$OUT/bin/" ;; esac; done

echo "==> CineHaven"
cp -r cinehaven cinehaven-launcher "$OUT/app/"
mkdir -p "$OUT/app/packaging" && cp packaging/smoke_test.py "$OUT/app/packaging/"
find "$OUT/app" -name __pycache__ -prune -exec rm -rf {} +
magick -background none data/io.github.cinehaven.CineHaven.svg -define icon:auto-resize=256,64,48,32,16 "$OUT/CineHaven.ico"
cat > "$OUT/CineHaven.bat" <<'BAT'
@echo off
start "" "%~dp0bin\pythonw.exe" "%~dp0app\cinehaven-launcher" %*
BAT
du -sh "$OUT"
echo "$OUT" > dist/windows-dir.txt
