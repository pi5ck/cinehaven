#!/bin/bash
# CineHaven installer – Ubuntu 24.04+/Debian 13+, Fedora, Arch, openSUSE.
# Installs for your user (no root needed after the system packages), and keeps itself up to date.
set -e
cd "$(dirname "$0")"
echo "==> Installing system packages (needs your password)…"
if command -v apt-get >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
       python3-libtorrent python3-mpv libmpv2
elif command -v dnf >/dev/null; then
  sudo dnf install -y python3 python3-gobject gtk4 libadwaita \
       rb_libtorrent-python3 python3-mpv mpv-libs
elif command -v pacman >/dev/null; then
  sudo pacman -S --needed --noconfirm python python-gobject gtk4 libadwaita \
       libtorrent-rasterbar python-mpv mpv
elif command -v zypper >/dev/null; then
  sudo zypper install -y python3 python3-gobject typelib-1_0-Adw-1 typelib-1_0-Gtk-4_0 \
       python3-libtorrent-rasterbar python3-mpv libmpv2
else
  echo "Unknown distro: install GTK4, libadwaita, python3-libtorrent, python-mpv and libmpv yourself."
fi

echo "==> Installing CineHaven for $USER…"
APP=~/.local/share/cinehaven-app
rm -rf "$APP"; mkdir -p "$APP" ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
cp -r cinehaven "$APP/"
cp cinehaven-launcher "$APP/cinehaven-launcher"
ln -sf "$APP/cinehaven-launcher" ~/.local/bin/cinehaven
sed "s|^Exec=cinehaven %U|Exec=$HOME/.local/bin/cinehaven %U|" data/io.github.cinehaven.CineHaven.desktop \
  > ~/.local/share/applications/io.github.cinehaven.CineHaven.desktop
cp data/io.github.cinehaven.CineHaven.svg ~/.local/share/icons/hicolor/scalable/apps/
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
xdg-mime default io.github.cinehaven.CineHaven.desktop x-scheme-handler/magnet application/x-bittorrent 2>/dev/null || true

python3 -c "import libtorrent, mpv, gi; gi.require_version('Adw','1')" \
  && echo "==> Done! Launch “CineHaven” from your app menu, or run: cinehaven" \
  || echo "!! Some dependencies are missing – see messages above."
