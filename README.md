<div align="center">

# 👏 clarvis 👏

### *Clap twice, and Claude shows up.*

**clap + JARVIS → opens **Cla**ude** — your own Stark-grade voice trigger, minus the arc reactor.

[![CI](https://github.com/gerardocipriano/clarvis/actions/workflows/ci.yml/badge.svg)](https://github.com/gerardocipriano/clarvis/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Platform: KDE Wayland](https://img.shields.io/badge/platform-KDE%20Plasma%206%20·%20Wayland-1d99f3.svg)](https://kde.org/plasma-desktop/)
[![Tests](https://img.shields.io/badge/tests-14%20passing-brightgreen.svg)](tests/)
[![Made for Claude Code](https://img.shields.io/badge/made%20for-Claude%20Code-d97757.svg)](https://claude.com/claude-code)

```
        .  o ..                     ┌─────────────────────┐
        o . o o.o      👏 👏  ───▶   │  $ claude           │
           ...oo                    │  ▏                  │
             __[]__                 └─────────────────────┘
```

</div>

A small background daemon for **KDE Plasma 6 on Wayland** that listens to your
microphone and, when it hears a **double clap**, launches [Claude Code](https://claude.com/claude-code):

- **In VSCode** (window focused): opens the **integrated terminal** at the
  project root and runs `claude`.
- **Anywhere else**: opens a standalone **Konsole** running `claude`.

No audio is ever written to disk — only in-RAM RMS analysis. Everything tunable
lives in one config file.

## How it works

```
microphone ──▶ clapd.py (RMS onset detector) ──▶ double clap?
                     │                                   │
                     │ which window is focused?          ▼
    KWin script ──D-Bus──▶ zero.gc.clarvis      ┌──────────────┐
   (windowActivated)                              │  VSCode?     │
                                                   ├──────────────┤
                                           yes ──▶ ydotool: Ctrl+] Ctrl+] + type "claude"
                                            no ──▶ konsole --hold -e claude
```

- **`daemon/clapd.py`** — captures audio (`sounddevice`), detects two sharp
  transients within a configurable gap, and fires the action. Hosts a tiny
  session D-Bus service so KWin can tell it the active window.
- **`kwin/`** — a KWin script (KPackage) that reports the active window's class
  to the daemon on every focus change. KWin scripts can't write files, so this
  goes over D-Bus.
- **`systemd/`** — user services for the daemon and for `ydotoold` (the uinput
  helper that sends keyboard shortcuts and types into VSCode on Wayland).

## Requirements

- KDE Plasma 6 / KWin on Wayland
- Arch/Manjaro (installer uses `pacman`); packages: `python-sounddevice`,
  `python-numpy`, `python-dbus`, `python-gobject`, `ydotool`
- `/dev/uinput` access (the installer adds a udev rule + `input` group)

## Install

```bash
git clone git@github.com:gerardocipriano/clarvis.git
cd clarvis
./install.sh
```

Then **calibrate** the clap threshold for your room and mic:

```bash
python3 ~/.local/share/clarvis/clapd.py --calibrate
```

Clap and watch the live RMS bar; set `detection.threshold` in
`~/.config/clarvis/config.toml` just below your clap peaks, then restart:

```bash
systemctl --user restart clarvis
```

> If `install.sh` added you to the `input` group, **log out and back in** before
> the VSCode (ydotool) branch will work.

## Turning it on and off

The installer drops a tiny `clarvis` command in `~/.local/bin`:

```bash
clarvis off        # stop listening now
clarvis on         # start listening now
clarvis status     # is it listening?
clarvis restart    # reload after editing config
clarvis logs       # follow the daemon log
clarvis calibrate  # live RMS meter to tune the clap threshold
clarvis enable     # start automatically at login (default)
clarvis disable    # don't start at login
```

## Configuration

All options live in `~/.config/clarvis/config.toml` (see [`config.toml`](config.toml)
for the annotated defaults). Most-tuned keys:

| Key | Meaning |
|-----|---------|
| `detection.threshold` | RMS level that counts as a clap (raise to reduce false positives) |
| `detection.min_gap_ms` / `max_gap_ms` | accepted spacing between the two claps |
| `detection.cooldown_ms` | ignore further triggers for this long after firing |
| `sound.enabled` | play JARVIS chime on trigger (`true` / `false`) |
| `sound.volume` | 0–1, 0.3 ≈ medium-low |
| `action.terminal_cmd` | command for the standalone (non-VSCode) case |
| `action.vscode_classes` | window classes treated as VSCode |

## Sound

On every trigger clarvis plays the iconic **JARVIS 3-note chime** (synthesised in
RAM — no audio files). Toggle with:

```bash
clarvis sound off   # silent trigger
clarvis sound on    # re-enable
```

Or by editing `sound.enabled` in the config file.

## Logs & troubleshooting

```bash
journalctl --user -u clarvis -f      # daemon logs
systemctl --user status ydotoold     # uinput helper
```

- **VSCode branch does nothing** → check `ydotoold` is running and you've
  re-logged-in after the `input` group change.

### Italian (or non-US) keyboard layout

The daemon sends `Ctrl+] Ctrl+]` (a layout-independent keycode) to toggle
VSCode's integrated terminal. Add this keybinding to
`~/.config/Code/User/keybindings.json` so VSCode responds:

```json
{
  "key": "ctrl+] ctrl+]",
  "command": "workbench.action.terminal.toggleTerminal"
}
```

> **Important**: do **not** include a `"when"` clause — if restricted to
> `terminal.active` the shortcut only works when the terminal is already
> focused, defeating its purpose.

- **Never triggers / triggers too easily** → re-run `--calibrate` and adjust
  `threshold`.
- **Active window always falls back to Konsole** → confirm the KWin tracker is
  enabled (`System Settings ▸ Window Management ▸ KWin Scripts`) and the daemon
  logs `D-Bus service zero.gc.clarvis ready`.

## Uninstall

```bash
./uninstall.sh
```

## License

MIT — see [LICENSE](LICENSE).
