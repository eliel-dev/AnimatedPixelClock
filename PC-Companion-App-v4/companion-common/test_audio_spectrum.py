"""Unit tests for audio spectrum streaming, auto-start, pacer, and DSP parity."""
import unittest
import threading
from contextlib import contextmanager
from unittest.mock import Mock, patch
import math

import audio_spectrum as audio


def trigger(threshold=-45.0, start=3.0, stop=20.0):
    t = audio.VizAutoTrigger()
    t.configure(True, threshold, start, stop)
    return t


def feed_span(t, level_db, t0, seconds, step=0.012):
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
    def test_live_audio_is_never_reopened(self):
        self.assertFalse(audio.capture_stale(0.0, 0.012, audio.SILENCE_REOPEN_S))

    def test_brief_quiet_passages_are_left_alone(self):
        self.assertFalse(audio.capture_stale(audio.SILENCE_REOPEN_S - 0.012, 0.012,
                                             audio.SILENCE_REOPEN_S))

    def test_long_silence_reopens_the_recorder(self):
        self.assertTrue(audio.capture_stale(audio.SILENCE_REOPEN_S, 0.012,
                                             audio.SILENCE_REOPEN_S))

    def test_backed_off_wait_is_respected(self):
        self.assertFalse(audio.capture_stale(audio.SILENCE_REOPEN_S, 0.012,
                                             audio.SILENCE_REOPEN_MAX_S))

    def test_resume_from_sleep_reopens_on_the_first_block(self):
        self.assertTrue(audio.capture_stale(0.0, 1800.0, audio.SILENCE_REOPEN_S))


@unittest.skipUnless(audio.AVAILABLE, "Audio dependencies unavailable")
class StreamTimingTests(unittest.TestCase):
    def setUp(self):
        self.stream = audio.SpectrumStreamer("pixelclock.local", 4210)
        self.stream._sock.close()
        self.stream._sock = Mock()
        self.legacy_bands = audio.np.zeros(audio.LEGACY_BANDS, dtype=audio.np.uint8)
        self.mica_bands = audio.np.zeros(audio.BANDS, dtype=audio.np.uint8)

    def test_capture_sends_only_to_cached_numeric_address(self):
        with patch.object(audio.socket, "getaddrinfo", side_effect=AssertionError("DNS in capture")):
            self.stream._send_bands(self.legacy_bands, self.mica_bands)
            self.stream._sock.sendto.assert_not_called()
            self.stream._target = ("192.0.2.1", 4210)
            self.stream._send_bands(self.legacy_bands, self.mica_bands)
        flat = bytes([128]) * audio.WAVE_POINTS
        expected_packet = (b"FFT3" + bytes(audio.LEGACY_BANDS)
                           + bytes(audio.BANDS) + flat)
        self.stream._sock.sendto.assert_called_once_with(
            expected_packet, ("192.0.2.1", 4210))

    def test_packet_fft3_carries_legacy_mica_and_wave(self):
        self.stream._target = ("192.0.2.1", 4210)
        legacy = audio.np.arange(audio.LEGACY_BANDS, dtype=audio.np.uint8)
        mica = audio.np.arange(audio.BANDS, dtype=audio.np.uint8)
        wave = audio.np.arange(audio.WAVE_POINTS, dtype=audio.np.uint8)
        self.stream._send_bands(legacy, mica, wave)
        packet = self.stream._sock.sendto.call_args[0][0]
        # 4 (magic) + 32 (legacy) + 128 (mica) + 128 (wave) = 292 bytes
        self.assertEqual(len(packet), 292)
        self.assertEqual(packet[:4], b"FFT3")
        self.assertEqual(packet[4:4 + audio.LEGACY_BANDS], legacy.tobytes())
        self.assertEqual(packet[4 + audio.LEGACY_BANDS:4 + audio.LEGACY_BANDS + audio.BANDS], mica.tobytes())
        self.assertEqual(packet[4 + audio.LEGACY_BANDS + audio.BANDS:], wave.tobytes())

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
        diffs = audio.np.diff(bins.astype(int))
        self.assertTrue(bool((diffs >= 0).all()))

    def test_waveform_triggers_on_a_rising_zero_crossing(self):
        t = audio.np.arange(audio.LEGACY_FRAMES, dtype=audio.np.float32) / audio.RATE
        first = audio.np.sin(2 * audio.np.pi * 110.0 * t).astype(audio.np.float32)
        shifted = audio.np.sin(2 * audio.np.pi * 110.0 * t + 1.1).astype(audio.np.float32)
        a = self.stream._process_wave(first)
        b = self.stream._process_wave(shifted)
        self.assertEqual(len(a), audio.WAVE_POINTS)
        self.assertLess(int(audio.np.abs(a.astype(int) - b.astype(int)).max()), 12)
        self.assertGreater(int(a.max()), 200)
        self.assertLess(int(a.min()), 55)

    def test_silence_stays_a_flat_trace(self):
        quiet = audio.np.zeros(audio.LEGACY_FRAMES, dtype=audio.np.float32)
        wave = self.stream._process_wave(quiet)
        self.assertTrue(bool((wave == 128).all()))


class LegacyAnalyzerTests(unittest.TestCase):
    """Verifies that the restored 32-band analyzer produces output identical to main."""

    @staticmethod
    def reference_main_process_block(mono, agc_ref):
        window = audio.np.hanning(1920).astype(audio.np.float32)
        edges = 50.0 * (16000.0 / 50.0) ** (audio.np.arange(33) / 32.0)
        bin_hz = 48000 / 2048.0
        band_bins = []
        for i in range(32):
            lo = int(edges[i] / bin_hz)
            hi = max(lo + 1, int(edges[i + 1] / bin_hz))
            band_bins.append((lo, hi))
        spec = audio.np.abs(audio.np.fft.rfft(mono * window, n=2048))
        amps = audio.np.empty(32, dtype=audio.np.float32)
        for i, (lo, hi) in enumerate(band_bins):
            amps[i] = audio.np.sqrt(audio.np.mean(spec[lo:hi] ** 2))
        db = 20.0 * audio.np.log10(amps + 1e-7)
        peak = float(db.max())
        dt = 1920 / 48000.0
        new_agc = max(agc_ref - 1.5 * dt, peak, -55.0)
        norm = (db - (new_agc - 38.0)) / 38.0
        return audio.np.clip(norm * 255.0, 0, 255).astype(audio.np.uint8), new_agc

    def test_legacy_bands_identical_to_main(self):
        stream = audio.SpectrumStreamer("127.0.0.1", 4210)
        stream._agc_ref = -40.0
        ref_agc = -40.0

        # Test with varied synthetic signals
        t = audio.np.arange(1920, dtype=audio.np.float32) / 48000.0
        tones = (
            0.5 * audio.np.sin(2 * audio.np.pi * 100.0 * t),
            0.3 * audio.np.sin(2 * audio.np.pi * 1000.0 * t) + 0.2 * audio.np.sin(2 * audio.np.pi * 4000.0 * t),
            audio.np.random.RandomState(42).uniform(-0.5, 0.5, 1920).astype(audio.np.float32)
        )

        for tone in tones:
            expected, ref_agc = self.reference_main_process_block(tone, ref_agc)
            actual = stream._process_legacy_bands(tone, dt=1920 / 48000.0)
            self.assertTrue(bool((actual == expected).all()),
                            "Restored legacy bands must match main exactly for the same 1920-sample input")
            self.assertAlmostEqual(stream._agc_ref, ref_agc, delta=1e-5)


class ColorVectorTests(unittest.TestCase):
    """Validates physical Rainbow palette mapping against Mica golden vectors (T3b)."""

    @staticmethod
    def sample_rainbow_mica(x):
        # Port of Mica's drawMirrorLinesVisual + samplePaletteColor(Rainbow) + rainbowColorForColumn
        # column = lroundf(clamp01(x / 127.0f) * 255.0f)
        # rainbowColorForColumn(column, 256) -> hue = (column * 255) / 255 = column
        t = max(0.0, min(1.0, float(x) / 127.0))
        hue = int(round(t * 255.0))
        region = hue // 43
        remainder = (hue - region * 43) * 6
        q = (255 - remainder) & 0xFF
        rem = remainder & 0xFF
        if region == 0:
            return (255, rem, 0)
        elif region == 1:
            return (q, 255, 0)
        elif region == 2:
            return (0, 255, rem)
        elif region == 3:
            return (0, q, 255)
        elif region == 4:
            return (rem, 0, 255)
        else:
            return (255, 0, q)

    def test_golden_vectors(self):
        vectors = {
            0: (255, 0, 0),
            32: (129, 255, 0),
            64: (0, 255, 255),
            96: (126, 0, 255),
            127: (255, 0, 15),
        }
        for x, expected in vectors.items():
            actual = self.sample_rainbow_mica(x)
            self.assertEqual(actual, expected, f"Column x={x} expected {expected} but got {actual}")


class MicaDspNumericalParityTests(unittest.TestCase):
    """Validates Python DSP against a verbatim line-by-line transcription of Mica C# (T4)."""

    class CSharpReferenceAnalyzer:
        """Line-by-line transcription of Mica's SpectrumAnalyzer + LedPayloadFactory + ToByte01."""

        def __init__(self):
            self.fft_size = 2048
            self.sample_rate = 48000
            self.hop_size = 256
            # FftUtility.BuildHannWindow (symmetric)
            self.hann_window = [0.5 * (1.0 - math.cos((2.0 * math.pi * i) / (self.fft_size - 1)))
                                for i in range(self.fft_size)]
            # B-weighting power multipliers
            self.weighting = self._build_b_weighting()
            # 42 Bark ranges (mode 0, viewport 1600)
            self.display_ranges = [(i, i + 1) for i in range(1, 43)]
            self.smoothed_power = None
            # EnvelopeSmoother: rise=0.82, fall=0.06, damping=0.30
            self.rise = 0.82
            self.fall = 0.06
            self.damping = 0.30
            self.target_state = [0.0] * 42
            self.smoothed_state = [0.0] * 42
            # Normalization thresholds
            self.amp_floor = 10.0 ** (-85.0 / 20.0)
            self.amp_ceil = 10.0 ** (-25.0 / 20.0)
            self.amp_range = self.amp_ceil - self.amp_floor
            self.linear_boost = 1.30

        def _build_b_weighting(self):
            mults = [1.0] * 1025
            c1 = 424.36
            c2 = 148693636.0
            for bin_idx in range(1, 1025):
                freq = bin_idx * self.sample_rate / float(self.fft_size)
                f2 = freq * freq
                denom = (f2 + c1) * math.sqrt(f2 + 25122.25) * (f2 + c2)
                if denom > 0:
                    val = (c2 * f2 * freq) / denom
                    db = 0.17 + 20.0 * math.log10(max(val, 1e-30))
                    amp_mult = 10.0 ** (db / 20.0)
                    mults[bin_idx] = max(1e-12, min(1e12, amp_mult * amp_mult))
            return mults

        def process_hop(self, window_2048):
            # Windowing & FFT power
            windowed = [window_2048[i] * self.hann_window[i] for i in range(2048)]
            fft_res = audio.np.fft.rfft(windowed, n=2048)
            power = [(fft_res[i].real ** 2 + fft_res[i].imag ** 2) / (2048.0 * 2048.0) for i in range(1025)]

            # FFT Smoothing (0.75 previous)
            if self.smoothed_power is None:
                self.smoothed_power = list(power)
            else:
                for i in range(1025):
                    self.smoothed_power[i] = (self.smoothed_power[i] * 0.75) + (power[i] * 0.25)

            # Weighting & Peak aggregation over 42 bands
            display_raw = [0.0] * 42
            for i, (lo, hi) in enumerate(self.display_ranges):
                peak_p = 0.0
                for b in range(lo, hi):
                    w_pow = self.smoothed_power[b] * self.weighting[b]
                    if w_pow > peak_p:
                        peak_p = w_pow
                amp = math.sqrt(peak_p)
                norm = max(0.0, min(1.0, (amp - self.amp_floor) / self.amp_range * self.linear_boost))
                display_raw[i] = norm

            # EnvelopeSmoother
            display_smooth = [0.0] * 42
            for i in range(42):
                inp = display_raw[i]
                target = self.target_state[i]
                speed = self.rise if inp > target else self.fall
                target += (inp - target) * speed
                self.target_state[i] = target
                smooth = self.smoothed_state[i]
                smooth += (target - smooth) * self.damping
                self.smoothed_state[i] = smooth
                display_smooth[i] = smooth

            # ResampleSpectrumBins (42 -> 128)
            bins128 = [0.0] * 128
            for idx in range(128):
                t = idx / 127.0
                scaled = t * 41.0
                left = int(math.floor(scaled))
                right = min(41, left + 1)
                blend = scaled - left
                bins128[idx] = (display_smooth[left] * (1.0 - blend)) + (display_smooth[right] * blend)

            # ToByte01
            return [max(0, min(255, int(round(max(0.0, min(1.0, v)) * 255.0)))) for v in bins128]

    def _evaluate_parity_on_signal(self, samples, signal_name):
        cs_ref = self.CSharpReferenceAnalyzer()
        py_streamer = audio.SpectrumStreamer("127.0.0.1", 4210)

        # Run 256-sample hops across the signal
        hop_count = (len(samples) - 2048) // 256
        self.assertGreater(hop_count, 10, f"Signal {signal_name} too short")

        total_bytes = 0
        matching_bytes = 0
        warmup_hops = 8  # Ignore initial filter transients

        for hop_idx in range(hop_count):
            start = hop_idx * 256
            window = samples[start:start + 2048]
            expected_bytes = cs_ref.process_hop(window)
            actual_bytes = list(py_streamer._analyse_window(audio.np.asarray(window, dtype=audio.np.float32)))

            if hop_idx >= warmup_hops:
                for b_idx in range(128):
                    total_bytes += 1
                    diff = abs(expected_bytes[b_idx] - actual_bytes[b_idx])
                    # Parity requirement: +/- 1 byte tolerance
                    if diff <= 1:
                        matching_bytes += 1

        parity_rate = matching_bytes / float(total_bytes)
        self.assertGreaterEqual(parity_rate, 0.99,
                                f"Signal '{signal_name}' DSP parity rate was {parity_rate*100:.2f}%, expected >= 99%")

    def test_dsp_parity_sine_sweep(self):
        # Sine sweep from 20 Hz to 1200 Hz over 2 seconds (96000 samples)
        t = audio.np.linspace(0.0, 2.0, 96000, dtype=audio.np.float32)
        # Chirp formula
        f0, f1 = 20.0, 1200.0
        phase = 2.0 * audio.np.pi * (f0 * t + 0.5 * (f1 - f0) / 2.0 * t * t)
        sweep = (0.7 * audio.np.sin(phase)).astype(audio.np.float32)
        self._evaluate_parity_on_signal(sweep, "Sine Sweep (20-1200 Hz)")

    def test_dsp_parity_pink_noise_bursts(self):
        # Voss-McCartney pink noise with 100ms bursts
        rng = audio.np.random.RandomState(12345)
        n = 48000  # 1 second
        white = rng.uniform(-0.5, 0.5, (16, n)).astype(audio.np.float32)
        # Approximate 1/f filter
        pink = audio.np.sum(white, axis=0) / 8.0
        # Create bursts (active 100ms, silence 50ms)
        envelope = (audio.np.sin(2 * audio.np.pi * 6.67 * audio.np.linspace(0, 1, n)) > 0).astype(audio.np.float32)
        bursts = (pink * envelope).astype(audio.np.float32)
        self._evaluate_parity_on_signal(bursts, "Pink Noise Bursts")

    def test_dsp_parity_synthetic_kick_loop(self):
        # 120 BPM kick loop (2 beats = 1 sec, 48000 samples)
        kick = audio.np.zeros(48000, dtype=audio.np.float32)
        beat_len = 24000  # 0.5s per kick
        for beat in (0, beat_len):
            t_beat = audio.np.linspace(0.0, 0.4, 19200, dtype=audio.np.float32)
            # Frequency drops from 160 Hz to 45 Hz
            freq = 45.0 + 115.0 * audio.np.exp(-30.0 * t_beat)
            phase = 2.0 * audio.np.pi * audio.np.cumsum(freq) / 48000.0
            amp_env = audio.np.exp(-12.0 * t_beat)
            kick[beat:beat + len(t_beat)] = (0.8 * amp_env * audio.np.sin(phase)).astype(audio.np.float32)
        self._evaluate_parity_on_signal(kick, "Synthetic Kick Loop")


class MicaDspTests(unittest.TestCase):
    def test_b_weighting_dc_bin_is_unity(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        self.assertAlmostEqual(float(m[0]), 1.0)

    def test_b_weighting_multipliers_length(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        self.assertEqual(len(m), 1025)

    def test_b_weighting_positive_values(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        self.assertTrue(bool((m > 0).all()))

    def test_b_weighting_boosts_midrange_over_infra(self):
        m = audio.build_b_weighting_multipliers(2048, 48000)
        self.assertGreater(float(m[43]), float(m[1]))

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

    def test_mode0_default_layout_has_one_range_per_usable_fft_bin(self):
        bands = audio.build_mode0_band_ranges(2048, 48000, 20, 1000, 1600)
        self.assertEqual(len(bands), 42)
        self.assertEqual(bands[0], (1, 2))
        self.assertEqual(bands[-1], (42, 43))

    def test_smoother_attack_is_fast(self):
        s = audio.EnvelopeSmoother(1, rise=0.82, fall=0.06, damping=0.30)
        spike = audio.np.array([1.0], dtype=audio.np.float32)
        out = s.process(spike)
        self.assertGreater(float(out[0]), 0.15)

    def test_smoother_decay_is_slow(self):
        s = audio.EnvelopeSmoother(1, rise=0.82, fall=0.06, damping=0.30)
        spike = audio.np.array([1.0], dtype=audio.np.float32)
        for _ in range(30):
            s.process(spike)
        silence = audio.np.array([0.0], dtype=audio.np.float32)
        after1 = float(s.process(silence)[0])
        self.assertGreater(after1, 0.5)

    def test_process_block_returns_both_legacy_and_mica(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        mono = audio.np.zeros(audio.FRAMES, dtype=audio.np.float32)
        legacy, mica = s._process_block(mono)
        self.assertEqual(len(legacy), 32)
        self.assertEqual(legacy.dtype, audio.np.uint8)
        self.assertEqual(len(mica), 128)
        self.assertEqual(mica.dtype, audio.np.uint8)

    def test_process_block_silence_is_zeros(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        mono = audio.np.zeros(audio.FRAMES, dtype=audio.np.float32)
        legacy, mica = s._process_block(mono)
        self.assertTrue(bool((mica == 0).all()))

    def test_process_block_loud_tone_nonzero(self):
        s = audio.SpectrumStreamer("127.0.0.1", 4210)
        t = audio.np.arange(audio.FRAMES, dtype=audio.np.float32) / audio.RATE
        mono = 0.5 * audio.np.sin(2 * audio.np.pi * 440.0 * t).astype(audio.np.float32)
        # Feed enough blocks to fill window (2048 samples = ~4 blocks of 576)
        for _ in range(4):
            s._process_block(mono)
        legacy, mica = s._process_block(mono)
        self.assertGreater(int(mica.max()), 0)
        self.assertGreater(int(legacy.max()), 0)


if __name__ == "__main__":
    unittest.main()
