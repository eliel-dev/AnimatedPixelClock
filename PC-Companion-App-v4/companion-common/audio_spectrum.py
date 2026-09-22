"""Audio spectrum streamer for the display's visualizer mode.

Captures what the PC is playing (WASAPI loopback on Windows, PulseAudio
monitor on Linux - both via the `soundcard` package), runs both the legacy
32-band logarithmic analyzer with AGC (for styles 0, 1, 2, 3, 5, 7) and
Mica Audio's AudioMotion mode-0 analyzer with Bark scaling and envelope
smoothing (for style 8 AudioMotion Clone). It sends a binary UDP packet:
b"FFT3" + 32 legacy amplitude bytes + 128 AudioMotion column bytes + 128
waveform bytes (292 bytes total) at ~83 packets/s (12 ms blocks) to the same
port the stats JSON uses.

The waveform tail is what the device's oscilloscope style draws; firmware
that predates FFT3 accepts legacy FFT1 and FFT2 packets for backward
compatibility. The device only displays the visualizer in its forced visualizer
mode (/api/mode/viz), so streaming is harmless otherwise.

With auto-start enabled the streamer also switches the display itself: it
watches block loudness and calls /api/mode/viz once sound has been playing
for the arming delay (so a notification pop cannot trigger it), then
/api/mode/auto after the configured quiet period.

Optional feature: `soundcard` and `numpy` may be missing, in which case
AVAILABLE is False and ensure() is a no-op (the web UI explains how to
install them). Everything is managed through ensure(config), which the
server calls after any config change and app_window calls at startup.
"""

import json
import socket
import sys
from contextlib import contextmanager
import threading
import time
from urllib.request import urlopen

try:
    import numpy as np
    import soundcard as sc
    AVAILABLE = True
    _IMPORT_ERROR = ""
except Exception as e:  # missing package OR no audio backend on this system
    np = None
    sc = None
    AVAILABLE = False
    _IMPORT_ERROR = str(e)

RATE = 48000
FRAMES = 576             # 12 ms capture blocks -> ~83.3 packets/s
LEGACY_FRAMES = 1920     # 40 ms rolling window for legacy 32-band FFT
FFT_N = 2048
HOP_N = 256              # Mica's SpectrumSampleWindow advance size

# Legacy 32-band analyzer calibration constants (matching main)
LEGACY_BANDS = 32
LEGACY_F_LO, LEGACY_F_HI = 50.0, 16000.0
AGC_RANGE_DB = 38.0      # dB span mapped onto 0..255
AGC_DECAY_DB_PER_S = 1.5
AGC_FLOOR_DB = -55.0

# Mica Audio DSP pipeline calibration constants (matching audiomotion-clone preset)
MODE0_VIEWPORT_WIDTH = 1600.0
BANDS = 128              # Physical HUB75 columns for style 8
MICA_BANDS = 128
F_LO, F_HI = 20.0, 1000.0
DB_FLOOR = -85.0
DB_CEILING = -25.0
LINEAR_BOOST = 1.30
FFT_SMOOTHING = 0.75
SMOOTHER_RISE = 0.82
SMOOTHER_FALL = 0.06
SMOOTHER_DAMPING = 0.30

# UDP packet magics
FFT1_MAGIC = b"FFT1"
FFT2_MAGIC = b"FFT2"
FFT3_MAGIC = b"FFT3"

# Waveform tail for oscilloscope style.
WAVE_POINTS = 128
WAVE_DECIM = 8
WAVE_AGC_DECAY = 0.55    # per second, multiplicative
WAVE_AGC_FLOOR = 0.02    # silence stays a flat line instead of amplified noise
WAVE_GAIN = 118.0        # peak deflection, leaving headroom inside +/-128

# Auto-start defaults (dBFS / seconds).
AUTO_THRESHOLD_DB = -45.0
AUTO_START_DELAY = 3.0
AUTO_STOP_DELAY = 20.0
AUTO_GAP_S = 1.0         # quiet gaps shorter than this do not re-arm
SILENT_DB = -120.0

# Watchdog timings scaled for 12 ms capture cadence (~83 pps).
GAP_S = 0.04             # no packet for 40ms (~3.3 blocks): watchdog steps in
HOLD_S = 0.25            # repeat the last frame 250ms before fading it
STALL_DECAY = 0.90       # per watchdog packet once fading
GIVE_UP_S = 6.0          # capture dead this long: stop sending, let device say so
RETRY_WAITS = (0.25, 0.5, 1.0, 3.0)

SILENCE_REOPEN_S = 10.0
SILENCE_REOPEN_MAX_S = 60.0
RESUME_JUMP_S = 5.0


def capture_stale(silence_s, wall_gap_s, reopen_after_s):
    """Whether the loopback client must be reopened rather than trusted."""
    return wall_gap_s >= RESUME_JUMP_S or silence_s >= reopen_after_s


def frame_action(age_s):
    """What the pacer does when no new frame is queued, by age of the last one."""
    if age_s <= HOLD_S:
        return "hold"
    if age_s < GIVE_UP_S:
        return "decay"
    return "stop"


def resample_audio_motion_bands(source):
    """Mica's endpoint-preserving linear 42->128 spectrum resample."""
    source = np.asarray(source, dtype=np.float32)
    if source.size == 0:
        return np.zeros(BANDS, dtype=np.uint8)
    if source.size == BANDS:
        return np.clip(np.rint(source), 0, 255).astype(np.uint8)
    positions = np.linspace(0.0, float(source.size - 1), BANDS,
                            dtype=np.float32)
    left = positions.astype(np.intp)
    right = np.minimum(left + 1, source.size - 1)
    blend = positions - left
    values = source[left] * (1.0 - blend) + source[right] * blend
    # Mica's ESP transport rounds normalized values to the nearest byte.
    return np.clip(np.rint(values), 0, 255).astype(np.uint8)


def build_b_weighting_multipliers(fft_size, sample_rate):
    """B-weighting acoustic curve power multipliers per FFT bin (matching Mica Audio)."""
    if np is None:
        return None
    half = fft_size // 2
    multipliers = np.ones(half + 1, dtype=np.float32)
    c1 = 424.36
    c2 = 148693636.0
    for bin_idx in range(1, half + 1):
        freq = float(bin_idx) * sample_rate / fft_size
        f2 = freq * freq
        denom = (f2 + c1) * np.sqrt(f2 + 25122.25) * (f2 + c2)
        if denom > 0:
            val = (c2 * f2 * freq) / denom
            db = 0.17 + 20.0 * np.log10(max(val, 1e-30))
            amp_mult = 10.0 ** (db / 20.0)
            power_mult = amp_mult * amp_mult
            multipliers[bin_idx] = float(np.clip(power_mult, 1e-12, 1e12))
    return multipliers


def to_bark(hz):
    """Converts frequency in Hz to Bark psychoacoustic scale (matching Mica Audio)."""
    return ((26.81 * hz) / (1960.0 + hz)) - 0.53


def from_bark(val):
    """Converts Bark scale value back to frequency in Hz (matching Mica Audio)."""
    denom = max(1e-6, (26.81 / (val + 0.53)) - 1.0)
    return 1960.0 / denom


def build_mode0_band_ranges(fft_size, sample_rate, min_hz=F_LO, max_hz=F_HI,
                            viewport_width=MODE0_VIEWPORT_WIDTH):
    """Mica ``LogBandMapper.CreateMode0Ranges`` for its Bark-scale preset.

    Mode 0 maps FFT bins directly to pixel x positions. Consecutive bins that
    land on the same pixel are aggregated into a single display range; at the
    reference width and frequency range each usable bin gets its own range.
    For 20..1000 Hz at 48 kHz / 2048 FFT this gives 42 distinct ranges (bins 1..42).
    """
    if np is None:
        return []
    width = max(1.0, float(viewport_width))
    width_int = max(1, int(np.rint(width)))
    max_analyser_bin = (fft_size // 2) - 1
    if max_analyser_bin < 1 or max_hz <= min_hz or min_hz <= 0.0:
        return []

    edge_inset = 1 if width_int > 2 else 0
    x_min = edge_inset
    x_max = max(x_min, width_int - 1 - edge_inset)
    min_scale = to_bark(min_hz)
    max_scale = to_bark(max_hz)
    unit_width = width / max(1e-6, max_scale - min_scale)
    min_bin = int(np.clip(np.floor(min_hz * fft_size / sample_rate),
                          1, max_analyser_bin))
    max_bin_exclusive = int(np.clip(np.ceil(max_hz * fft_size / sample_rate),
                                    min_bin + 1, max_analyser_bin + 1))
    ranges = []
    previous_x = None
    for bin_index in range(min_bin, max_bin_exclusive):
        frequency = max(1.0, (bin_index + 0.5) * sample_rate / float(fft_size))
        x = int(np.rint((to_bark(frequency) - min_scale) * unit_width))
        x = int(np.clip(x, x_min, x_max))
        end_exclusive = min(max_analyser_bin + 1, bin_index + 1)
        if previous_x is None or x > previous_x:
            ranges.append((bin_index, end_exclusive))
            previous_x = x
        else:
            start, _ = ranges[-1]
            ranges[-1] = (start, end_exclusive)

    return ranges


class EnvelopeSmoother:
    """Two-stage attack/decay smoother with second-order motion damping (matching Mica Audio)."""

    def __init__(self, size, rise=SMOOTHER_RISE, fall=SMOOTHER_FALL, damping=SMOOTHER_DAMPING):
        self.rise = float(np.clip(rise, 0.0, 1.0)) if np else rise
        self.fall = float(np.clip(fall, 0.0, 1.0)) if np else fall
        self.damping = float(np.clip(damping, 0.0, 1.0)) if np else damping
        self.target_state = np.zeros(size, dtype=np.float32) if np else []
        self.smoothed_state = np.zeros(size, dtype=np.float32) if np else []

    def reset(self):
        if np and isinstance(self.target_state, np.ndarray):
            self.target_state.fill(0.0)
            self.smoothed_state.fill(0.0)

    def process(self, input_values):
        if np is None:
            return input_values
        input_values = np.asarray(input_values, dtype=np.float32)
        if input_values.shape != self.target_state.shape:
            self.target_state = np.zeros_like(input_values)
            self.smoothed_state = np.zeros_like(input_values)

        # Vectorized attack / decay:
        speed = np.where(input_values > self.target_state, self.rise, self.fall)
        self.target_state += (input_values - self.target_state) * speed

        # Second-order motion damping:
        self.smoothed_state += (self.target_state - self.smoothed_state) * self.damping
        return self.smoothed_state.copy()


def boost_thread_priority():
    """Windows: put this thread on MMCSS "Pro Audio" scheduling and lift its
    priority, the same treatment media players give their audio threads."""
    handle = None
    try:
        import ctypes
        from ctypes import wintypes
        avrt = ctypes.WinDLL("avrt")
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR,
                                                       ctypes.POINTER(wintypes.DWORD)]
        task_index = wintypes.DWORD(0)
        handle = avrt.AvSetMmThreadCharacteristicsW("Pro Audio",
                                                    ctypes.byref(task_index)) or None
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThread.restype = wintypes.HANDLE
        kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2)  # HIGHEST
    except Exception:
        pass
    return handle


def boost_process_priority():
    """Windows: ABOVE_NORMAL for the app while it is streaming."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00008000)
    except Exception:
        pass


@contextmanager
def audio_thread_com():
    """COM belongs to the calling thread, not the SoundCard import thread."""
    ole32 = None
    initialized = False
    if sys.platform == "win32":
        import ctypes
        ole32 = ctypes.OleDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        try:
            result = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
            initialized = result in (0, 1)  # S_OK and S_FALSE both need balancing
        except OSError as error:
            if (error.winerror & 0xffffffff) != 0x80010106:
                raise
    try:
        yield
    finally:
        if initialized:
            ole32.CoUninitialize()


def release_thread_priority(handle):
    if handle:
        import ctypes
        from ctypes import wintypes
        avrt = ctypes.WinDLL("avrt")
        avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
        avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
        avrt.AvRevertMmThreadCharacteristics(handle)


def clamp_auto(threshold_db, start_delay, stop_delay):
    """Clamp auto-start settings to the ranges the UI offers."""
    return (min(-10.0, max(-80.0, float(threshold_db))),
            min(60.0, max(0.0, float(start_delay))),
            min(3600.0, max(1.0, float(stop_delay))))


class VizAutoTrigger:
    """Turns a stream of block levels into force/release decisions.

    No numpy/soundcard use, so it stays testable on any machine."""

    def __init__(self):
        self.enabled = False
        self.threshold_db = AUTO_THRESHOLD_DB
        self.start_delay = AUTO_START_DELAY
        self.stop_delay = AUTO_STOP_DELAY
        self.forced = False      # True while we hold the display in viz mode
        self._loud_since = None
        self._last_loud = None

    def configure(self, enabled, threshold_db, start_delay, stop_delay):
        """Apply settings. True when the display must be handed back."""
        self.enabled = bool(enabled)
        self.threshold_db, self.start_delay, self.stop_delay = clamp_auto(
            threshold_db, start_delay, stop_delay)
        if not self.enabled:
            return self.release()
        return False

    def feed(self, level_db, now):
        """Consume one block level. Returns 'viz', 'auto' or None."""
        if not self.enabled:
            return None
        if level_db >= self.threshold_db:
            if self._last_loud is None or (now - self._last_loud) > AUTO_GAP_S:
                self._loud_since = now
            self._last_loud = now
            if not self.forced and (now - self._loud_since) >= self.start_delay:
                self.forced = True
                return "viz"
            return None
        if self.forced and self._last_loud is not None and \
                (now - self._last_loud) >= self.stop_delay:
            self.forced = False
            self._loud_since = None
            return "auto"
        return None

    def release(self):
        """Forget armed/forced state. True when we still held the display."""
        was_forced = self.forced
        self.forced = False
        self._loud_since = None
        self._last_loud = None
        return was_forced


class SpectrumStreamer(threading.Thread):
    def __init__(self, ip, port):
        super().__init__(daemon=True, name="audio-spectrum")
        self._lock = threading.Lock()
        self._ip = ip
        self._port = int(port)
        self._target = None  # Only numeric addresses may reach the capture thread.
        self._default_device_id = None
        self._maintenance_wake = threading.Event()
        self._stop_event = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.last_sent = 0.0
        self.last_error = ""
        self.last_level_db = SILENT_DB
        self.auto = VizAutoTrigger()
        self.auto_error = ""
        self._mode_pending = None
        self._mode_revision = 0
        self._mode_retry_at = 0.0
        self._device_boot_at = None
        self._device_viz = False
        self.stalls = 0
        self.reopens = 0
        self._capture_mmcss = None   # MMCSS handles: kept alive, not inspected
        self._watchdog_mmcss = None

        # Legacy 32-band analyzer precomputations (matching main)
        self._legacy_window = np.hanning(LEGACY_FRAMES).astype(np.float32) if np else None
        if np:
            edges = LEGACY_F_LO * (LEGACY_F_HI / LEGACY_F_LO) ** (np.arange(LEGACY_BANDS + 1) / float(LEGACY_BANDS))
            bin_hz = RATE / float(FFT_N)
            self._legacy_band_bins = []
            for i in range(LEGACY_BANDS):
                lo = int(edges[i] / bin_hz)
                hi = max(lo + 1, int(edges[i + 1] / bin_hz))
                self._legacy_band_bins.append((lo, hi))
        else:
            self._legacy_band_bins = []
        self._agc_ref = AGC_FLOOR_DB
        self._legacy_buffer = np.empty(0, dtype=np.float32) if np else []

        # Mica AudioMotion DSP state
        self._window = np.hanning(FFT_N).astype(np.float32) if np else None
        self._display_ranges = build_mode0_band_ranges(
            FFT_N, RATE, F_LO, F_HI, MODE0_VIEWPORT_WIDTH) if np else []
        self._sample_window = np.empty(0, dtype=np.float32) if np else []
        self._b_weight = build_b_weighting_multipliers(FFT_N, RATE) if np else None
        self._smoothed_power = None
        self._envelope = EnvelopeSmoother(
            len(self._display_ranges), SMOOTHER_RISE, SMOOTHER_FALL,
            SMOOTHER_DAMPING) if np else None
        self._amp_floor = 10.0 ** (DB_FLOOR / 20.0)
        self._amp_ceil = 10.0 ** (DB_CEILING / 20.0)
        self._amp_range = self._amp_ceil - self._amp_floor
        self._last_mica_bands = np.zeros(BANDS, dtype=np.uint8) if np else None

        self._wave_ref = WAVE_AGC_FLOOR
        self._last_legacy_bands = np.zeros(LEGACY_BANDS, dtype=np.uint8) if np else None
        self._last_bands = None
        self._last_wave = None
        self._last_frame_at = 0.0

    def set_target(self, ip, port):
        with self._lock:
            if (ip, int(port)) == (self._ip, self._port):
                return
            self._ip = ip
            self._port = int(port)
            self._target = None
            self._mode_revision += 1
            self._mode_pending = "viz" if self.auto.forced else None
            self._mode_retry_at = 0.0
            self.auto_error = ""
            self._device_boot_at = None
            self._device_viz = False
        self._maintenance_wake.set()

    def set_auto(self, enabled, threshold_db, start_delay, stop_delay):
        with self._lock:
            release = self.auto.configure(enabled, threshold_db,
                                          start_delay, stop_delay)
        if release:
            self._send_mode("auto")

    def stop(self):
        self._stop_event.set()
        self._maintenance_wake.set()

    def _send_mode(self, mode, blocking=False):
        """Queue the latest intent; transient wake/network failures are retried."""
        with self._lock:
            self._mode_revision += 1
            self._mode_pending = mode
            self._mode_retry_at = 0.0
        if blocking:
            self._flush_mode()
        else:
            self._maintenance_wake.set()

    def _flush_mode(self):
        """Only the control worker performs HTTP, serially and outside the lock."""
        with self._lock:
            mode = self._mode_pending
            revision = self._mode_revision
            ip = self._target[0] if self._target else self._ip
            if not mode or not ip or time.monotonic() < self._mode_retry_at:
                return
        error = ""
        try:
            with urlopen("http://%s/api/mode/%s" % (ip, mode), timeout=4) as r:
                r.read(256)
        except Exception as err:
            error = "%s: %s" % (mode, err)
        with self._lock:
            if revision != self._mode_revision:
                return  # A newer intent must not be cleared by this response.
            self.auto_error = error
            if error:
                self._mode_retry_at = time.monotonic() + 2.0
            else:
                self._mode_pending = None
                self._device_viz = mode == "viz"

    def _check_device_restart(self):
        with self._lock:
            target = self._target
            revision = self._mode_revision
            enabled = self.auto.enabled
        if not target or not enabled:
            return
        try:
            with urlopen("http://%s/api/status" % target[0], timeout=1.5) as r:
                state = json.loads(r.read(4096))
            uptime = state.get("uptime")
            boot_at = time.monotonic() - uptime if isinstance(uptime, (int, float)) else None
        except Exception:
            return
        with self._lock:
            if target != self._target or revision != self._mode_revision:
                return
            restarted = (boot_at is not None and self._device_boot_at is not None
                         and boot_at - self._device_boot_at > 3.0)
            if boot_at is not None:
                self._device_boot_at = boot_at
            self._device_viz = bool(state.get("forcedViz"))
            if restarted and self.auto.forced:
                self._mode_revision += 1
                self._mode_pending = "viz"
                self._mode_retry_at = 0.0

    def _process_legacy_bands(self, mono, dt=None):
        """Processes 32 logarithmic frequency bands with AGC (exactly matching main)."""
        if np is None:
            return None
        if dt is None:
            dt = FRAMES / float(RATE)
        mono = np.asarray(mono, dtype=np.float32).reshape(-1)
        if mono.size < LEGACY_FRAMES:
            mono = np.pad(mono, (LEGACY_FRAMES - mono.size, 0))
        elif mono.size > LEGACY_FRAMES:
            mono = mono[-LEGACY_FRAMES:]

        spec = np.abs(np.fft.rfft(mono * self._legacy_window, n=FFT_N))
        amps = np.empty(LEGACY_BANDS, dtype=np.float32)
        for i, (lo, hi) in enumerate(self._legacy_band_bins):
            amps[i] = np.sqrt(np.mean(spec[lo:hi] ** 2))
        db = 20.0 * np.log10(amps + 1e-7)

        peak = float(db.max())
        self._agc_ref = max(self._agc_ref - AGC_DECAY_DB_PER_S * dt,
                            peak, AGC_FLOOR_DB)
        norm = (db - (self._agc_ref - AGC_RANGE_DB)) / AGC_RANGE_DB
        return np.clip(norm * 255.0, 0, 255).astype(np.uint8)

    def _analyse_window(self, samples):
        """Analyse one 2048-sample Mica window and return its Bins128 payload."""
        # 1. FFT → normalised power spectrum (Mica FftUtility.PowerSpectrum: |X|²/N²)
        fft_out = np.fft.rfft(samples * self._window, n=FFT_N)
        power = (fft_out.real ** 2 + fft_out.imag ** 2) / (FFT_N * FFT_N)

        # 2. Mica's FftSmoothing is the previous-frame weight (0.75)
        if self._smoothed_power is None or self._smoothed_power.shape != power.shape:
            self._smoothed_power = power.copy()
        else:
            self._smoothed_power *= FFT_SMOOTHING
            self._smoothed_power += power * (1.0 - FFT_SMOOTHING)

        # 3. B-weighting (per Mica's WeightingCurve.BuildPowerMultipliers)
        weighted = self._smoothed_power * self._b_weight

        # 4. AudioMotion display bands use peak aggregation across each Bark range
        amps = np.empty(len(self._display_ranges), dtype=np.float32)
        for i, (lo, hi) in enumerate(self._display_ranges):
            segment = weighted[lo:hi]
            amps[i] = np.sqrt(np.max(segment)) if segment.size > 0 else 0.0

        # 5. Linear amplitude normalization -85..-25 dB x 1.3
        norm = np.clip(
            (amps - self._amp_floor) / self._amp_range * LINEAR_BOOST,
            0.0, 1.0)

        # 6. Envelope smoother (rise 0.82 / fall 0.06 / damping 0.30 per hop)
        smoothed = self._envelope.process(norm)

        # 7. Endpoint-preserving linear resample 42 -> 128
        return resample_audio_motion_bands(smoothed * 255.0)

    def _process_block(self, mono):
        """Append captured samples, update rolling buffers, run Mica hops and legacy analyzer."""
        samples = np.asarray(mono, dtype=np.float32).reshape(-1)
        if samples.size:
            self._sample_window = np.concatenate((self._sample_window, samples))
            self._legacy_buffer = np.concatenate((self._legacy_buffer, samples))
            if self._legacy_buffer.size > LEGACY_FRAMES:
                self._legacy_buffer = self._legacy_buffer[-LEGACY_FRAMES:]

        # Mica AudioMotion DSP: process all complete 256-sample hops
        latest_mica = None
        while self._sample_window.size >= FFT_N:
            latest_mica = self._analyse_window(self._sample_window[:FFT_N])
            self._sample_window = self._sample_window[HOP_N:]

        if latest_mica is not None:
            mica_bands = latest_mica
            self._last_mica_bands = mica_bands
        elif self._last_mica_bands is not None:
            mica_bands = self._last_mica_bands
        else:
            mica_bands = np.zeros(BANDS, dtype=np.uint8)

        # Legacy 32-band analyzer over rolling 1920-sample window
        dt = float(samples.size) / float(RATE) if samples.size else (FRAMES / float(RATE))
        legacy_bands = self._process_legacy_bands(self._legacy_buffer, dt=dt)
        self._last_legacy_bands = legacy_bands

        return legacy_bands, mica_bands

    def _process_wave(self, mono, dt=None):
        """Trigger-aligned, decimated waveform as offset-binary bytes."""
        if dt is None:
            dt = FRAMES / float(RATE)
        usable = (len(mono) // WAVE_DECIM) * WAVE_DECIM
        low = mono[:usable].reshape(-1, WAVE_DECIM).mean(axis=1)
        if len(low) < WAVE_POINTS:
            low = np.pad(low, (0, WAVE_POINTS - len(low)))

        # Start on a rising zero crossing so the trace stands still instead of
        # sliding sideways one block to the next.
        start = 0
        limit = len(low) - WAVE_POINTS
        if limit > 0:
            head = low[:limit + 1]
            rising = np.nonzero((head[:-1] <= 0.0) & (head[1:] > 0.0))[0]
            if rising.size:
                start = int(rising[0]) + 1
        seg = low[start:start + WAVE_POINTS]

        peak = float(np.abs(seg).max())
        self._wave_ref = max(self._wave_ref * (WAVE_AGC_DECAY ** dt), peak,
                             WAVE_AGC_FLOOR)
        scaled = seg / self._wave_ref * WAVE_GAIN + 128.0
        return np.clip(scaled, 0, 255).astype(np.uint8)

    def _block_level_db(self, mono):
        """Block loudness in dBFS. AGC-free, so the auto-start threshold
        means the same thing whatever the music's volume."""
        rms = float(np.sqrt(np.mean(mono ** 2)))
        if rms <= 0.0:
            return SILENT_DB
        return max(SILENT_DB, 20.0 * np.log10(rms))

    def _send_bands(self, legacy_bands, mica_bands, wave=None):
        with self._lock:
            target = self._target
        if target is None or self._stop_event.is_set():
            return
        if wave is None:
            wave = np.full(WAVE_POINTS, 128, dtype=np.uint8)
        try:
            packet = (FFT3_MAGIC
                      + legacy_bands.astype(np.uint8).tobytes()
                      + mica_bands.astype(np.uint8).tobytes()
                      + wave.astype(np.uint8).tobytes())
            self._sock.sendto(packet, target)
            self.last_sent = time.monotonic()
        except OSError:
            pass

    def _watchdog(self):
        """Cover gaps the capture thread cannot fill. Silent while capture keeps
        up, because then a packet has always just gone out."""
        self._watchdog_mmcss = boost_thread_priority()
        try:
            holding = False
            while not self._stop_event.is_set():
                self._stop_event.wait(GAP_S / 2.0)
                now = time.monotonic()
                with self._lock:
                    legacy_bands = self._last_legacy_bands
                    mica_bands = self._last_bands
                    wave = self._last_wave
                    last_frame_at = self._last_frame_at
                if legacy_bands is None or mica_bands is None or not self.last_sent or now - self.last_sent < GAP_S:
                    holding = False
                    continue
                action = frame_action(now - last_frame_at)
                if action == "stop":
                    continue
                if not holding:
                    holding = True
                    self.stalls += 1
                if action == "decay":
                    legacy_bands = (legacy_bands.astype(np.float32) * STALL_DECAY).astype(np.uint8)
                    mica_bands = mica_bands * STALL_DECAY
                    if wave is not None:
                        wave = np.clip((wave.astype(np.float32) - 128.0)
                                       * STALL_DECAY + 128.0, 0, 255).astype(np.uint8)
                    with self._lock:
                        self._last_legacy_bands = legacy_bands
                        self._last_bands = mica_bands
                        self._last_wave = wave
                self._send_bands(legacy_bands, mica_bands.astype(np.uint8), wave)
        finally:
            release_thread_priority(self._watchdog_mmcss)

    def _maintenance(self):
        """Slow DNS and device enumeration never run between captured blocks."""
        resolved_for = None
        resolve_at = check_at = status_at = 0.0
        with audio_thread_com():
            while not self._stop_event.is_set():
                now = time.monotonic()
                with self._lock:
                    requested = (self._ip, self._port)
                if requested != resolved_for or now >= resolve_at:
                    try:
                        address = socket.getaddrinfo(*requested, socket.AF_INET,
                                                     socket.SOCK_DGRAM)[0][4]
                        with self._lock:
                            if requested == (self._ip, self._port):
                                self._target = address
                        resolved_for = requested
                        resolve_at = now + 30.0
                    except OSError:
                        resolved_for = requested
                        resolve_at = now + 2.0
                if now >= check_at:
                    try:
                        device_id = sc.default_speaker().id
                        with self._lock:
                            self._default_device_id = device_id
                    except Exception:
                        pass
                    check_at = now + 10.0
                if self._stop_event.is_set():
                    break
                if now >= status_at:
                    self._check_device_restart()
                    status_at = now + 10.0
                self._flush_mode()
                self._maintenance_wake.wait(1.0)
                self._maintenance_wake.clear()

    def run(self):
        self._capture_mmcss = boost_thread_priority()
        watchdog = threading.Thread(target=self._watchdog, daemon=True,
                                    name="audio-watchdog")
        maintenance = threading.Thread(target=self._maintenance, daemon=True,
                                       name="audio-device-control")
        try:
            with audio_thread_com():
                watchdog.start()
                maintenance.start()
                self._capture_loop()
        except Exception as error:
            self.last_error = str(error)
        finally:
            self.stop()
            if watchdog.ident is not None:
                watchdog.join(timeout=1.0)
            if maintenance.ident is not None:
                maintenance.join(timeout=5.0)
            with self._lock:
                release = self.auto.release()
            if release:
                self._send_mode("auto", blocking=True)
            self._sock.close()
            release_thread_priority(self._capture_mmcss)

    def _capture_loop(self):
        attempt = 0
        reopen_after = SILENCE_REOPEN_S
        while not self._stop_event.is_set():
            try:
                spk = sc.default_speaker()
                with self._lock:
                    self._default_device_id = spk.id
                mic = sc.get_microphone(id=spk.id, include_loopback=True)
                with mic.recorder(samplerate=RATE, blocksize=FRAMES) as rec:
                    attempt = 0
                    silent_since = None
                    last_wall = time.time()
                    while not self._stop_event.is_set():
                        data = rec.record(numframes=FRAMES)
                        now_wall = time.time()
                        wall_gap = now_wall - last_wall
                        last_wall = now_wall
                        mono = data.mean(axis=1) if data.ndim > 1 else data
                        mono = mono.astype(np.float32)
                        legacy_bands, mica_bands = self._process_block(mono)
                        wave = self._process_wave(self._legacy_buffer)
                        level = self._block_level_db(mono)
                        self.last_level_db = level
                        self.last_error = ""
                        with self._lock:
                            self._last_legacy_bands = legacy_bands.copy()
                            self._last_bands = mica_bands.astype(np.float32)
                            self._last_wave = wave
                            self._last_frame_at = time.monotonic()
                            action = self.auto.feed(level, self._last_frame_at)
                        self._send_bands(legacy_bands, mica_bands, wave)
                        if action:
                            self._send_mode(action)
                        if level > SILENT_DB:
                            silent_since = None
                            reopen_after = SILENCE_REOPEN_S
                        elif silent_since is None:
                            silent_since = self._last_frame_at
                        silence = 0.0 if silent_since is None else \
                            self._last_frame_at - silent_since
                        if capture_stale(silence, wall_gap, reopen_after):
                            reopen_after = min(reopen_after * 2, SILENCE_REOPEN_MAX_S)
                            self.reopens += 1
                            break
                        with self._lock:
                            default_id = self._default_device_id
                        if default_id is not None and default_id != spk.id:
                            break
            except Exception as e:
                self.last_error = str(e)
                self._stop_event.wait(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
                attempt += 1


_lock = threading.Lock()
_streamer = None


def auto_settings(config):
    """Clamped auto-start settings from a config dict."""
    config = config or {}
    threshold, start, stop = clamp_auto(
        config.get("audio_viz_threshold", AUTO_THRESHOLD_DB),
        config.get("audio_viz_start_delay", AUTO_START_DELAY),
        config.get("audio_viz_stop_delay", AUTO_STOP_DELAY))
    return bool(config.get("audio_viz_auto")), threshold, start, stop


def ensure(config):
    """Start/stop/redirect the streamer to match the config. Safe to call often."""
    global _streamer
    if not AVAILABLE:
        return
    config = config or {}
    ip = (config.get("esp32_ip") or "").strip()
    port = int(config.get("udp_port") or 4210)
    want = bool(config.get("audio_viz")) and bool(ip)
    auto = auto_settings(config)
    with _lock:
        if _streamer is not None and _streamer._stop_event.is_set():
            _streamer.join(timeout=2.0)
            if _streamer.is_alive():
                return
        if _streamer is not None and not _streamer.is_alive():
            _streamer = None
        if want and _streamer is None:
            boost_process_priority()
            _streamer = SpectrumStreamer(ip, port)
            _streamer.set_auto(*auto)
            _streamer.start()
        elif want and _streamer is not None:
            _streamer.set_target(ip, port)
            _streamer.set_auto(*auto)
        elif not want and _streamer is not None:
            _streamer.stop()
            _streamer.join(timeout=2.0)
            if not _streamer.is_alive():
                _streamer = None


def stop_all():
    global _streamer
    with _lock:
        if _streamer is not None:
            _streamer.stop()
            _streamer.join(timeout=2.0)
            if not _streamer.is_alive():
                _streamer = None


def status():
    """Fields merged into /api/status for the web UI."""
    with _lock:
        running = _streamer is not None and _streamer.is_alive()
        sending = running and (time.monotonic() - _streamer.last_sent) < 2.0
        err = _streamer.last_error if _streamer is not None else ""
        stalls = _streamer.stalls if _streamer is not None else 0
        reopens = _streamer.reopens if _streamer is not None else 0
        auto_on = running and _streamer.auto.enabled
        forced = running and _streamer.auto.forced and _streamer._device_viz
        level = _streamer.last_level_db if running else SILENT_DB
        auto_err = _streamer.auto_error if _streamer is not None else ""
    return {
        "audioVizAvailable": AVAILABLE,
        "audioVizReason": _IMPORT_ERROR,
        "audioVizEnabled": running,
        "audioVizSending": sending,
        "audioVizError": err,
        "audioVizAuto": auto_on,
        "audioVizForced": forced,
        "audioVizLevel": round(float(level), 1),
        "audioVizAutoError": auto_err,
        "audioVizStalls": stalls,
        "audioVizReopens": reopens,
    }
