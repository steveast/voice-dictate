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


class BuildBackend(unittest.TestCase):
    """Which engine a VOICE_DICTATE_BACKEND name selects, and what happens when
    it will not start.

    The fallback is the part worth pinning. Every GPU engine here depends on a
    runtime, a device and a multi-gigabyte model directory, any of which can go
    missing between reboots — and dictation is the user's input method, so the
    daemon that cannot reach the iGPU has to come up on the CPU rather than not
    come up. A second engine made that an easy thing to break by accident: the
    old code named OpenVINO twice, so a new backend bolted on beside it could
    silently inherit no fallback at all.
    """

    def build_with(self, name, **patches):
        with mock.patch.object(vd, "BACKEND", name), \
             mock.patch.object(vd, "notify", lambda *a, **k: None), \
             mock.patch.multiple(vd, **patches):
            return vd.build_backend()

    def ok(self, label):
        """A stand-in engine that loads, tagged so the choice is visible."""
        return lambda: label

    def test_openvino_by_name(self):
        self.assertEqual(self.build_with("openvino", OpenVinoBackend=self.ok("ov")),
                         "ov")

    def test_qwen_by_name(self):
        self.assertEqual(self.build_with("qwen3-asr", QwenAsrBackend=self.ok("qwen")),
                         "qwen")

    def test_qwen_short_alias(self):
        self.assertEqual(self.build_with("qwen", QwenAsrBackend=self.ok("qwen")),
                         "qwen")

    def test_cpu_by_name(self):
        self.assertEqual(
            self.build_with("faster-whisper", FasterWhisperBackend=self.ok("cpu")),
            "cpu")

    def test_unknown_name_falls_back_rather_than_raising(self):
        self.assertEqual(self.build_with("nonsense",
                                         FasterWhisperBackend=self.ok("cpu")),
                         "cpu")

    def test_a_gpu_engine_that_will_not_start_degrades_to_the_cpu(self):
        def boom():
            raise RuntimeError("no device")
        for name, attr in (("openvino", "OpenVinoBackend"),
                           ("qwen3-asr", "QwenAsrBackend")):
            with self.subTest(backend=name):
                self.assertEqual(
                    self.build_with(name, **{attr: boom,
                                             "FasterWhisperBackend": self.ok("cpu")}),
                    "cpu")

    def test_a_missing_model_directory_degrades_too(self):
        # The most likely real failure: the model was never downloaded, or the
        # directory moved. FileNotFoundError is not an Exception subclass people
        # always remember to catch, so prove this one is.
        def missing():
            raise FileNotFoundError("no Qwen3-ASR model at /nope")
        self.assertEqual(
            self.build_with("qwen3-asr", QwenAsrBackend=missing,
                            FasterWhisperBackend=self.ok("cpu")),
            "cpu")


class KeepWarm(unittest.TestCase):
    """The idle worker re-touches the model so the take after a quiet spell
    does not wait on swap-in (3.1s of audio once took 18.5s).

    What is pinned: warming happens only while the queue is empty, a real take
    always wins, and a failing warm-up can neither kill the worker nor keep
    logging the same error every interval for the rest of the session."""

    class Engine:
        def __init__(self, on_warm=None, fail=False):
            self.warmed = 0
            self.on_warm = on_warm
            self.fail = fail

        def warm(self):
            self.warmed += 1
            if self.fail:
                raise RuntimeError("GPU lost")
            if self.on_warm:
                self.on_warm()

    def setUp(self):
        patches = [mock.patch.object(vd, "KEEPWARM_SEC", 0.01),
                   mock.patch.object(vd, "log", lambda *a, **k: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.jobs = vd.queue.Queue()

    def test_a_queued_take_is_returned_without_warming(self):
        engine = self.Engine()
        self.jobs.put("take")
        self.assertEqual(vd.next_take(self.jobs, engine), "take")
        self.assertEqual(engine.warmed, 0)

    def test_an_idle_queue_warms_until_a_take_arrives(self):
        # The third pass "presses the key": the take queued then is handed over.
        engine = self.Engine()
        engine.on_warm = lambda: engine.warmed == 3 and self.jobs.put("take")
        self.assertEqual(vd.next_take(self.jobs, engine), "take")
        self.assertEqual(engine.warmed, 3)

    def test_an_engine_without_warm_just_waits(self):
        timer = vd.threading.Timer(0.05, self.jobs.put, ("take",))
        timer.start()
        self.assertEqual(vd.next_take(self.jobs, object()), "take")

    def test_a_failing_warm_up_is_tried_once_then_dropped(self):
        engine = self.Engine(fail=True)
        timer = vd.threading.Timer(0.1, self.jobs.put, ("take",))
        timer.start()
        self.assertEqual(vd.next_take(self.jobs, engine), "take")
        self.assertEqual(engine.warmed, 1)

    def test_zero_turns_it_off(self):
        engine = self.Engine()
        timer = vd.threading.Timer(0.05, self.jobs.put, ("take",))
        timer.start()
        with mock.patch.object(vd, "KEEPWARM_SEC", 0):
            self.assertEqual(vd.next_take(self.jobs, engine), "take")
        self.assertEqual(engine.warmed, 0)

    def test_a_slow_pass_is_logged(self):
        logged = []
        engine = self.Engine()
        clock = iter([100.0, 100.0 + vd.WARM_SLOW_SEC + 1])
        with mock.patch.object(vd, "log", lambda *a: logged.append(a)), \
             mock.patch.object(vd.time, "time", lambda: next(clock)):
            self.assertTrue(vd.keep_warm(engine))
        self.assertEqual(len(logged), 1)
        self.assertIn("paged out", logged[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
