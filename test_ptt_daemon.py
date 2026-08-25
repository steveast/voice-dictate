#!/usr/bin/env python
"""Unit tests for the parts of the daemon that can be checked without a mic.

Run them with the venv, which is where numpy/evdev live:

    ./test_ptt_daemon.py            # or: venv/bin/python -m unittest -v test_ptt_daemon

Deliberately stdlib `unittest` rather than pytest: the daemon has no test
dependency today and one assert-heavy file is not worth adding one.

What is covered here is the capture SOURCE, because that is where a whole class
of silent breakage lives. The daemon used to record from whatever PipeWire called
the default source, so connecting a Bluetooth headset handed dictation to a
16 kHz HFP mic with a call-grade noise gate in it, and unplugging one handed it
to a far-field laptop array — a 30 dB drop in speech-band SNR, measured. Neither
announces itself: whisper does not report damaged audio, it just returns
confident nonsense. So the mic is named, a fallback is named after it, and these
tests hold both in place.
"""
import os
import sys
import subprocess
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ptt_daemon as vd  # noqa: E402

BUDS = "bluez_input.C4:60:0A:A4:1D:64"
BUILTIN = "alsa_input.pci-0000_00_1f.3.HiFi__Mic1__source"


def only(*names):
    """Pretend exactly `names` are on PATH, so a recorder branch can be forced."""
    return mock.patch.object(vd, "on_path", lambda n: n in names)


class FindRecorder(unittest.TestCase):
    def test_first_recorder_on_path_wins(self):
        with only("parecord", "arecord"):
            self.assertEqual(vd.find_recorder("16000", "1")[0], "parecord")

    def test_rate_and_channels_are_passed(self):
        with only("pw-record"):
            cmd = vd.find_recorder("48000", "2")
        self.assertEqual(cmd[cmd.index("--rate") + 1], "48000")
        self.assertEqual(cmd[cmd.index("--channels") + 1], "2")

    def test_no_recorder_on_path(self):
        with only():
            self.assertIsNone(vd.find_recorder())


class PinFlag(unittest.TestCase):
    """Each recorder spells "capture from THIS device" differently."""

    def test_pw_record(self):
        self.assertEqual(vd.pin_flag(["pw-record", "--rate", "48000"], BUDS),
                         ["--target", BUDS])

    def test_parecord(self):
        self.assertEqual(vd.pin_flag(["parecord", "--rate=48000"], BUDS),
                         [f"--device={BUDS}"])

    def test_arecord(self):
        self.assertEqual(vd.pin_flag(["arecord", "-f", "S16_LE"], "hw:0,6"),
                         ["-D", "hw:0,6"])

    def test_no_source_means_no_flag(self):
        # Empty = follow the system default, which is the documented default and
        # must stay a plain unpinned command.
        self.assertEqual(vd.pin_flag(["pw-record"], ""), [])

    def test_unknown_recorder_is_left_alone(self):
        self.assertEqual(vd.pin_flag(["sox", "-d"], BUDS), [])

    def test_flag_goes_before_the_output_path(self):
        # Dictation.start appends the WAV path; all three recorders want the
        # filename last, so a pin appended after it would be read as the output.
        cmd = ["pw-record"] + vd.pin_flag(["pw-record"], BUDS) + ["/tmp/x.wav"]
        self.assertEqual(cmd[-1], "/tmp/x.wav")


class PickSource(unittest.TestCase):
    """Which of the named mics to record from, given what is plugged in now."""

    def test_first_present_preference_wins(self):
        self.assertEqual(vd.pick_source([BUDS, BUILTIN], {BUDS, BUILTIN}), BUDS)

    def test_falls_through_to_the_next(self):
        # The headset ran out of battery mid-session: dictation must land on the
        # laptop mic rather than fail outright.
        self.assertEqual(vd.pick_source([BUDS, BUILTIN], {BUILTIN}), BUILTIN)

    def test_none_present_means_system_default(self):
        # Better an unpinned recording than no recording: a lost take cannot be
        # dictated again, and the daemon says which mic it settled on.
        self.assertEqual(vd.pick_source([BUDS, BUILTIN], {"something.else"}), "")

    def test_no_preferences_means_system_default(self):
        self.assertEqual(vd.pick_source([], {BUDS}), "")

    def test_unknown_availability_honours_the_pin(self):
        # Nothing could answer "what sources exist" — trust the configuration
        # rather than silently unpinning to whatever the desktop points at.
        self.assertEqual(vd.pick_source([BUDS, BUILTIN], None), BUDS)


class ListSources(unittest.TestCase):
    def run_returns(self, stdout, rc=0):
        return mock.patch.object(
            vd.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout, b""))

    def test_parses_pactl_short_output(self):
        out = (f"66\t{BUILTIN}\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
               f"477\t{BUDS}\tPipeWire\tfloat32le 1ch 48000Hz\tSUSPENDED\n").encode()
        with only("pactl"), self.run_returns(out):
            self.assertEqual(vd.list_sources(), {BUILTIN, BUDS})

    def test_no_tool_returns_unknown(self):
        # None, not an empty set: "cannot tell" must not read as "nothing there",
        # which would unpin every mic.
        with only():
            self.assertIsNone(vd.list_sources())

    def test_failing_tool_returns_unknown(self):
        with only("pactl"), self.run_returns(b"", rc=1):
            self.assertIsNone(vd.list_sources())

    def test_broken_tool_returns_unknown(self):
        def boom(*a, **k):
            raise OSError("no")
        with only("pactl"), mock.patch.object(vd.subprocess, "run", boom):
            self.assertIsNone(vd.list_sources())


class AvailableSources(unittest.TestCase):
    """The list is cached: resolving the mic happens on the key press, and a
    subprocess there would clip the first word."""

    def setUp(self):
        vd.reset_source_cache()

    tearDown = setUp

    def test_second_call_within_the_window_does_not_reprobe(self):
        calls = []
        with mock.patch.object(vd, "list_sources", lambda: calls.append(1) or {BUDS}):
            self.assertEqual(vd.available_sources(now=100.0, max_age=10), {BUDS})
            self.assertEqual(vd.available_sources(now=105.0, max_age=10), {BUDS})
        self.assertEqual(len(calls), 1)

    def test_reprobes_once_the_window_passes(self):
        calls = []
        with mock.patch.object(vd, "list_sources", lambda: calls.append(1) or {BUDS}):
            vd.available_sources(now=100.0, max_age=10)
            vd.available_sources(now=111.0, max_age=10)
        self.assertEqual(len(calls), 2)

    def test_unknown_is_not_cached(self):
        # A failed probe must not pin "cannot tell" in place for the next ten
        # seconds; the next call should try again.
        calls = []
        with mock.patch.object(vd, "list_sources", lambda: calls.append(1) or None):
            self.assertIsNone(vd.available_sources(now=100.0, max_age=10))
            self.assertIsNone(vd.available_sources(now=100.5, max_age=10))
        self.assertEqual(len(calls), 2)


class SourcesFromEnv(unittest.TestCase):
    """VD_SOURCE is what the unit file sets; it must arrive intact."""

    def reload_with(self, value):
        import importlib
        with mock.patch.dict(os.environ, {"VD_SOURCE": value}):
            return importlib.reload(vd)

    def tearDown(self):
        import importlib
        os.environ.pop("VD_SOURCE", None)
        importlib.reload(vd)

    def test_single_name(self):
        self.assertEqual(self.reload_with(BUDS).SOURCES, [BUDS])

    def test_comma_separated_preference_order(self):
        self.assertEqual(self.reload_with(f"{BUDS},{BUILTIN}").SOURCES,
                         [BUDS, BUILTIN])

    def test_spaces_are_separators_too(self):
        # Same spelling as VD_PTT_KEY_2 / VD_POLISH_KEY, which already accept
        # both; node names never contain spaces.
        self.assertEqual(self.reload_with(f"{BUDS}, {BUILTIN}").SOURCES,
                         [BUDS, BUILTIN])

    def test_empty_means_system_default(self):
        self.assertEqual(self.reload_with("   ").SOURCES, [])

    def test_unset_means_system_default(self):
        import importlib
        os.environ.pop("VD_SOURCE", None)
        self.assertEqual(importlib.reload(vd).SOURCES, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
