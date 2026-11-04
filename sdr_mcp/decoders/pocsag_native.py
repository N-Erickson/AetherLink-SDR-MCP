"""
Native POCSAG decoder: IQ in, pager messages out, no external programs.

Pipeline per IQ chunk (all stages keep state across chunks):
  FM discriminator -> low-pass + integer decimation to ~24 kHz -> DC tracking
  -> per-baud-rate bit timing recovery (512 / 1200 / 2400) -> sync word search
  -> BCH(31,21) check/correct -> address / message assembly.

The encoder at the bottom exists so the decoder can be tested without a transmitter.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from scipy import signal as sg

from ..analysis.channelizer import BitSlicer, Channelizer, DCTracker, FMDiscriminator
from .pocsag import POCSAGMessage

logger = logging.getLogger(__name__)

SYNC_WORD = 0x7CD215D8
IDLE_WORD = 0x7A89C197
BCH_POLY = 0x769            # x^10 + x^9 + x^8 + x^6 + x^5 + x^3 + 1
BAUD_RATES = (512, 1200, 2400)
CODEWORDS_PER_BATCH = 16
FSK_DEVIATION_HZ = 4500.0
NUMERIC_CHARS = "0123456789*U -)("


# ---------------------------------------------------------------- BCH(31,21) + parity

def _poly_mod(value: int) -> int:
    """Remainder of a 31-bit value divided by the BCH generator polynomial."""
    for i in range(30, 9, -1):
        if value & (1 << i):
            value ^= BCH_POLY << (i - 10)
    return value & 0x3FF


def bch_encode(data21: int) -> int:
    """Build a full 32-bit POCSAG codeword (21 data bits + 10 BCH bits + even parity)."""
    shifted = (data21 & 0x1FFFFF) << 10
    cw31 = shifted | _poly_mod(shifted)
    parity = bin(cw31).count("1") & 1
    return (cw31 << 1) | parity


def _build_syndrome_table() -> Dict[int, int]:
    table: Dict[int, int] = {}
    for i in range(31):
        e = 1 << i
        table.setdefault(_poly_mod(e), e)
    for i in range(31):
        for j in range(i + 1, 31):
            e = (1 << i) | (1 << j)
            table.setdefault(_poly_mod(e), e)
    return table


SYNDROMES = _build_syndrome_table()


def bch_correct(cw32: int) -> Optional[int]:
    """Return the codeword with up to 2 bit errors corrected, or None if uncorrectable."""
    cw31 = cw32 >> 1
    syndrome = _poly_mod(cw31)
    if syndrome == 0:
        return cw32
    mask = SYNDROMES.get(syndrome)
    if mask is None:
        return None
    return ((cw31 ^ mask) << 1) | (cw32 & 1)


def _hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFF).count("1")


# ---------------------------------------------------------------- message assembly

def _message_bits(codewords: List[int]) -> List[int]:
    """Message bits in transmission order: codeword bit 30 first ... bit 11 last."""
    bits: List[int] = []
    for cw in codewords:
        data = (cw >> 11) & 0xFFFFF
        for i in range(19, -1, -1):
            bits.append((data >> i) & 1)
    return bits


def decode_alpha(codewords: List[int]) -> str:
    """7-bit ASCII packed across message codewords, each character LSB first."""
    bits = _message_bits(codewords)
    chars = []
    for i in range(0, len(bits) - 6, 7):
        value = sum(bit << k for k, bit in enumerate(bits[i:i + 7]))
        if value in (0x04, 0x17):      # EOT / ETB end the message
            break
        if value == 0:
            continue
        chars.append(chr(value) if 32 <= value <= 126 else f"<{value:02X}>")
    return "".join(chars).strip()


def decode_numeric(codewords: List[int]) -> str:
    """4-bit BCD digits packed across message codewords, each digit LSB first."""
    bits = _message_bits(codewords)
    digits = []
    for i in range(0, len(bits) - 3, 4):
        value = sum(bit << k for k, bit in enumerate(bits[i:i + 4]))
        digits.append(NUMERIC_CHARS[value])
    return "".join(digits).strip()


def _looks_like_text(text: str) -> bool:
    if not text:
        return False
    good = sum(1 for c in text if c.isalnum() or c in " .,:;-/()'\"!?@#&+=%*_")
    return good / max(len(text), 1) > 0.8


@dataclass
class _Pending:
    address: int
    function: int
    frame: int
    codewords: List[int]


class POCSAGBitDecoder:
    """Turns a bit stream (transmission order) into POCSAG messages."""

    def __init__(self, bitrate: int, on_message: Callable[[POCSAGMessage], None]):
        self.bitrate = bitrate
        self.on_message = on_message
        self.register = 0
        self.locked = False
        self.inverted = False
        self.bit_count = 0           # bits collected into the current codeword
        self.codeword_index = 0      # 0..15 within the batch
        self.pending: Optional[_Pending] = None
        self.batches = 0
        self.codewords_ok = 0
        self.codewords_bad = 0
        self.messages = 0
        self._consecutive_bad = 0

    def feed_bits(self, bits: np.ndarray):
        for b in bits:
            self._feed_bit(int(b))

    def _feed_bit(self, bit: int):
        self.register = ((self.register << 1) | bit) & 0xFFFFFFFF
        if not self.locked:
            if self.register == SYNC_WORD:
                self._start_batch(inverted=False)
            elif self.register == SYNC_WORD ^ 0xFFFFFFFF:
                self._start_batch(inverted=True)
            return

        self.bit_count += 1
        if self.bit_count < 32:
            return
        self.bit_count = 0
        word = self.register ^ 0xFFFFFFFF if self.inverted else self.register

        if self.codeword_index == CODEWORDS_PER_BATCH:
            # Expecting the next batch's sync word
            if _hamming(word, SYNC_WORD) <= 2:
                self.codeword_index = 0
                self.batches += 1
            else:
                self._lose_lock()
            return

        self._handle_codeword(word, self.codeword_index)
        self.codeword_index += 1

    def _start_batch(self, inverted: bool):
        self.locked = True
        self.inverted = inverted
        self.bit_count = 0
        self.codeword_index = 0
        self.batches += 1
        self._consecutive_bad = 0

    def _lose_lock(self):
        self._flush()
        self.locked = False
        self.bit_count = 0
        self.codeword_index = 0

    def _handle_codeword(self, word: int, index: int):
        corrected = bch_correct(word)
        if corrected is None:
            self.codewords_bad += 1
            self._consecutive_bad += 1
            if self._consecutive_bad >= 4:
                self._lose_lock()
            return
        self._consecutive_bad = 0
        self.codewords_ok += 1

        if corrected == IDLE_WORD:
            self._flush()
            return
        if (corrected >> 31) & 1:
            # Message codeword
            if self.pending is not None:
                self.pending.codewords.append(corrected)
            return
        # Address codeword: 18 address bits + 2 function bits; frame number supplies the 3 LSBs
        self._flush()
        address = (((corrected >> 13) & 0x3FFFF) << 3) | (index // 2)
        function = (corrected >> 11) & 0x3
        self.pending = _Pending(address=address, function=function, frame=index // 2, codewords=[])

    def _flush(self):
        pending, self.pending = self.pending, None
        if pending is None:
            return
        message_type, text = "tone", ""
        if pending.codewords:
            alpha = decode_alpha(pending.codewords)
            numeric = decode_numeric(pending.codewords)
            if pending.function == 3:
                message_type, text = "alpha", alpha
            elif pending.function == 0:
                message_type, text = "numeric", numeric
            elif _looks_like_text(alpha):
                message_type, text = "alpha", alpha
            else:
                message_type, text = "numeric", numeric
        self.messages += 1
        self.on_message(POCSAGMessage(
            address=pending.address,
            function=pending.function,
            message=text,
            timestamp=datetime.now(),
            bitrate=self.bitrate,
            numeric=(message_type == "numeric"),
            message_type=message_type,
        ))

    def stats(self) -> Dict[str, int]:
        return {
            "baud": self.bitrate, "batches": self.batches, "codewords_ok": self.codewords_ok,
            "codewords_bad": self.codewords_bad, "messages": self.messages, "locked": int(self.locked),
        }


# ---------------------------------------------------------------- IQ front end

class POCSAGDemodulator:
    """Feeds wide IQ chunks through channel selection, FM demodulation and all bit decoders.

    offset_hz is where the pager channel nominally sits relative to the capture
    center (the SDR is tuned off-channel to keep the signal clear of the DC
    spike). An automatic frequency control loop watches the spectrum around that
    offset and re-centers the channel on the actual carrier, which absorbs the
    frequency error of inexpensive dongles (tens of ppm, i.e. several kHz).
    """

    CHANNEL_BANDWIDTH_HZ = 14e3
    TARGET_RATE = 24000.0
    AFC_FFT = 4096
    AFC_MIN_SNR_DB = 12.0
    AFC_MIN_STEP_HZ = 250.0

    def __init__(self, sample_rate: float, offset_hz: float = 0.0, bauds=BAUD_RATES,
                 on_message: Optional[Callable[[POCSAGMessage], None]] = None,
                 afc_window_hz: float = 10e3):
        self.sample_rate = float(sample_rate)
        self.nominal_offset = float(offset_hz)
        self.afc_window = float(afc_window_hz)
        self.on_message = on_message or (lambda m: None)
        self.channel = Channelizer(self.sample_rate, offset_hz, self.CHANNEL_BANDWIDTH_HZ, self.TARGET_RATE)
        self.rate = self.channel.rate
        self.disc = FMDiscriminator(self.rate)
        self._data_sos = sg.butter(4, 3600.0, btype="low", fs=self.rate, output="sos")
        self._data_zi = sg.sosfilt_zi(self._data_sos) * 0.0
        self.dc = DCTracker(self.rate, 0.2)
        self.slicers = {b: BitSlicer(self.rate, b) for b in bauds}
        self.decoders = {b: POCSAGBitDecoder(b, self._emit) for b in bauds}
        self.chunks = 0
        self.level = 0.0
        self.afc_hz = 0.0
        self.afc_updates = 0
        self.signal_present = False

    def _emit(self, message: POCSAGMessage):
        self.on_message(message)

    def _afc(self, samples: np.ndarray):
        """Re-center the channel on the FSK carrier nearest the nominal offset.

        A 2-FSK signal shows two spectral peaks (mark and space) about 9 kHz
        apart, so the raw spectrum is first smoothed over that width; the smoothed
        peak sits on the carrier, and a noise-subtracted power centroid refines it.
        """
        nper = min(self.AFC_FFT, len(samples))
        if nper < 256:
            return
        freqs, pxx = sg.welch(samples, fs=self.sample_rate, nperseg=nper, noverlap=0,
                              return_onesided=False, scaling="spectrum", detrend=False)
        freqs = np.fft.fftshift(freqs)
        pxx = np.fft.fftshift(pxx)
        pdb = 10 * np.log10(pxx + 1e-20)
        window = np.abs(freqs - self.nominal_offset) <= self.afc_window
        if not window.any():
            return
        floor_db = float(np.median(pdb))
        raw_peak = int(np.argmax(np.where(window, pdb, -np.inf)))
        if pdb[raw_peak] - floor_db < self.AFC_MIN_SNR_DB:
            self.signal_present = False
            return
        self.signal_present = True

        bin_hz = abs(float(freqs[1] - freqs[0]))
        box = max(3, int(round((2 * FSK_DEVIATION_HZ + 2000.0) / bin_hz)))
        smooth = np.convolve(pxx, np.ones(box) / box, mode="same")
        smooth_db = 10 * np.log10(smooth + 1e-20)
        best = int(np.argmax(np.where(window, smooth_db, -np.inf)))

        # Noise-subtracted power centroid around the smoothed peak, refined twice
        half = int(round((FSK_DEVIATION_HZ + 2500.0) / bin_hz))
        noise_lin = 10 ** (floor_db / 10)
        estimate = float(freqs[best])
        for _ in range(2):
            center_bin = int(np.argmin(np.abs(freqs - estimate)))
            lo, hi = max(center_bin - half, 0), min(center_bin + half + 1, len(freqs))
            weights = np.clip(pxx[lo:hi] - noise_lin, 0, None)
            if weights.sum() <= 0:
                return
            estimate = float(np.sum(freqs[lo:hi] * weights) / np.sum(weights))

        error = estimate - self.channel.offset_hz
        if abs(error) >= self.AFC_MIN_STEP_HZ:
            new_offset = self.channel.offset_hz + 0.5 * error
            new_offset = min(max(new_offset, self.nominal_offset - self.afc_window),
                             self.nominal_offset + self.afc_window)
            self.channel.offset_hz = new_offset
            self.afc_hz = new_offset - self.nominal_offset
            self.afc_updates += 1
            logger.debug(f"POCSAG AFC: carrier at {self.afc_hz:+.0f} Hz from nominal")

    def feed_iq(self, samples: np.ndarray):
        """Process one chunk of complex samples (any length)."""
        if len(samples) == 0:
            return
        self._afc(samples)
        base = self.channel.process(samples)
        if len(base) == 0:
            return
        envelope = np.abs(base)
        freq = self.disc.process(base)
        freq, self._data_zi = sg.sosfilt(self._data_sos, freq, zi=self._data_zi)
        bipolar = self.dc.process(freq)
        self.level = float(np.mean(envelope))
        for baud, slicer in self.slicers.items():
            bits = slicer.process(bipolar, weights=envelope)
            if len(bits):
                self.decoders[baud].feed_bits(bits)
        self.chunks += 1

    def stats(self) -> Dict[str, Any]:
        return {
            "afc_hz": round(self.afc_hz),
            "afc_updates": self.afc_updates,
            "signal_present": self.signal_present,
            "decoders": {str(b): d.stats() for b, d in self.decoders.items()},
        }


# ---------------------------------------------------------------- encoder (for tests)

def encode_alpha(text: str) -> List[int]:
    bits: List[int] = []
    for ch in text + "\x04":
        value = ord(ch) & 0x7F
        bits.extend((value >> k) & 1 for k in range(7))
    return _pack_message_bits(bits)


def encode_numeric(text: str) -> List[int]:
    digits = list(text)
    while len(digits) % 5:
        digits.append(" ")           # pad to whole codewords with spaces, as pagers do
    bits: List[int] = []
    for ch in digits:
        value = NUMERIC_CHARS.index(ch)
        bits.extend((value >> k) & 1 for k in range(4))
    return _pack_message_bits(bits)


def _pack_message_bits(bits: List[int]) -> List[int]:
    while len(bits) % 20:
        bits.append(0)
    words = []
    for i in range(0, len(bits), 20):
        data = 0
        for bit in bits[i:i + 20]:
            data = (data << 1) | bit
        words.append(bch_encode((1 << 20) | data))     # flag bit 1 = message
    return words


def encode_transmission(messages: List[Dict], preamble_bits: int = 576) -> List[int]:
    """Build the bit stream for a list of {address, function, text, kind} messages."""
    bits: List[int] = [1, 0] * (preamble_bits // 2)
    batch: List[int] = []

    def emit_batch():
        nonlocal batch
        while len(batch) < CODEWORDS_PER_BATCH:
            batch.append(IDLE_WORD)
        for word in [SYNC_WORD] + batch:
            bits.extend((word >> k) & 1 for k in range(31, -1, -1))
        batch = []

    for msg in messages:
        address = msg["address"]
        if not (0 <= address <= 0x1FFFFF):
            raise ValueError(f"POCSAG address {address} out of range (0-2097151)")
        frame = address & 0x7
        addr_word = bch_encode(((address >> 3) << 2) | (msg["function"] & 0x3))
        words = []
        if msg.get("text"):
            words = encode_alpha(msg["text"]) if msg.get("kind", "alpha") == "alpha" else encode_numeric(msg["text"])
        # Address must sit in its frame (2 codewords per frame)
        while (len(batch) // 2) != frame or len(batch) % 2:
            batch.append(IDLE_WORD)
            if len(batch) == CODEWORDS_PER_BATCH:
                emit_batch()
        batch.append(addr_word)
        if len(batch) == CODEWORDS_PER_BATCH:
            emit_batch()
        for word in words:
            batch.append(word)
            if len(batch) == CODEWORDS_PER_BATCH:
                emit_batch()
    if batch:
        emit_batch()
    emit_batch()   # trailing idle batch terminates the last message
    return bits


def modulate_fsk(bits: List[int], baud: int, sample_rate: float, deviation: float = FSK_DEVIATION_HZ,
                 invert: bool = False, noise: float = 0.0, freq_offset: float = 0.0,
                 rng: Optional[np.random.Generator] = None) -> np.ndarray:
    """2-FSK modulate a bit stream into complex IQ at sample_rate (1 -> -dev, 0 -> +dev)."""
    spb = sample_rate / baud
    n = int(len(bits) * spb)
    bit_index = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
    levels = np.where(np.asarray(bits)[bit_index] == 1, -1.0, 1.0)
    if invert:
        levels = -levels
    freq = levels * deviation + freq_offset
    phase = 2 * np.pi * np.cumsum(freq) / sample_rate
    iq = np.exp(1j * phase)
    if noise > 0:
        rng = rng or np.random.default_rng(0)
        iq = iq + noise * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return iq.astype(np.complex64)


def modulate_fsk_in_capture(bits: List[int], baud: int, sample_rate: float, offset_hz: float,
                            amplitude: float = 1.0, **kwargs) -> np.ndarray:
    """FSK signal placed at offset_hz inside a wide capture (for tests)."""
    iq = modulate_fsk(bits, baud, sample_rate, **kwargs).astype(np.complex128)
    t = np.arange(len(iq)) / sample_rate
    return (amplitude * iq * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)
