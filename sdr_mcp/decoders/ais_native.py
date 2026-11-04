"""
Native AIS receiver: wide IQ in, NMEA !AIVDM sentences out, no external programs.

Both channels (161.975 and 162.025 MHz) are decoded from one capture centered
on 162.000 MHz. Per channel: Channelizer -> FM discriminator (GMSK) -> bit
slicer at 9600 baud -> NRZI -> HDLC deframing with bit de-stuffing -> CRC-16
check -> 6-bit armoring into NMEA sentences, which the regular AISDecoder
(pyais) then parses. The encoder at the bottom exists for tests.
"""

import logging
from typing import Callable, Dict, List, Optional

import numpy as np
from scipy import signal as sg

from ..analysis.channelizer import BitSlicer, Channelizer, DCTracker, FMDiscriminator

logger = logging.getLogger(__name__)

AIS_CENTER_HZ = 162.0e6
AIS_CHANNELS = {"A": 161.975e6, "B": 162.025e6}
AIS_BAUD = 9600
GMSK_DEVIATION_HZ = 2400.0          # modulation index 0.5
HDLC_FLAG = 0x7E
MAX_FRAME_BITS = 1200               # longest AIS message is 1008 bits + stuffing
PPP_GOOD_FCS = 0xF0B8               # RFC 1662 residue when the FCS is appended correctly


# ---------------------------------------------------------------- HDLC / CRC

def crc16_x25(data: bytes, init: int = 0xFFFF) -> int:
    """Reflected CRC-16 (poly 0x1021) as used for the HDLC frame check sequence."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def bits_to_bytes_lsb_first(bits: List[int]) -> bytes:
    out = bytearray(len(bits) // 8)
    for i, bit in enumerate(bits[:len(out) * 8]):
        if bit:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def armor_payload(payload: bytes, nbits: Optional[int] = None):
    """AIS binary payload -> (6-bit ASCII armored string, fill bits)."""
    bits = []
    for byte in payload:
        bits.extend((byte >> k) & 1 for k in range(7, -1, -1))
    if nbits is not None:
        bits = bits[:nbits]
    fill = (-len(bits)) % 6
    bits.extend([0] * fill)
    chars = []
    for i in range(0, len(bits), 6):
        value = 0
        for bit in bits[i:i + 6]:
            value = (value << 1) | bit
        chars.append(chr(value + 48 if value < 40 else value + 56))
    return "".join(chars), fill


def nmea_checksum(body: str) -> str:
    csum = 0
    for ch in body:
        csum ^= ord(ch)
    return f"{csum:02X}"


def build_sentences(armored: str, fill: int, channel: str, seq_id: int, max_chars: int = 56) -> List[str]:
    parts = [armored[i:i + max_chars] for i in range(0, len(armored), max_chars)] or [""]
    total = len(parts)
    seq = str(seq_id % 10) if total > 1 else ""
    sentences = []
    for index, part in enumerate(parts, 1):
        fill_bits = fill if index == total else 0
        body = f"AIVDM,{total},{index},{seq},{channel},{part},{fill_bits}"
        sentences.append(f"!{body}*{nmea_checksum(body)}")
    return sentences


class HDLCDeframer:
    """Finds flag-delimited frames in an NRZI-decoded bit stream and validates their FCS."""

    def __init__(self, on_frame: Callable[[bytes], None]):
        self.on_frame = on_frame
        self.register = 0
        self.in_frame = False
        self.raw: List[int] = []
        self.frames = 0
        self.crc_errors = 0
        self.oversize = 0

    def feed(self, bits: np.ndarray):
        for b in bits:
            bit = int(b)
            self.register = ((self.register << 1) | bit) & 0xFF
            if self.in_frame:
                self.raw.append(bit)
                if self.register == HDLC_FLAG:
                    frame_bits = self.raw[:-8]
                    self.raw = []
                    if frame_bits:
                        self._finish(frame_bits)
                elif len(self.raw) > MAX_FRAME_BITS:
                    self.oversize += 1
                    self.in_frame = False
                    self.raw = []
            elif self.register == HDLC_FLAG:
                self.in_frame = True
                self.raw = []

    def _finish(self, stuffed: List[int]):
        bits: List[int] = []
        ones = 0
        for bit in stuffed:
            if ones == 5:
                ones = 0
                if bit == 0:
                    continue        # stuffed zero
            if bit:
                ones += 1
            else:
                ones = 0
            bits.append(bit)
        if len(bits) < 24 or len(bits) % 8:
            self.crc_errors += 1
            return
        data = bits_to_bytes_lsb_first(bits)
        if crc16_x25(data) != PPP_GOOD_FCS:
            self.crc_errors += 1
            return
        self.frames += 1
        self.on_frame(data[:-2])


# ---------------------------------------------------------------- per-channel demodulator

class AISChannel:
    """Burst-oriented GMSK demodulation and framing for one AIS channel.

    AIS is TDMA: each transmission is a 27-ms burst. The channel envelope is
    squelched against a tracked noise floor to find bursts; each burst is then
    demodulated on its own, so the carrier offset and bit clock are estimated
    from that burst alone and idle-channel noise never disturbs them.
    """

    CHANNEL_BANDWIDTH_HZ = 20e3
    TARGET_RATE = 48000.0
    MIN_BURST_BITS = 64
    MAX_BURST_S = 0.35
    SQUELCH_RATIO = 3.0

    def __init__(self, sample_rate: float, offset_hz: float, name: str,
                 on_sentence: Callable[[str], None],
                 on_frequency_error: Optional[Callable[[float], None]] = None):
        self.name = name
        self.on_sentence = on_sentence
        self.on_frequency_error = on_frequency_error
        self.nominal_offset = float(offset_hz)
        self.channelizer = Channelizer(sample_rate, offset_hz, self.CHANNEL_BANDWIDTH_HZ, self.TARGET_RATE)
        self.rate = self.channelizer.rate
        self.spb = self.rate / AIS_BAUD
        self._data_sos = sg.butter(4, 6000.0, btype="low", fs=self.rate, output="sos")
        self.noise: Optional[float] = None
        self.pending: Optional[np.ndarray] = None
        self.seq = 0
        self.bursts = 0
        self.frames = 0
        self.crc_errors = 0
        self.oversize = 0
        self.messages = 0

    def set_frequency_correction(self, hz: float):
        self.channelizer.offset_hz = self.nominal_offset + hz

    def feed_iq(self, samples: np.ndarray):
        base = self.channelizer.process(samples)
        if len(base) == 0:
            return
        envelope = np.abs(base)
        floor = float(np.percentile(envelope, 20))
        if self.noise is None or self.noise <= 0:
            self.noise = max(floor, 1e-9)
        elif floor < 2.0 * self.noise:
            self.noise = 0.9 * self.noise + 0.1 * floor
        else:
            self.noise *= 1.02          # creep up slowly if the floor really rose
        threshold = self.SQUELCH_RATIO * self.noise

        k = max(1, int(round(self.spb)))
        smooth = np.convolve(envelope, np.ones(k) / k, mode="same")
        active = smooth > threshold
        if self.pending is not None:
            base = np.concatenate((self.pending, base))
            active = np.concatenate((np.ones(len(self.pending), dtype=bool), active))
            self.pending = None

        n = len(base)
        margin = int(2 * self.spb)
        edges = np.diff(active.astype(np.int8), prepend=0, append=0)
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        for s_, e_ in zip(starts, ends):
            if e_ >= n:
                # Burst continues into the next chunk
                if (n - s_) < self.MAX_BURST_S * self.rate:
                    self.pending = base[max(0, s_ - margin):]
                else:
                    self.oversize += 1
                break
            if (e_ - s_) < self.MIN_BURST_BITS * self.spb:
                continue
            self._process_burst(base[max(0, s_ - margin):min(n, e_ + margin)], margin)

    def _process_burst(self, x: np.ndarray, margin: int):
        self.bursts += 1
        disc = FMDiscriminator(self.rate).process(x)
        core = disc[margin:len(disc) - margin] if len(disc) > 2 * margin + 8 else disc
        offset = float(np.mean(core))                      # carrier error of this burst
        zi = sg.sosfilt_zi(self._data_sos) * 0.0
        y, _ = sg.sosfilt(self._data_sos, disc - offset, zi=zi)
        slicer = BitSlicer(self.rate, AIS_BAUD, loop_gain=0.5)
        levels = np.concatenate((slicer.process(y, weights=np.abs(x)), slicer.flush()))
        if len(levels) < self.MIN_BURST_BITS:
            return
        # NRZI: a level change encodes 0, no change encodes 1
        bits = (levels[1:] == levels[:-1]).astype(np.int8)
        deframer = HDLCDeframer(lambda payload: self._on_frame(payload, offset))
        deframer.feed(bits)
        self.frames += deframer.frames
        self.crc_errors += deframer.crc_errors

    def _on_frame(self, payload: bytes, offset: float):
        if len(payload) < 4:
            return
        armored, fill = armor_payload(payload)
        self.seq += 1
        self.messages += 1
        for sentence in build_sentences(armored, fill, self.name, self.seq):
            self.on_sentence(sentence)
        if self.on_frequency_error:
            self.on_frequency_error(offset)

    def stats(self) -> Dict[str, int]:
        return {"bursts": self.bursts, "frames": self.frames, "crc_errors": self.crc_errors,
                "oversize": self.oversize, "messages": self.messages}


class AISReceiver:
    """Decodes both AIS channels from a capture centered on AIS_CENTER_HZ.

    Frequency errors measured on successfully decoded bursts feed a shared
    correction, so after the first good frame both channels are centered.
    """

    MAX_CORRECTION_HZ = 12e3

    def __init__(self, sample_rate: float, on_sentence: Callable[[str], None],
                 center_hz: float = AIS_CENTER_HZ):
        self.frequency_error_hz = 0.0
        self.channels = {
            name: AISChannel(sample_rate, freq - center_hz, name, on_sentence, self._on_frequency_error)
            for name, freq in AIS_CHANNELS.items()
        }
        self.chunks = 0

    def _on_frequency_error(self, residual_hz: float):
        corrected = self.frequency_error_hz + 0.5 * residual_hz
        self.frequency_error_hz = max(-self.MAX_CORRECTION_HZ, min(self.MAX_CORRECTION_HZ, corrected))
        for ch in self.channels.values():
            ch.set_frequency_correction(self.frequency_error_hz)

    def feed_iq(self, samples: np.ndarray):
        for ch in self.channels.values():
            ch.feed_iq(samples)
        self.chunks += 1

    def stats(self) -> Dict[str, object]:
        return {
            "frequency_error_hz": round(self.frequency_error_hz),
            "channels": {name: ch.stats() for name, ch in self.channels.items()},
        }


# ---------------------------------------------------------------- encoder (for tests)

def unarmor_payload(armored: str, fill: int) -> bytes:
    bits: List[int] = []
    for ch in armored:
        value = ord(ch) - 48
        if value > 40:
            value -= 8
        bits.extend((value >> k) & 1 for k in range(5, -1, -1))
    if fill:
        bits = bits[:-fill]
    while len(bits) % 8:
        bits.append(0)
    out = bytearray(len(bits) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i >> 3] |= 1 << (7 - (i & 7))
    return bytes(out)


def hdlc_frame_bits(payload: bytes, training_bits: int = 24) -> List[int]:
    """Training sequence, flag, bit-stuffed payload + FCS (LSB first), flag, trailing idle."""
    fcs = crc16_x25(payload) ^ 0xFFFF
    data = payload + bytes([fcs & 0xFF, fcs >> 8])
    body: List[int] = []
    for byte in data:
        body.extend((byte >> k) & 1 for k in range(8))
    stuffed: List[int] = []
    ones = 0
    for bit in body:
        stuffed.append(bit)
        if bit:
            ones += 1
            if ones == 5:
                stuffed.append(0)
                ones = 0
        else:
            ones = 0
    flag = [(HDLC_FLAG >> k) & 1 for k in range(8)]
    return [0, 1] * (training_bits // 2) + flag + stuffed + flag + [0] * 32


def nrzi_encode(bits: List[int]) -> List[int]:
    level = 0
    out = []
    for bit in bits:
        if bit == 0:
            level ^= 1
        out.append(level)
    return out


def modulate_gmsk(levels: List[int], sample_rate: float, offset_hz: float = 0.0, bt: float = 0.4,
                  amplitude: float = 1.0) -> np.ndarray:
    """GMSK (BT 0.4, h 0.5) of NRZI levels, placed at offset_hz inside a capture."""
    spb = sample_rate / AIS_BAUD
    n = int(len(levels) * spb)
    bit_index = np.minimum((np.arange(n) / spb).astype(int), len(levels) - 1)
    nrz = np.where(np.asarray(levels)[bit_index] == 1, 1.0, -1.0)
    # Gaussian pulse shaping
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt * AIS_BAUD)
    taps_t = np.arange(-int(3 * spb), int(3 * spb) + 1) / sample_rate
    gauss = np.exp(-taps_t ** 2 / (2 * sigma ** 2))
    gauss /= gauss.sum()
    shaped = np.convolve(nrz, gauss, mode="same")
    freq = shaped * GMSK_DEVIATION_HZ + offset_hz
    phase = 2 * np.pi * np.cumsum(freq) / sample_rate
    return (amplitude * np.exp(1j * phase)).astype(np.complex64)
