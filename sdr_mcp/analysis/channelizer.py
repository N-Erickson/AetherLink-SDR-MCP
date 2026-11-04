"""
Stateful DSP building blocks for narrowband decoders fed from a wide IQ capture.

Channelizer: mix one channel to baseband, low-pass it and decimate to an
integer sub-rate. FMDiscriminator: instantaneous frequency in Hz. BitSlicer:
clock recovery and mid-bit sampling at one baud rate. All of them carry state
across chunks so a stream can be processed piecewise with no seams.
"""

import math
from typing import Optional

import numpy as np
from scipy import signal as sg


class Channelizer:
    """Select one narrow channel out of a wide complex capture."""

    def __init__(self, sample_rate: float, offset_hz: float, bandwidth_hz: float,
                 target_rate: float, order: int = 6):
        self.sample_rate = float(sample_rate)
        self.offset_hz = float(offset_hz)
        self.decimation = max(1, int(round(self.sample_rate / target_rate)))
        self.rate = self.sample_rate / self.decimation
        cutoff = min(bandwidth_hz / 2.0, 0.45 * self.rate)
        # Two stages keep the IIR well conditioned at very low normalized cutoffs
        stage1_cut = max(cutoff * 4, 0.02 * self.sample_rate)
        stage1_cut = min(stage1_cut, 0.45 * self.sample_rate)
        self.sos1 = sg.butter(order, stage1_cut, btype="low", fs=self.sample_rate, output="sos")
        self.sos2 = sg.butter(order, cutoff, btype="low", fs=self.sample_rate, output="sos")
        self.zi1 = np.zeros((self.sos1.shape[0], 2), dtype=np.complex128)
        self.zi2 = np.zeros((self.sos2.shape[0], 2), dtype=np.complex128)
        self.phase = 0.0
        self.decim_phase = 0

    def process(self, iq: np.ndarray) -> np.ndarray:
        n = len(iq)
        if n == 0:
            return np.zeros(0, dtype=np.complex128)
        x = np.asarray(iq, dtype=np.complex128)
        if self.offset_hz:
            w = -2.0 * math.pi * self.offset_hz / self.sample_rate
            x = x * np.exp(1j * (self.phase + w * np.arange(n)))
            self.phase = (self.phase + w * n) % (2.0 * math.pi)
        y, self.zi1 = sg.sosfilt(self.sos1, x, zi=self.zi1)
        y, self.zi2 = sg.sosfilt(self.sos2, y, zi=self.zi2)
        idx = np.arange(self.decim_phase, n, self.decimation)
        if len(idx):
            self.decim_phase = int(idx[-1] + self.decimation - n)
            return y[idx]
        self.decim_phase -= n
        return np.zeros(0, dtype=np.complex128)


class FMDiscriminator:
    """Instantaneous frequency (Hz) from consecutive-sample phase differences."""

    def __init__(self, rate: float):
        self.rate = float(rate)
        self.last: Optional[complex] = None

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return np.zeros(0)
        prev = x[0] if self.last is None else self.last
        self.last = x[-1]
        shifted = np.concatenate(([prev], x[:-1]))
        return np.angle(x * np.conj(shifted)) * (self.rate / (2.0 * math.pi))


class DCTracker:
    """Single-pole tracker that removes a slowly varying offset (frequency error)."""

    def __init__(self, rate: float, time_constant_s: float = 0.2):
        alpha = 1.0 / (time_constant_s * rate)
        self.b = [alpha]
        self.a = [1.0, -(1.0 - alpha)]
        self.zi = None

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return x
        if self.zi is None:
            self.zi = sg.lfilter_zi(self.b, self.a) * x[0]
        track, self.zi = sg.lfilter(self.b, self.a, x, zi=self.zi)
        return x - track


class BitSlicer:
    """Recover bits from a bipolar baseband signal at one baud rate.

    Transition phases are averaged (circularly) in absolute sample coordinates,
    optionally weighted by signal strength so idle-channel noise cannot steer
    the clock. The sampling instant sits half a bit after the transitions, and
    a fractional absolute index carries timing across chunks so nothing is
    sampled twice or skipped.
    """

    def __init__(self, sample_rate: float, baud: float, loop_gain: float = 0.25):
        self.spb = float(sample_rate) / float(baud)
        self.loop_gain = loop_gain
        self.phase: Optional[float] = None       # absolute transition phase, 0..spb
        self.next_abs: Optional[float] = None    # absolute index of the next sample instant
        self.base = 0                            # absolute index of carry[0] / x[0]
        self.carry = np.zeros(0)
        self.carry_w = np.zeros(0)

    def process(self, x: np.ndarray, weights: Optional[np.ndarray] = None) -> np.ndarray:
        if weights is None:
            weights = np.ones(len(x))
        if len(self.carry):
            x = np.concatenate((self.carry, x))
            weights = np.concatenate((self.carry_w, weights))
        n = len(x)
        keep = int(2 * self.spb) + 1
        if n < 4 * self.spb:
            self.carry, self.carry_w = x, weights
            return np.zeros(0, dtype=np.int8)

        signs = (x > 0).astype(np.int8)
        transitions = np.flatnonzero(np.diff(signs)) + 1
        if len(transitions) >= 4:
            w = weights[np.minimum(transitions, n - 1)]
            if float(np.sum(w)) > 0:
                angles = 2.0 * math.pi * ((transitions + self.base) % self.spb) / self.spb
                mean = math.atan2(float(np.sum(w * np.sin(angles))), float(np.sum(w * np.cos(angles))))
                offset = (mean / (2.0 * math.pi) * self.spb) % self.spb
                if self.phase is None:
                    self.phase = offset
                    first = offset + self.spb / 2.0
                    k = max(0, math.ceil((self.base - first) / self.spb))
                    self.next_abs = first + k * self.spb
                else:
                    delta = ((offset - self.phase + self.spb / 2.0) % self.spb) - self.spb / 2.0
                    self.phase = (self.phase + self.loop_gain * delta) % self.spb
                    self.next_abs += self.loop_gain * delta

        if self.phase is None:
            self.carry, self.carry_w = x[-keep:], weights[-keep:]
            self.base += n - keep
            return np.zeros(0, dtype=np.int8)

        rel = np.arange(self.next_abs - self.base, n, self.spb)
        rel = rel[rel >= 0]
        bits = signs[rel.astype(int)] if len(rel) else np.zeros(0, dtype=np.int8)
        if len(rel):
            self.next_abs = self.base + rel[-1] + self.spb
        self.carry, self.carry_w = x[-keep:], weights[-keep:]
        self.base += n - keep
        return bits

    def flush(self) -> np.ndarray:
        """Sample whatever is still held back (end of a burst)."""
        if self.phase is None or not len(self.carry):
            return np.zeros(0, dtype=np.int8)
        x = self.carry
        n = len(x)
        signs = (x > 0).astype(np.int8)
        rel = np.arange(self.next_abs - self.base, n, self.spb)
        rel = rel[rel >= 0]
        bits = signs[rel.astype(int)] if len(rel) else np.zeros(0, dtype=np.int8)
        if len(rel):
            self.next_abs = self.base + rel[-1] + self.spb
        self.carry, self.carry_w = np.zeros(0), np.zeros(0)
        self.base += n
        return bits
