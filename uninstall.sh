#!/usr/bin/env bash
# clarvis uninstaller. Removes services, daemon, and KWin tracker. Leaves the
# 'input' group membership and udev rule in place (harmless, may be shared).
set -euo pipefail

say() { printf '\033[1;36m[clarvis]\033[0m %s\n' "$*"; }

say "stopping & disabling services"
systemctl --user disable --now clarvis.service 2>/dev/null || true
systemctl --user disable --now ydotoold.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/clarvis.service" \
      "$HOME/.config/systemd/user/ydotoold.service"
systemctl --user daemon-reload

say "removing KWin tracker"
kwriteconfig6 --file kwinrc --group Plugins --key clarvis-trackerEnabled false 2>/dev/null || true
qdbus6 org.kde.KWin /KWin reconfigure 2>/dev/null || true
rm -rf "$HOME/.local/share/kwin/scripts/clarvis-tracker"

say "removing daemon (config in ~/.config/clarvis kept)"
rm -rf "$HOME/.local/share/clarvis"

say "done. To fully clean up:  sudo rm /etc/udev/rules.d/99-clarvis-uinput.rules"
