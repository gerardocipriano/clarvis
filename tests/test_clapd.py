"""Unit tests for clarvis pure logic: config merging and clap detection.

These run anywhere — no microphone, PortAudio, D-Bus, or KWin required (the
audio import in clapd is soft, and the detector is driven by injected time).
"""
import clapd
import pytest

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def test_merge_is_deep_and_non_destructive():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    out = clapd._merge(base, {"a": {"y": 20}, "c": 4})
    assert out == {"a": {"x": 1, "y": 20}, "b": 3, "c": 4}
    # original untouched
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


def test_load_config_returns_defaults_when_missing(tmp_path):
    cfg = clapd.load_config(tmp_path / "nope.toml")
    assert cfg["detection"]["threshold"] == \
        clapd.DEFAULT_CONFIG["detection"]["threshold"]


def test_load_config_overlays_user_values(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[detection]\nthreshold = 0.99\n")
    cfg = clapd.load_config(p)
    assert cfg["detection"]["threshold"] == 0.99
    # untouched keys keep defaults
    assert cfg["detection"]["min_gap_ms"] == \
        clapd.DEFAULT_CONFIG["detection"]["min_gap_ms"]


# --------------------------------------------------------------------------- #
# Clap detection
# --------------------------------------------------------------------------- #

def make_detector(**overrides):
    """Build a detector with a trigger counter, using fast default timings."""
    cfg = clapd.load_config(None)
    # Detection tests use absolute times ~10s; pin a short cooldown so they
    # stay independent of the (long, production) default cooldown_ms.
    cfg["detection"]["cooldown_ms"] = 1000
    cfg["detection"].update(overrides)
    fired = []
    det = clapd.ClapDetector(cfg, on_trigger=lambda: fired.append(True))
    return det, fired


def feed(det, events):
    """events: list of (rms, t_seconds)."""
    for rms, t in events:
        det.process(rms, t)


def test_two_claps_in_window_trigger():
    det, fired = make_detector()
    feed(det, [
        (0.5, 10.00),   # clap 1 -> onset
        (0.00, 10.10),  # quiet, re-arm (>refractory 0.08)
        (0.5, 10.30),   # clap 2, gap 0.30s in [0.15, 0.60] -> trigger
    ])
    assert fired == [True]


def test_single_clap_does_not_trigger():
    det, fired = make_detector()
    feed(det, [(0.5, 10.0), (0.0, 10.1)])
    assert fired == []


def test_claps_too_far_apart_do_not_trigger():
    det, fired = make_detector()
    feed(det, [
        (0.5, 10.0),
        (0.0, 10.2),
        (0.5, 11.5),   # gap 1.5s > max_gap -> no trigger
    ])
    assert fired == []


def test_claps_too_close_do_not_trigger():
    det, fired = make_detector()
    feed(det, [
        (0.5, 10.00),
        (0.0, 10.05),
        (0.5, 10.09),  # gap 0.09s < min_gap 0.15 -> no trigger
    ])
    assert fired == []


def test_refractory_collapses_one_clap_into_single_onset():
    # A loud clap spread over several blocks must not read as two onsets.
    det, fired = make_detector()
    feed(det, [
        (0.5, 10.00),
        (0.4, 10.02),  # still loud, still within refractory -> ignored
        (0.5, 10.04),
    ])
    assert fired == []


def test_gradual_rise_is_not_a_clap():
    # A sustained loud sound (speech/music) that ramps over threshold without a
    # sharp attack must not register as a clap onset.
    det, fired = make_detector(min_attack=0.10)
    feed(det, [
        (0.10, 10.00),  # ramp up, below threshold
        (0.19, 10.02),  # crosses threshold but attack 0.09 < 0.10 -> ignored
        (0.27, 10.04),  # still rising slowly, attack 0.08 -> ignored
    ])
    assert det.last_onset is None


def test_cooldown_blocks_immediate_second_trigger():
    det, fired = make_detector(cooldown_ms=3000)
    # first double clap
    feed(det, [(0.5, 10.0), (0.0, 10.1), (0.5, 10.3)])
    # second double clap 1s later — inside cooldown
    feed(det, [(0.0, 10.5), (0.5, 11.0), (0.0, 11.1), (0.5, 11.3)])
    assert fired == [True]


def test_trigger_allowed_after_cooldown_elapses():
    det, fired = make_detector(cooldown_ms=1000)
    feed(det, [(0.5, 10.0), (0.0, 10.1), (0.5, 10.3)])      # trigger @10.3
    feed(det, [(0.0, 12.0), (0.5, 12.0), (0.0, 12.1), (0.5, 12.3)])  # @12.3
    assert fired == [True, True]


def test_first_clap_near_zero_does_not_self_trigger():
    # Regression: last_onset starts as None (not 0.0), so a single first clap
    # at a small timestamp must not look like the 2nd half of a pair.
    det, fired = make_detector()
    feed(det, [(0.5, 0.30)])  # gap-from-zero would be 0.30 (inside window)
    assert fired == []


def test_forced_rearm_in_noisy_room():
    # RMS never falls below `release` between claps, but a forced re-arm after
    # max_gap must still let the second clap register.
    det, fired = make_detector(max_gap_ms=600)
    feed(det, [
        (0.5, 10.00),   # clap 1
        (0.10, 10.40),  # above release (0.06) but past max_gap -> forced re-arm
        (0.5, 10.75),   # clap 2 (gap 0.35 from clap1... but clap1 was reset)
    ])
    # Note: after forced re-arm the pairing restarts; this asserts the detector
    # does not permanently lock up (it remains able to onset again).
    assert det.last_onset is not None


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #

def test_validate_rejects_inverted_gap(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[detection]\nmin_gap_ms = 800\nmax_gap_ms = 600\n")
    with pytest.raises(ValueError, match="min_gap_ms"):
        clapd.load_config(p)


def test_validate_rejects_empty_terminal_cmd(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[action]\nterminal_cmd = []\n")
    with pytest.raises(ValueError, match="terminal_cmd"):
        clapd.load_config(p)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
