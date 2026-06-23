#!/usr/bin/env bash
# clarvis installer — idempotent. Sets up dependencies, the daemon, the KWin
# focus tracker, /dev/uinput access for ydotool, and systemd user services.
#
# Safe to re-run. Steps needing root prompt for sudo only when required.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE="$HOME/.local/share/clarvis"
CONF_DIR="$HOME/.config/clarvis"
KWIN_DIR="$HOME/.local/share/kwin/scripts/clarvis-tracker"
UNIT_DIR="$HOME/.config/systemd/user"
UDEV_RULE="/etc/udev/rules.d/99-clarvis-uinput.rules"

say() { printf '\033[1;36m[clarvis]\033[0m %s\n' "$*"; }

# 1. Dependencies (Arch/Manjaro). --needed skips already-installed packages.
say "installing system packages (sudo)…"
sudo pacman -S --needed --noconfirm \
    python python-numpy python-sounddevice python-dbus python-gobject ydotool

# 2. Daemon + config + control command.
say "installing daemon to $SHARE"
install -Dm644 "$REPO_DIR/daemon/clapd.py" "$SHARE/clapd.py"
install -Dm755 "$REPO_DIR/bin/clarvis" "$HOME/.local/bin/clarvis"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) say "note: add ~/.local/bin to PATH to use the 'clarvis' command";;
esac
mkdir -p "$CONF_DIR"
if [[ -f "$CONF_DIR/config.toml" ]]; then
    say "keeping existing config.toml"
else
    install -Dm644 "$REPO_DIR/config.toml" "$CONF_DIR/config.toml"
fi

# 3. KWin focus tracker (KPackage).
say "installing KWin tracker to $KWIN_DIR"
rm -rf "$KWIN_DIR"
mkdir -p "$KWIN_DIR"
cp -r "$REPO_DIR/kwin/." "$KWIN_DIR/"
if command -v kwriteconfig6 >/dev/null; then
    kwriteconfig6 --file kwinrc --group Plugins --key clarvis-trackerEnabled true
    qdbus6 org.kde.KWin /KWin reconfigure 2>/dev/null || \
        say "could not reconfigure KWin live — log out/in to load the tracker"
else
    say "kwriteconfig6 not found — enable 'clarvis active window tracker' "
    say "manually under System Settings ▸ Window Management ▸ KWin Scripts"
fi

# 4. /dev/uinput access for ydotool (sudo).
if [[ ! -f "$UDEV_RULE" ]]; then
    say "adding udev rule for /dev/uinput (sudo)"
    echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
        | sudo tee "$UDEV_RULE" >/dev/null
    sudo modprobe uinput || true   # ensure the device node exists before trigger
    sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput || true
fi
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
    say "adding $USER to the 'input' group (sudo) — RE-LOGIN required after"
    sudo usermod -aG input "$USER"
    NEED_RELOGIN=1
fi

# 5. systemd user services.
say "installing systemd user services"
install -Dm644 "$REPO_DIR/systemd/ydotoold.service" "$UNIT_DIR/ydotoold.service"
install -Dm644 "$REPO_DIR/systemd/clarvis.service"  "$UNIT_DIR/clarvis.service"
systemctl --user daemon-reload
systemctl --user enable --now ydotoold.service
systemctl --user enable --now clarvis.service

say "done."
say "calibrate the threshold with:  python3 $SHARE/clapd.py --calibrate"
say "logs:  journalctl --user -u clarvis -f"
if [[ "${NEED_RELOGIN:-0}" == "1" ]]; then
    say "NOTE: log out and back in so 'input' group membership takes effect,"
    say "      otherwise the VSCode (ydotool) branch will fail until you do."
fi
