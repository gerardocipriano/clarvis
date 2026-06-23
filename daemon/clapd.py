#!/usr/bin/env python3
"""clarvis — clap-to-Claude daemon.

Listens to the microphone, detects a "double clap" (two sharp transients within
a configurable time window) and, when triggered, launches Claude Code:

  * If the active window is VSCode  -> open the *integrated* terminal at the
    project root (Ctrl+`) via ydotool and type `claude`.
  * Otherwise                       -> open a standalone Konsole running claude.

How it knows the active window: KWin scripts are sandboxed and cannot write
files, so a tiny session D-Bus service (zero.gc.clarvis) is exposed here;
the companion KWin script calls SetActiveWindow(class) on every focus change.

Security model: this is a *single-user, trusted-session* tool. The D-Bus
service refuses to start if the well-known name is already taken (anti-squat),
and the VSCode branch re-checks focus immediately before injecting keystrokes
(global uinput injection cannot bind to a target, so this is best-effort).

Design goals: no audio is ever written to disk (only in-RAM RMS analysis),
every tunable lives in config.toml, and the process survives a disappearing
audio device so systemd never has to babysit it.

Usage:
    clapd.py                  run the daemon (what systemd does)
    clapd.py --calibrate      print live RMS / detected onsets, trigger nothing
    clapd.py --config PATH     use an alternate config file

Author: Gerardo Cipriano — inspired by Tony Stark's JARVIS.
"""

from __future__ import annotations

import argparse
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import numpy as np

# sounddevice is imported softly: the pure detection logic must remain
# importable (and unit-testable) on machines without PortAudio. Only run() needs
# a working stream, and it checks for this None below.
try:
    import sounddevice as sd
except (OSError, ImportError) as exc:  # PortAudio/lib missing
    sd = None
    _SD_IMPORT_ERROR = exc
else:
    _SD_IMPORT_ERROR = None


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "audio": {
        "samplerate": 16000,
        "block_ms": 20,          # analysis block size in milliseconds
        "device": None,          # None -> default input; or name/index string
    },
    "detection": {
        "threshold": 0.18,       # RMS level (0..1) that counts as a transient
        "release": 0.06,         # RMS must drop below this before re-arming
        "refractory_ms": 80,     # ignore new onsets for this long after one
        "min_gap_ms": 150,       # min spacing between the two claps
        "max_gap_ms": 600,       # max spacing between the two claps
        "cooldown_ms": 3000,     # ignore triggers for this long after firing
    },
    "action": {
        # Standalone terminal command (non-VSCode case). argv-style list.
        "terminal_cmd": ["konsole", "--hold", "-e", "claude"],
        # Window resourceClass values that mean "we are inside VSCode".
        "vscode_classes": ["code", "code-insiders", "codium", "vscodium"],
        # Command run inside the VSCode integrated terminal.
        "vscode_run": "claude",
        # Delay between opening the integrated terminal and typing into it.
        "vscode_open_delay_ms": 450,
        # Hard timeout for any single ydotool invocation (s) — keeps a wedged
        # ydotoold from blocking the daemon forever.
        "ydotool_timeout_s": 5,
    },
}

# D-Bus identity shared with the KWin script (kwin/contents/code/main.js).
DBUS_NAME = "zero.gc.clarvis"
DBUS_PATH = "/zero/gc/clarvis"


def _merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base."""
    out = {k: (v.copy() if isinstance(v, dict) else v) for k, v in base.items()}
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def validate_config(cfg: dict) -> None:
    """Fail fast on nonsensical config rather than silently never triggering."""
    d = cfg["detection"]
    if d["min_gap_ms"] >= d["max_gap_ms"]:
        raise ValueError(
            f"detection.min_gap_ms ({d['min_gap_ms']}) must be < "
            f"max_gap_ms ({d['max_gap_ms']})")
    if d["release"] > d["threshold"]:
        raise ValueError(
            f"detection.release ({d['release']}) must be <= "
            f"threshold ({d['threshold']})")
    if not isinstance(cfg["action"]["terminal_cmd"], list) or \
            not cfg["action"]["terminal_cmd"]:
        raise ValueError("action.terminal_cmd must be a non-empty list")


def load_config(path: Path | None) -> dict:
    if path and path.is_file():
        with path.open("rb") as fh:
            user_cfg = tomllib.load(fh)
        cfg = _merge(DEFAULT_CONFIG, user_cfg)
    else:
        cfg = {k: v.copy() for k, v in DEFAULT_CONFIG.items()}
    validate_config(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# Active-window tracker (session D-Bus service fed by the KWin script)
# --------------------------------------------------------------------------- #

class WindowTracker:
    """Holds the current active-window class, updated over D-Bus by KWin.

    Runs a GLib main loop on a daemon thread. If D-Bus is unavailable the
    tracker degrades gracefully to an empty class (everything falls back to the
    standalone terminal branch).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current = ""

    @property
    def current(self) -> str:
        with self._lock:
            return self._current

    def _set(self, cls: str) -> None:
        with self._lock:
            self._current = (cls or "").strip().lower()

    def start(self) -> None:
        threading.Thread(target=self._serve, name="clarvis-dbus",
                         daemon=True).start()

    def _serve(self) -> None:
        try:
            import dbus
            import dbus.service
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib
        except Exception as exc:  # noqa: BLE001 - any import/runtime failure
            print(f"[clarvis] D-Bus unavailable ({exc}); "
                  "VSCode detection disabled", file=sys.stderr)
            return

        tracker = self

        class Service(dbus.service.Object):
            @dbus.service.method(DBUS_NAME, in_signature="s")
            def SetActiveWindow(self, cls):  # noqa: N802 - D-Bus method name
                tracker._set(str(cls))

        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        # Anti-squat: refuse to run if the name is already owned, instead of
        # silently queueing behind whoever grabbed it first.
        name = dbus.service.BusName(DBUS_NAME, bus,
                                    do_not_queue=True, allow_replacement=False)
        if name.get_bus().name_has_owner(DBUS_NAME) and \
                bus.get_unique_name() != name.get_bus().get_name_owner(DBUS_NAME):
            print(f"[clarvis] {DBUS_NAME} already owned by another process; "
                  "VSCode detection disabled", file=sys.stderr)
            return
        Service(bus, DBUS_PATH)
        print(f"[clarvis] D-Bus service {DBUS_NAME} ready")
        GLib.MainLoop().run()


# --------------------------------------------------------------------------- #
# Action layer
# --------------------------------------------------------------------------- #

# Linux input event codes (see /usr/include/linux/input-event-codes.h)
KEY_LEFTCTRL = 29
KEY_ENTER = 28
KEY_GRAVE = 41  # the backtick / `~` key


class Actuator:
    """Decides what to launch when a double clap fires."""

    def __init__(self, cfg: dict, tracker: WindowTracker):
        self.cfg = cfg
        self.tracker = tracker
        self._vscode = {c.lower() for c in cfg["action"]["vscode_classes"]}
        self._timeout = cfg["action"]["ydotool_timeout_s"]

    def _is_vscode(self) -> bool:
        return self.tracker.current in self._vscode

    def fire(self) -> None:
        """Run the action. Called on a worker thread so it never blocks audio."""
        if self._is_vscode():
            self._open_in_vscode()
        else:
            self._open_standalone()

    def _open_standalone(self) -> None:
        cmd = self.cfg["action"]["terminal_cmd"]
        print(f"[clarvis] launching standalone terminal: "
              f"{' '.join(map(str, cmd))}")
        try:
            subprocess.Popen(cmd, start_new_session=True)
        except FileNotFoundError:
            print(f"[clarvis] terminal not found: {cmd[0]!r}", file=sys.stderr)

    def _open_in_vscode(self) -> None:
        run = self.cfg["action"]["vscode_run"]
        delay = self.cfg["action"]["vscode_open_delay_ms"] / 1000.0
        print("[clarvis] VSCode focused -> opening integrated terminal")
        # Ctrl+` toggles the integrated terminal, which opens at the workspace root.
        if not self._ydotool(
            "key", f"{KEY_LEFTCTRL}:1", f"{KEY_GRAVE}:1",
            f"{KEY_GRAVE}:0", f"{KEY_LEFTCTRL}:0"
        ):
            return
        time.sleep(delay)
        # TOCTOU guard: focus may have moved during the delay. Global uinput
        # injection can't target a window, so abort rather than type into
        # whatever is now focused (could be a shell, a chat box, a sudo prompt).
        if not self._is_vscode():
            print("[clarvis] focus left VSCode before typing — aborting",
                  file=sys.stderr)
            return
        self._ydotool("type", run)
        self._ydotool("key", f"{KEY_ENTER}:1", f"{KEY_ENTER}:0")

    def _ydotool(self, *args: str) -> bool:
        try:
            subprocess.run(["ydotool", *args], check=True, timeout=self._timeout)
            return True
        except FileNotFoundError:
            print("[clarvis] ydotool not installed; cannot drive VSCode",
                  file=sys.stderr)
        except subprocess.TimeoutExpired:
            print("[clarvis] ydotool timed out; is ydotoold running?",
                  file=sys.stderr)
        except subprocess.CalledProcessError as exc:
            print(f"[clarvis] ydotool failed ({exc.returncode}); "
                  "is ydotoold running and /dev/uinput accessible?",
                  file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# Clap detection
# --------------------------------------------------------------------------- #

class ClapDetector:
    """Onset-based double-clap detector driven by per-block RMS values.

    A clap is a sharp transient: RMS jumps above ``threshold`` from a quiet
    state, then decays. We re-arm only after RMS falls back below ``release``
    (plus a refractory delay), so a single clap yields exactly one onset.
    Two onsets spaced within [min_gap, max_gap] fire a trigger.
    """

    def __init__(self, cfg: dict, on_trigger, verbose: bool = False):
        d = cfg["detection"]
        self.threshold = d["threshold"]
        self.release = d["release"]
        self.refractory = d["refractory_ms"] / 1000.0
        self.min_gap = d["min_gap_ms"] / 1000.0
        self.max_gap = d["max_gap_ms"] / 1000.0
        self.cooldown = d["cooldown_ms"] / 1000.0
        self.on_trigger = on_trigger
        self.verbose = verbose

        self.armed = True
        self.disarmed_at = 0.0        # when the current disarm started
        self.last_onset = None        # None = no previous clap yet
        self.last_trigger = 0.0

    def process(self, rms: float, now: float) -> None:
        if self.verbose:
            bar = "#" * int(min(rms, 1.0) * 50)
            mark = " <ONSET" if (self.armed and rms >= self.threshold) else ""
            print(f"\rrms={rms:0.3f} |{bar:<50}|{mark}", end="", flush=True)

        if self.armed and rms >= self.threshold:
            self._onset(now)
        elif not self.armed:
            quiet = rms < self.release
            elapsed = now - self.disarmed_at
            # Re-arm on a falling edge after the refractory window, OR force a
            # re-arm if we have been stuck disarmed past max_gap (a noisy room
            # whose RMS never drops below `release` must not lock us out).
            if elapsed >= self.refractory and (quiet or elapsed >= self.max_gap):
                self.armed = True

    def _onset(self, now: float) -> None:
        self.armed = False
        self.disarmed_at = now
        if self.last_onset is not None:
            gap = now - self.last_onset
            if self.min_gap <= gap <= self.max_gap and \
                    now - self.last_trigger >= self.cooldown:
                self.last_trigger = now
                self.last_onset = None
                if self.verbose:
                    print(f"\n[clarvis] DOUBLE CLAP (gap={gap*1000:.0f}ms)")
                self.on_trigger()
                return
        self.last_onset = now


# --------------------------------------------------------------------------- #
# Audio loop
# --------------------------------------------------------------------------- #

def run(cfg: dict, calibrate: bool) -> None:
    if sd is None:
        print(f"[clarvis] cannot load sounddevice/PortAudio: {_SD_IMPORT_ERROR}",
              file=sys.stderr)
        sys.exit(1)

    a = cfg["audio"]
    samplerate = a["samplerate"]
    blocksize = int(samplerate * a["block_ms"] / 1000)
    device = a["device"]

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # Bounded queue of (rms, capture_time). Timestamp at capture so detection
    # timing is immune to consumer lag; drop oldest-on-full so a stalled
    # consumer can never grow memory without bound.
    rms_q: "queue.Queue[tuple[float, float]]" = queue.Queue(maxsize=64)

    def callback(indata, frames, time_info, status):  # runs on audio thread
        if status:
            print(f"[clarvis] audio status: {status}", file=sys.stderr)
        rms = float(np.sqrt(np.mean(np.square(indata[:, 0]))))
        try:
            rms_q.put_nowait((rms, time.monotonic()))
        except queue.Full:
            pass  # consumer is behind; dropping a block is fine

    tracker = WindowTracker()
    if not calibrate:
        tracker.start()
    actuator = Actuator(cfg, tracker)

    # Run the action off the consumer thread: launching terminals / driving
    # ydotool must never stall RMS consumption.
    def trigger() -> None:
        if calibrate:
            return
        threading.Thread(target=actuator.fire, name="clarvis-fire",
                         daemon=True).start()

    detector = ClapDetector(cfg, on_trigger=trigger, verbose=calibrate)

    backoff = 1.0
    while not stop.is_set():
        try:
            with sd.InputStream(
                samplerate=samplerate,
                blocksize=blocksize,
                channels=1,
                dtype="float32",
                device=device,
                callback=callback,
            ):
                print("[clarvis] calibrate mode — clap away (Ctrl+C to quit)"
                      if calibrate else "[clarvis] listening for double claps")
                backoff = 1.0
                while not stop.is_set():
                    try:
                        rms, t = rms_q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    detector.process(rms, t)
        except Exception as exc:  # device vanished, etc. — retry, never die
            if stop.is_set():
                break
            print(f"\n[clarvis] audio error: {exc}; retry in {backoff:.0f}s",
                  file=sys.stderr)
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
    print("\n[clarvis] shutting down")


def main() -> None:
    parser = argparse.ArgumentParser(description="clarvis clap-to-Claude daemon")
    parser.add_argument("--calibrate", action="store_true",
                        help="print live RMS and onsets without triggering")
    parser.add_argument("--config", type=Path,
                        default=Path(os.path.expanduser(
                            "~/.config/clarvis/config.toml")),
                        help="path to config.toml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run(cfg, calibrate=args.calibrate)


if __name__ == "__main__":
    main()
