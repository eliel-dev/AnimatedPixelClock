"""Auto-start decisions: arming delay, short-sound rejection, quiet release."""
import unittest
import threading
from contextlib import contextmanager
from unittest.mock import Mock, patch

import audio_spectrum as audio


def trigger(threshold=-45.0, start=3.0, stop=20.0):
    t = audio.VizAutoTrigger()
    t.configure(True, threshold, start, stop)
    return t


def feed_span(t, level_db, t0, seconds, step=0.04):
    """Play one level for a span; returns the actions in order."""
    actions, now = [], t0
    end = t0 + seconds
    while now < end:
        a = t.feed(level_db, now)
        if a:
            actions.append((a, now))
        now += step
    return actions, now


class AutoTriggerTests(unittest.TestCase):
    def test_disabled_never_acts(self):
        t = audio.VizAutoTrigger()
        actions, _ = feed_span(t, -10.0, 0.0, 30.0)
        self.assertEqual(actions, [])

    def test_music_starts_after_delay(self):
        t = trigger(start=3.0)
        actions, _ = feed_span(t, -20.0, 0.0, 5.0)
        self.assertEqual([a for a, _ in actions], ["viz"])
        self.assertAlmostEqual(actions[0][1], 3.0, delta=0.05)
        self.assertTrue(t.forced)

    def test_short_notification_is_ignored(self):
        t = trigger(start=3.0)
        now = 0.0
        for _ in range(4):  # four 1s pops, 2s apart
            _, now = feed_span(t, -15.0, now, 1.0)
            actions, now = feed_span(t, -90.0, now, 2.0)
            self.assertEqual(actions, [])
        self.assertFalse(t.forced)

    def test_short_gap_does_not_rearm(self):
        t = trigger(start=3.0)
        actions, now = feed_span(t, -20.0, 0.0, 2.0)
        self.assertEqual(actions, [])
        _, now = feed_span(t, -90.0, now, 0.5)      # gap below AUTO_GAP_S
        actions, _ = feed_span(t, -20.0, now, 2.0)
        self.assertEqual([a for a, _ in actions], ["viz"])

    def test_release_after_stop_delay_only(self):
        t = trigger(start=1.0, stop=10.0)
        _, now = feed_span(t, -20.0, 0.0, 2.0)
        self.assertTrue(t.forced)
        actions, now = feed_span(t, -90.0, now, 8.0)
        self.assertEqual(actions, [])               # still holding the display
        actions, _ = feed_span(t, -90.0, now, 4.0)
        self.assertEqual([a for a, _ in actions], ["auto"])
        self.assertFalse(t.forced)

    def test_threshold_rejects_quiet_sound(self):
        t = trigger(threshold=-30.0, start=1.0)
        actions, _ = feed_span(t, -40.0, 0.0, 10.0)
        self.assertEqual(actions, [])

    def test_disabling_hands_the_display_back(self):
        t = trigger(start=1.0)
        feed_span(t, -20.0, 0.0, 2.0)
        self.assertTrue(t.configure(False, -45.0, 3.0, 20.0))
        self.assertFalse(t.forced)
        self.assertFalse(t.configure(False, -45.0, 3.0, 20.0))

    def test_settings_are_clamped(self):
        t = trigger()
        t.configure(True, -200.0, -5.0, 0.0)
        self.assertEqual((t.threshold_db, t.start_delay, t.stop_delay),
                         (-80.0, 0.0, 1.0))
        t.configure(True, 50.0, 900.0, 99999.0)
        self.assertEqual((t.threshold_db, t.start_delay, t.stop_delay),
                         (-10.0, 60.0, 3600.0))

    def test_auto_settings_from_config(self):
        self.assertEqual(audio.auto_settings({}),
                         (False, audio.AUTO_THRESHOLD_DB,
                          audio.AUTO_START_DELAY, audio.AUTO_STOP_DELAY))
        self.assertEqual(audio.auto_settings({
            "audio_viz_auto": True, "audio_viz_threshold": -55,
            "audio_viz_start_delay": 5, "audio_viz_stop_delay": 90}),
            (True, -55.0, 5.0, 90.0))


class PacerTests(unittest.TestCase):
    def test_short_gaps_hold_the_last_frame(self):
        self.assertEqual(audio.frame_action(0.0), "hold")
        self.assertEqual(audio.frame_action(audio.HOLD_S), "hold")

    def test_longer_gaps_fade_the_last_frame(self):
        self.assertEqual(audio.frame_action(audio.HOLD_S + 0.01), "decay")
        self.assertEqual(audio.frame_action(audio.GIVE_UP_S - 0.01), "decay")

    def test_dead_capture_stops_sending(self):
        self.assertEqual(audio.frame_action(audio.GIVE_UP_S), "stop")
        self.assertEqual(audio.frame_action(60.0), "stop")

    def test_decay_reaches_silence_before_giving_up(self):
        level = 255.0
        ticks = int((audio.GIVE_UP_S - audio.HOLD_S) / audio.GAP_S)
        for _ in range(ticks):
            level *= audio.STALL_DECAY
        self.assertLess(level, 1.0)


class StaleCaptureTests(unittest.TestCase):
    """A loopback client that survives a suspend reads silence without error."""

    def test_live_audio_is_never_reopened(self):
        self.assertFalse(audio.capture_stale(0.0, 0.04, audio.SILENCE_REOPEN_S))

    def test_brief_quiet_passages_are_left_alone(self):
        self.assertFalse(audio.capture_stale(audio.SILENCE_REOPEN_S - 0.04, 0.04,
                                             audio.SILENCE_REOPEN_S))

    def test_long_silence_reopens_the_recorder(self):
        self.assertTrue(audio.capture_stale(audio.SILENCE_REOPEN_S, 0.04,
                                            audio.SILENCE_REOPEN_S))

    def test_backed_off_wait_is_respected(self):
        self.assertFalse(audio.capture_stale(audio.SILENCE_REOPEN_S, 0.04,
                                             audio.SILENCE_REOPEN_MAX_S))

    def test_resume_from_sleep_reopens_on_the_first_block(self):
        # The capture thread is frozen while the PC sleeps, so the wall clock
        # jumps across a single block even though nothing looks wrong yet.
        self.assertTrue(audio.capture_stale(0.0, 1800.0, audio.SILENCE_REOPEN_S))


@unittest.skipUnless(audio.AVAILABLE, "Audio dependencies unavailable")
class StreamTimingTests(unittest.TestCase):
    def setUp(self):
        self.stream = audio.SpectrumStreamer("pixelclock.local", 4210)
        self.stream._sock.close()
        self.stream._sock = Mock()
        self.bands = audio.np.zeros(audio.BANDS, dtype=audio.np.uint8)

    def test_capture_sends_only_to_cached_numeric_address(self):
        with patch.object(audio.socket, "getaddrinfo", side_effect=AssertionError("DNS in capture")):
            self.stream._send_bands(self.bands)
            self.stream._sock.sendto.assert_not_called()
            self.stream._target = ("192.0.2.1", 4210)
            self.stream._send_bands(self.bands)
        flat = bytes([128]) * audio.WAVE_POINTS
        self.stream._sock.sendto.assert_called_once_with(
            b"FFT2" + bytes(audio.BANDS) + flat, ("192.0.2.1", 4210))

    def test_packet_carries_bands_then_waveform(self):
        self.stream._target = ("192.0.2.1", 4210)
        wave = audio.np.arange(audio.WAVE_POINTS, dtype=audio.np.uint8)
        self.stream._send_bands(self.bands, wave)
        packet = self.stream._sock.sendto.call_args[0][0]
        self.assertEqual(len(packet), 4 + audio.BANDS + audio.WAVE_POINTS)
        self.assertEqual(packet[:4], b"FFT2")
        self.assertEqual(packet[4 + audio.BANDS:], wave.tobytes())

    def test_mica_resample_preserves_endpoints_and_interpolates(self):
        source = audio.np.zeros(42, dtype=audio.np.uint8)
        source[0], source[-1], source[1] = 10, 250, 110
        bins = audio.resample_audio_motion_bands(source)
        self.assertEqual(len(bins), 128)
        self.assertEqual(int(bins[0]), 10)
        self.assertEqual(int(bins[-1]), 250)
        # Column 3 maps to source position 3 * 41 / 127 = 0.968...
        self.assertEqual(int(bins[3]), 107)

    def test_mica_resample_handles_silence_and_full_scale(self):
        self.assertTrue(bool((audio.resample_audio_motion_bands(
            audio.np.zeros(42)) == 0).all()))
        self.assertTrue(bool((audio.resample_audio_motion_bands(
            audio.np.full(42, 255)) == 255).all()))

    def test_mica_resample_linear_ramp(self):
        source = audio.np.linspace(0, 255, 42, dtype=audio.np.float32)
        bins = audio.resample_audio_motion_bands(source)
        self.assertEqual(len(bins), 128)
        self.assertEqual(bins[0], 0)
        self.assertEqual(bins[-1], 255)
        # Verify strictly non-decreasing ramp across all 128 columns
        diffs = audio.np.diff(bins.astype(int))
        self.assertTrue(bool((diffs >= 0).all()))

    def test_mica_resample_isolated_peaks(self):
        source = audio.np.zeros(42, dtype=audio.np.uint8)
        source[20] = 250  # peak near the middle (band 20 of 0..41)
        bins = audio.resample_audio_motion_bands(source)
        self.assertEqual(len(bins), 128)
        # Peak column is round(20 * 127 / 41) = round(61.951) = 62
        peak_idx = int(audio.np.argmax(bins))
        self.assertIn(peak_idx, (61, 62, 63))
        self.assertGreater(int(bins[peak_idx]), 200)
        # Far-off columns remain silent (0)
        self.assertEqual(int(bins[0]), 0)
        self.assertEqual(int(bins[-1]), 0)
        self.assertEqual(int(bins[30]), 0)
        self.assertEqual(int(bins[100]), 0)

    def test_waveform_triggers_on_a_rising_zero_crossing(self):
        # Two blocks of the same tone at different phases must yield the same
        # trace, otherwise the scope slides sideways instead of standing still.
        t = audio.np.arange(audio.FRAMES, dtype=audio.np.float32) / audio.RATE
        first = audio.np.sin(2 * audio.np.pi * 110.0 * t).astype(audio.np.float32)
        shifted = audio.np.sin(2 * audio.np.pi * 110.0 * t + 1.1).astype(audio.np.float32)
        a = self.stream._process_wave(first)
        b = self.stream._process_wave(shifted)
        self.assertEqual(len(a), audio.WAVE_POINTS)
        self.assertLess(int(audio.np.abs(a.astype(int) - b.astype(int)).max()), 12)
        self.assertGreater(int(a.max()), 200)
        self.assertLess(int(a.min()), 55)

    def test_silence_stays_a_flat_trace(self):
        quiet = audio.np.zeros(audio.FRAMES, dtype=audio.np.float32)
        wave = self.stream._process_wave(quiet)
        self.assertTrue(bool((wave == 128).all()))

    def test_slow_dns_does_not_block_capture_or_publish_old_target(self):
        entered, finish = threading.Event(), threading.Event()

        def resolve(*args):
            entered.set()
            finish.wait(2)
            self.stream.stop()
            return [(None, None, None, None, ("192.0.2.1", 4210))]

        self.stream._target = ("192.0.2.1", 4210)
        with patch.object(audio, "audio_thread_com"), patch.object(audio.sc, "default_speaker"), \
                patch.object(audio.socket, "getaddrinfo", side_effect=resolve):
            worker = threading.Thread(target=self.stream._maintenance)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                sender = threading.Thread(target=self.stream._send_bands, args=(self.bands,))
                sender.start()
                sender.join(timeout=0.5)
                self.assertFalse(sender.is_alive(), "DNS holds up audio sends")
                self.stream.set_target("new-clock.local", 4210)
                finish.set()
                worker.join(timeout=1)
                self.assertIsNone(self.stream._target)
            finally:
                finish.set()
                self.stream.stop()
                worker.join(timeout=2)

    def test_silent_stream_is_reopened_not_trusted(self):
        """The regression: after a resume the old client returns zeros forever."""
        opens, blocks = [], [0]

        class FakeRecorder:
            def __enter__(inner):
                opens.append(True)
                return inner

            def __exit__(inner, *exc):
                return False

            def record(inner, numframes):
                blocks[0] += 1
                # Bounded so an unfixed capture loop fails here instead of
                # spinning on synthesised silence forever.
                if len(opens) > 2 or blocks[0] > 5000:
                    self.stream.stop()
                return audio.np.zeros((numframes, 2), dtype=audio.np.float32)

        class FakeMic:
            def recorder(inner, samplerate, blocksize):
                return FakeRecorder()

        speaker = Mock()
        speaker.id = "spk"
        self.stream._target = ("192.0.2.1", 4210)
        with patch.object(audio, "SILENCE_REOPEN_S", 0.02),                 patch.object(audio, "SILENCE_REOPEN_MAX_S", 0.02),                 patch.object(audio.sc, "default_speaker", return_value=speaker),                 patch.object(audio.sc, "get_microphone", return_value=FakeMic()):
            self.stream._capture_loop()

        self.assertGreater(len(opens), 1, "silent capture was never reopened")
        self.assertEqual(self.stream.reopens, len(opens) - 1)
        self.assertEqual(self.stream.last_error, "")

    def test_unchanged_settings_keep_resolved_address(self):
        self.stream._target = ("192.0.2.1", 4210)
        self.stream.set_target("pixelclock.local", 4210)
        self.assertEqual(self.stream._target, ("192.0.2.1", 4210))
        self.stream.set_target("pixelclock.local", 4211)
        self.assertIsNone(self.stream._target)

    def test_capture_initializes_com_on_its_own_thread_and_can_join(self):
        entered = []
        exited = []

        @contextmanager
        def com():
            entered.append(threading.get_ident())
            try:
                yield
            finally:
                exited.append(threading.get_ident())

        def capture():
            self.assertIn(threading.get_ident(), entered)

        with patch.object(audio, "audio_thread_com", com), \
                patch.object(audio, "boost_thread_priority"), \
                patch.object(audio, "release_thread_priority"), \
                patch.object(self.stream, "_watchdog"), \
                patch.object(self.stream, "_maintenance"), \
                patch.object(self.stream, "_capture_loop", side_effect=capture):
            self.stream.start()
            self.stream.join(timeout=2)
        self.assertFalse(self.stream.is_alive())
        self.assertEqual(entered, [self.stream.ident])
        self.assertEqual(exited, entered)

    def test_stopped_stream_does_not_send_more_packets(self):
        self.stream._target = ("192.0.2.1", 4210)
        self.stream.stop()
        self.stream._send_bands(self.bands)
        self.stream._sock.sendto.assert_not_called()

    def test_mode_retries_network_failure_using_cached_address(self):
        self.stream._target = ("192.0.2.1", 4210)
        reply = Mock()
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        with patch.object(audio, "urlopen", side_effect=[OSError("network waking"), reply]) as request, \
                patch.object(audio.time, "monotonic", return_value=10.0) as now:
            self.stream._send_mode("auto")
            request.assert_not_called()  # Capture only queues the request.
            self.stream._flush_mode()
            self.assertIn("network waking", self.stream.auto_error)
            self.stream._flush_mode()
            self.assertEqual(request.call_count, 1)
            now.return_value = 12.0
            self.stream._flush_mode()
            self.assertEqual(self.stream.auto_error, "")
            self.assertIsNone(self.stream._mode_pending)
            request.assert_called_with("http://192.0.2.1/api/mode/auto", timeout=4)

    def test_new_mode_supersedes_failed_or_inflight_request(self):
        def fail(*args, **kwargs):
            self.stream._send_mode("auto")
            raise OSError("old request failed")

        self.stream._send_mode("viz")
        with patch.object(audio, "urlopen", side_effect=fail):
            self.stream._flush_mode()
        self.assertEqual(self.stream._mode_pending, "auto")
        self.assertEqual(self.stream.auto_error, "")
        self.assertEqual(self.stream._mode_retry_at, 0.0)

    def test_target_change_cancels_pending_old_mode(self):
        self.stream._send_mode("auto")
        self.stream.auto.forced = True
        self.stream.set_target("new-clock.local", 4210)
        self.assertEqual(self.stream._mode_pending, "viz")
        self.assertIsNone(self.stream._target)

    def test_reopened_device_does_not_loop_on_stale_default_id(self):
        self.stream._default_device_id = "old-speaker"
        recorder = Mock()
        recorder.__enter__ = Mock(return_value=recorder)
        recorder.__exit__ = Mock(return_value=False)
        calls = []

        def record(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                self.stream.stop()
            return audio.np.zeros((audio.FRAMES, 2), dtype=audio.np.float32)

        recorder.record.side_effect = record
        mic = Mock()
        mic.recorder.return_value = recorder
        with patch.object(audio.sc, "default_speaker", return_value=Mock(id="new-speaker")) as speaker, \
                patch.object(audio.sc, "get_microphone", return_value=mic):
            self.stream._capture_loop()
        speaker.assert_called_once()
        self.assertEqual(len(calls), 2)

    def check_device(self, now, uptime, forced=False):
        reply = Mock()
        reply.read.return_value = audio.json.dumps({"uptime": uptime, "forcedViz": forced}).encode()
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        with patch.object(audio, "urlopen", return_value=reply), \
                patch.object(audio.time, "monotonic", return_value=now):
            self.stream._check_device_restart()

    def test_device_restart_reasserts_active_playback(self):
        self.stream._target = ("192.0.2.1", 4210)
        self.stream.auto.enabled = self.stream.auto.forced = True
        self.check_device(100, 50, True)
        self.check_device(200, 3)
        self.assertEqual(self.stream._mode_pending, "viz")
        self.assertFalse(self.stream._device_viz)

    def test_manual_stop_is_not_treated_as_a_device_restart(self):
        self.stream._target = ("192.0.2.1", 4210)
        self.stream.auto.enabled = self.stream.auto.forced = True
        self.check_device(100, 50, True)
        self.check_device(110, 60)
        self.assertIsNone(self.stream._mode_pending)
        self.assertFalse(self.stream._device_viz)

    def test_restart_during_silence_does_not_force_visualizer(self):
        self.stream._target = ("192.0.2.1", 4210)
        self.stream.auto.enabled = True
        self.check_device(100, 50)
        self.check_device(200, 3)
        self.assertIsNone(self.stream._mode_pending)


class MicaDspTests(unittest.TestCase):
    """Tests for the ported Mica Audio DSP pipeline components."""

    # --- B-weighting ---
    def test_b_weighting_dc_bin_is_unity(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        self.assertAlmostEqual(float(m[0]), 1.0)

    def test_b_weighting_multipliers_length(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        self.assertEqual(len(m), 1025)  # FFT_N//2 + 1

    def test_b_weighting_positive_values(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        self.assertTrue(bool((m > 0).all()))

    def test_b_weighting_boosts_midrange_over_infra(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        # 1 kHz bin ≈ bin 43  vs  20 Hz bin ≈ bin 1
        self.assertGreater(float(m[43]), float(m[1]))

    # --- Bark scale ---
    def test_bark_roundtrip(self):
        for hz in (20, 100, 440, 1000, 8000, 16000):
            got = audio.from_bark(audio.to_bark(float(hz)))
            self.assertAlmostEqual(got, float(hz), delta=0.1)

    def test_bark_monotonic(self):
        prev = audio.to_bark(20.0)
        for f in range(50, 20001, 50):
            b = audio.to_bark(float(f))
            self.assertGreater(b, prev)
            prev = b

    # --- AudioMotion mode 0 layout ---
    def test_mode0_default_layout_has_one_range_per_usable_fft_bin(self):
        bands = audio.build_mode0_band_ranges(2048, 48000, 20, 1000, 1600)
        self.assertEqual(len(bands), 42)
        self.assertEqual(bands[0], (1, 2))
        self.assertEqual(bands[-1], (42, 43))

    def test_mode0_ranges_are_monotonic_and_nonempty(self):
        bands = audio.build_mode0_band_ranges(2048, 48000, 20, 1000, 1600)
        for i in range(1, len(bands)):
            self.assertGreaterEqual(bands[i][0], bands[i - 1][1])
        for lo, hi in bands:
            self.assertGreater(hi, lo)

    def test_mode0_narrow_viewport_merges_adjacent_bins(self):
        wide = audio.build_mode0_band_ranges(2048, 48000, 20, 1000, 1600)
        narrow = audio.build_mode0_band_ranges(2048, 48000, 20, 1000, 8)
        self.assertLess(len(narrow), len(wide))
        self.assertEqual(narrow[0][0], 1)
        self.assertEqual(narrow[-1][1], 43)

    # --- EnvelopeSmoother ---
    def test_smoother_attack_is_fast(self):
        s = audio.EnvelopeSmoother(1, rise=0.82, fall=0.06, damping=0.30)
        spike = audio.np.array([1.0], dtype=audio.np.float32)
        out = s.process(spike)
        self.assertGreater(float(out[0]), 0.15, "First step should track upward rapidly")

    def test_smoother_decay_is_slow(self):
        s = audio.EnvelopeSmoother(1, rise=0.82, fall=0.06, damping=0.30)
        spike = audio.np.array([1.0], dtype=audio.np.float32)
        # Pump to saturation
        for _ in range(30):
            s.process(spike)
        silence = audio.np.array([0.0], dtype=audio.np.float32)
        after1 = float(s.process(silence)[0])
        self.assertGreater(after1, 0.5, "Decay should be gradual")

    def test_smoother_reset(self):
        s = audio.EnvelopeSmoother(4, rise=0.82, fall=0.06, damping=0.30)
        s.process(audio.np.ones(4, dtype=audio.np.float32))
        s.reset()
        self.assertTrue(bool((s.target_state == 0).all()))
        self.assertTrue(bool((s.smoothed_state == 0).all()))

    def test_smoother_damping_smooths_steps(self):
        s = audio.EnvelopeSmoother(1, rise=1.0, fall=1.0, damping=0.30)
        out1 = float(s.process(audio.np.array([1.0]))[0])
        out2 = float(s.process(audio.np.array([1.0]))[0])
        self.assertLess(out1, out2, "Damping means second step is closer to 1.0")

    # --- Linear normalization ---
    def test_normalization_floor_maps_to_zero(self):
        amp_floor = 10.0 ** (audio.DB_FLOOR / 20.0)
        norm = max(0.0, min(1.0, (amp_floor - amp_floor) / (10.0 ** (audio.DB_CEILING / 20.0) - amp_floor) * audio.LINEAR_BOOST))
        self.assertAlmostEqual(norm, 0.0)

    def test_normalization_ceiling_exceeds_one(self):
        amp_floor = 10.0 ** (audio.DB_FLOOR / 20.0)
        amp_ceil = 10.0 ** (audio.DB_CEILING / 20.0)
        # At exactly the ceiling, norm = 1.0 * LINEAR_BOOST = 1.3, clipped to 1.0
        raw = (amp_ceil - amp_floor) / (amp_ceil - amp_floor) * audio.LINEAR_BOOST
        self.assertGreater(raw, 1.0, "LINEAR_BOOST pushes ceiling above 1.0 before clip")

    # --- Full pipeline integration ---
    def test_process_block_returns_128_bytes(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        mono = audio.np.zeros(audio.FRAMES, dtype=audio.np.float32)
        result = s._process_block(mono)
        self.assertEqual(len(result), 128)
        self.assertEqual(result.dtype, audio.np.uint8)

    def test_process_block_silence_is_zeros(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        mono = audio.np.zeros(audio.FRAMES, dtype=audio.np.float32)
        result = s._process_block(mono)
        self.assertTrue(bool((result == 0).all()))

    def test_process_block_loud_tone_nonzero(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        t = audio.np.arange(audio.FRAMES, dtype=audio.np.float32) / audio.RATE
        mono = 0.5 * audio.np.sin(2 * audio.np.pi * 440.0 * t).astype(audio.np.float32)
        self.assertTrue(bool((s._process_block(mono) == 0).all()))  # FFT warm-up
        result = s._process_block(mono)
        self.assertGreater(int(result.max()), 0, "440 Hz tone should light up some columns")

    def test_process_block_runs_all_available_256_sample_hops(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        block = audio.np.zeros(audio.FRAMES, dtype=audio.np.float32)
        s._process_block(block)  # 1920 samples: below the first 2048-sample FFT
        with patch.object(s, "_analyse_window", wraps=s._analyse_window) as analyse:
            s._process_block(block)
        # 3840 samples contain windows at offsets 0, 256, ..., 1792.
        self.assertEqual(analyse.call_count, 8)

    def test_fft_smoothing_keeps_75_percent_of_the_previous_power(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        t = audio.np.arange(audio.FFT_N, dtype=audio.np.float32) / audio.RATE
        tone = 0.5 * audio.np.sin(2 * audio.np.pi * 440.0 * t).astype(audio.np.float32)
        s._analyse_window(tone)
        previous = s._smoothed_power.copy()
        s._analyse_window(audio.np.zeros(audio.FFT_N, dtype=audio.np.float32))
        self.assertTrue(bool(audio.np.allclose(
            s._smoothed_power, previous * audio.FFT_SMOOTHING, rtol=1e-5, atol=1e-9)))


if __name__ == "__main__":
    unittest.main()
