"""
RTL-SDR hardware support for SDR-MCP
"""

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Optional

import numpy as np

from .base import SDRDevice

logger = logging.getLogger(__name__)

RTLSDR_IMPORT_ERROR: Optional[str] = None

# pyrtlsdr 0.3.x imports pkg_resources, which setuptools removed in version 81.
# Provide a stub so pyrtlsdr's version lookup falls through to its own fallback.
try:
    import pkg_resources  # noqa: F401
except ImportError:
    import sys
    import types

    def _no_pkg_resources(*_args, **_kwargs):
        raise ImportError("pkg_resources is not available")

    _shim = types.ModuleType("pkg_resources")
    _shim.require = _no_pkg_resources  # type: ignore[attr-defined]
    _shim.get_distribution = _no_pkg_resources  # type: ignore[attr-defined]
    sys.modules["pkg_resources"] = _shim

try:
    import rtlsdr
    from rtlsdr.librtlsdr import librtlsdr as _librtlsdr  # the ctypes handle to the C library
    RTLSDR_AVAILABLE = True
except (ImportError, AttributeError, OSError) as e:
    rtlsdr = None
    _librtlsdr = None
    RTLSDR_AVAILABLE = False
    RTLSDR_IMPORT_ERROR = str(e)
    logger.warning(f"RTL-SDR library not available: {e}. Install librtlsdr and pyrtlsdr.")

# librtlsdr rtlsdr_tuner enum
TUNER_NAMES = {0: "Unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}

RTLSDR_INSTALL_HINT = (
    "Install the RTL-SDR drivers ('brew install librtlsdr' on macOS, "
    "'sudo apt install rtl-sdr librtlsdr-dev' on Debian/Ubuntu) and the "
    "pyrtlsdr Python package."
)


class RTLSDRDevice(SDRDevice):
    """RTL-SDR hardware implementation"""

    def __init__(self, device_index: int = 0):
        super().__init__(device_index)
        self.device_name = "RTL-SDR"
        self.tuner_type: Optional[str] = None

        # RTL-SDR specific limits
        self.min_frequency = 24e6      # 24 MHz
        self.max_frequency = 1.766e9   # 1.766 GHz
        self.min_sample_rate = 225e3   # 225 ksps
        self.max_sample_rate = 3.2e6   # 3.2 Msps

        # Set safe default frequency (100 MHz FM broadcast)
        self.frequency = 100e6
        self.ppm: int = 0
        self.bias_tee: bool = False

        self._streaming = False

    @staticmethod
    def list_devices() -> list:
        """Enumerate attached RTL-SDR dongles (index, name, serial) without opening them"""
        if not RTLSDR_AVAILABLE:
            return []
        devices = []
        try:
            count = int(_librtlsdr.rtlsdr_get_device_count())
        except Exception as e:
            logger.debug(f"rtlsdr_get_device_count failed: {e}")
            return devices
        serials = []
        try:
            serials = list(rtlsdr.RtlSdr.get_device_serial_addresses())
        except Exception as e:
            logger.debug(f"Could not read RTL-SDR serials: {e}")
        for index in range(count):
            try:
                raw = _librtlsdr.rtlsdr_get_device_name(index)
                name = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            except Exception:
                name = "RTL-SDR"
            devices.append({
                "type": "rtlsdr",
                "index": index,
                "name": name,
                "serial": serials[index] if index < len(serials) else None,
            })
        return devices

    async def connect(self) -> bool:
        """Connect to RTL-SDR device"""
        if not RTLSDR_AVAILABLE:
            logger.error(f"RTL-SDR library not available: {RTLSDR_IMPORT_ERROR}")
            return False

        try:
            self.device = await asyncio.to_thread(rtlsdr.RtlSdr, self.device_index)

            # Set initial parameters
            self.device.sample_rate = self.sample_rate
            self.device.center_freq = self.frequency
            self.device.gain = self.gain
            if self.ppm:
                await self.set_freq_correction(self.ppm)
            if self.bias_tee:
                await self.set_bias_tee(True)

            try:
                code = self.device.get_tuner_type()
                self.tuner_type = TUNER_NAMES.get(int(code), str(code))
            except Exception:
                self.tuner_type = None
            logger.info(f"Connected to RTL-SDR #{self.device_index} (tuner: {self.tuner_type})")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to RTL-SDR #{self.device_index}: {e}")
            self.device = None
            return False

    async def disconnect(self):
        """Disconnect from RTL-SDR device"""
        if self.device:
            try:
                if self._streaming:
                    try:
                        self.device.cancel_read_async()
                    except Exception:
                        pass
                self.device.close()
                logger.info("Disconnected from RTL-SDR")
            except Exception as e:
                logger.error(f"Error disconnecting RTL-SDR: {e}")
            finally:
                self.device = None
                self._streaming = False
                self.is_capturing = False

    async def set_frequency(self, freq: float):
        """Set center frequency in Hz"""
        # Validate frequency range
        if not (self.min_frequency <= freq <= self.max_frequency):
            raise ValueError(
                f"Frequency {freq/1e6:.3f} MHz out of RTL-SDR range "
                f"({self.min_frequency/1e6:.1f}-{self.max_frequency/1e6:.1f} MHz)"
            )

        # E4000 tuner has a gap at 1084-1239 MHz - warn but don't fail
        if 1084e6 <= freq <= 1239e6:
            logger.warning(
                f"Frequency {freq/1e6:.1f} MHz is in E4000 L-band gap (1084-1239 MHz). "
                "The tuner may not lock properly at this frequency."
            )

        self.frequency = freq
        if self.device:
            self.device.center_freq = freq
            logger.debug(f"Set frequency to {freq/1e6:.3f} MHz")

    async def set_sample_rate(self, rate: float):
        """Set sample rate in Hz"""
        # Validate sample rate
        if not (self.min_sample_rate <= rate <= self.max_sample_rate):
            raise ValueError(
                f"Sample rate {rate/1e6:.3f} Msps out of RTL-SDR range "
                f"({self.min_sample_rate/1e6:.3f}-{self.max_sample_rate/1e6:.1f} Msps)"
            )

        self.sample_rate = rate
        if self.device:
            self.device.sample_rate = rate
            logger.debug(f"Set sample rate to {rate/1e6:.3f} Msps")

    async def set_gain(self, gain: Any):
        """Set gain (dB or 'auto')"""
        self.gain = gain
        if self.device:
            self.device.gain = gain

            # Log actual gain if manual mode
            if gain != 'auto':
                actual_gain = self.device.gain
                logger.debug(f"Set gain to {actual_gain} dB")
            else:
                logger.debug("Set gain to auto")

    async def set_freq_correction(self, ppm: int):
        """Set tuner frequency correction in PPM"""
        ppm = int(ppm)
        if not (-1000 <= ppm <= 1000):
            raise ValueError("PPM correction must be between -1000 and 1000")
        self.ppm = ppm
        if self.device:
            # Call the C function directly: pyrtlsdr's wrapper closes the device on the
            # harmless -2 ("already set to this value") return code.
            result = _librtlsdr.rtlsdr_set_freq_correction(self.device.dev_p, ppm)
            if result < 0 and result != -2:
                raise RuntimeError(f"Could not set frequency correction to {ppm} ppm (error {result})")
            logger.debug(f"Set frequency correction to {ppm} ppm")

    async def set_bias_tee(self, enabled: bool):
        """Enable or disable the bias tee (antenna port power) on RTL-SDR Blog V3/V4 dongles"""
        self.bias_tee = bool(enabled)
        if self.device:
            try:
                self.device.set_bias_tee(self.bias_tee)
            except Exception as e:
                raise RuntimeError(
                    f"Bias tee control failed: {e}. It needs a dongle with a bias tee (RTL-SDR Blog V3/V4) "
                    "and an librtlsdr build that supports it."
                )
            logger.info(f"Bias tee {'enabled' if self.bias_tee else 'disabled'}")

    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Read IQ samples from RTL-SDR (one-shot synchronous read)"""
        if not self.device:
            raise RuntimeError("Device not connected")
        if self._streaming:
            raise RuntimeError("Device is streaming; stop the active capture before reading")

        self.is_capturing = True
        try:
            # Use asyncio to avoid blocking
            samples = await asyncio.to_thread(self.device.read_samples, num_samples)
            return samples
        finally:
            self.is_capturing = False

    async def stream(self, chunk_size: int) -> AsyncIterator[np.ndarray]:
        """Continuously yield IQ chunks using librtlsdr's asynchronous USB reads.

        Unlike repeated read_samples calls, the device keeps streaming into a
        queue between consumer iterations, so recordings have no gaps as long as
        the consumer keeps up on average. If it falls behind, the oldest queued
        chunks are dropped and the drop count is logged.
        """
        if not self.device:
            raise RuntimeError("Device not connected")
        if self._streaming:
            raise RuntimeError("Device is already streaming")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        dropped = 0

        def _enqueue(samples):
            nonlocal dropped
            if queue.full():
                try:
                    queue.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(samples)

        def _callback(samples, _context):
            # Runs on librtlsdr's USB thread; hand off to the event loop thread.
            try:
                loop.call_soon_threadsafe(_enqueue, np.asarray(samples, dtype=np.complex64))
            except RuntimeError:
                pass  # event loop closed

        self._streaming = True
        self.is_capturing = True
        reader = asyncio.ensure_future(
            asyncio.to_thread(self.device.read_samples_async, _callback, int(chunk_size))
        )
        getter: Optional[asyncio.Future] = None
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                # Reader finished on its own: surface its error, or stop cleanly.
                reader.result()
                break
        finally:
            if getter is not None and not getter.done():
                getter.cancel()
            try:
                self.device.cancel_read_async()
            except Exception:
                pass
            try:
                await asyncio.wait_for(reader, timeout=3.0)
            except Exception:
                pass
            if self.device is not None:
                try:
                    # Required before the next synchronous read after an async session.
                    _librtlsdr.rtlsdr_reset_buffer(self.device.dev_p)
                except Exception:
                    pass
            self._streaming = False
            self.is_capturing = False
            if dropped:
                logger.warning(f"RTL-SDR stream dropped {dropped} chunk(s); consumer fell behind")

    async def get_info(self) -> dict:
        """Get extended device information"""
        info = await super().get_info()

        if self.device:
            try:
                info.update({
                    "tuner_type": self.tuner_type,
                    "tuner_gains": self.device.get_gains(),
                    "freq_correction": self.ppm,
                    "bias_tee": self.bias_tee,
                    "sample_rate_range": f"{self.min_sample_rate/1e6:.3f}-{self.max_sample_rate/1e6:.1f} Msps",
                    "frequency_range": f"{self.min_frequency/1e6:.1f}-{self.max_frequency/1e6:.1f} MHz"
                })
            except Exception as e:
                logger.debug(f"Could not get extended info: {e}")

        return info


class MockRTLSDRDevice(SDRDevice):
    """Mock RTL-SDR for offline testing. Never substituted for real hardware automatically.

    Enabled for the server with AETHERLINK_MOCK_SDR=1. If AETHERLINK_MOCK_IQ_FILE
    points at a .npy file of complex samples, reads play it back in a loop (at the
    device sample rate) so decoders can be exercised end to end without hardware.
    """

    def __init__(self, device_index: int = 0, iq_file: Optional[str] = None):
        super().__init__(device_index)
        self.device_name = "RTL-SDR (Mock)"
        self.frequency = 100e6
        self.min_frequency = 24e6
        self.max_frequency = 1.766e9
        self.min_sample_rate = 225e3
        self.max_sample_rate = 3.2e6
        self.ppm = 0
        self.bias_tee = False
        self._iq: Optional[np.ndarray] = None
        self._pos = 0
        path = iq_file or os.environ.get("AETHERLINK_MOCK_IQ_FILE")
        if path and os.path.exists(path):
            try:
                self._iq = np.load(path).astype(np.complex64)
                logger.info(f"Mock RTL-SDR playing back {len(self._iq)} samples from {path}")
            except Exception as e:
                logger.warning(f"Could not load mock IQ file {path}: {e}")
        logger.info("Using mock RTL-SDR implementation")

    @staticmethod
    def list_devices() -> list:
        return [{"type": "rtlsdr", "index": 0, "name": "RTL-SDR (Mock)", "serial": "MOCK"}]

    async def connect(self) -> bool:
        self.device = True  # Fake connection
        self.tuner_type = "Mock"
        logger.info("Mock RTL-SDR connected")
        return True

    async def disconnect(self):
        self.device = None
        logger.info("Mock RTL-SDR disconnected")

    async def set_frequency(self, freq: float):
        if not (self.min_frequency <= freq <= self.max_frequency):
            raise ValueError(f"Frequency {freq/1e6:.3f} MHz out of RTL-SDR range")
        self.frequency = freq
        logger.debug(f"Mock RTL-SDR set frequency to {freq/1e6:.3f} MHz")

    async def set_sample_rate(self, rate: float):
        if not (self.min_sample_rate <= rate <= self.max_sample_rate):
            raise ValueError(f"Sample rate {rate/1e6:.3f} Msps out of RTL-SDR range")
        self.sample_rate = rate
        logger.debug(f"Mock RTL-SDR set sample rate to {rate/1e6:.3f} Msps")

    async def set_gain(self, gain: Any):
        self.gain = gain
        logger.debug(f"Mock RTL-SDR set gain to {gain}")

    async def set_freq_correction(self, ppm: int):
        self.ppm = int(ppm)

    async def set_bias_tee(self, enabled: bool):
        self.bias_tee = bool(enabled)

    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Play back the IQ file, or generate noise with a few tones, paced like real hardware"""
        await asyncio.sleep(num_samples / self.sample_rate)
        if self._iq is not None and len(self._iq):
            out = np.empty(num_samples, dtype=np.complex64)
            filled = 0
            while filled < num_samples:
                take = min(num_samples - filled, len(self._iq) - self._pos)
                out[filled:filled + take] = self._iq[self._pos:self._pos + take]
                filled += take
                self._pos = (self._pos + take) % len(self._iq)
            return out
        noise = (np.random.randn(num_samples) + 1j * np.random.randn(num_samples)) * 0.1
        t = np.arange(num_samples) / self.sample_rate
        signal = noise
        signal += 0.5 * np.exp(2j * np.pi * 100e3 * t)   # +100 kHz tone
        signal += 0.3 * np.exp(2j * np.pi * 250e3 * t)   # +250 kHz tone
        signal += 0.2 * np.exp(2j * np.pi * -400e3 * t)  # -400 kHz tone
        return signal.astype(np.complex64)
