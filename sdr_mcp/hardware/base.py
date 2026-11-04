"""
Base class for SDR hardware devices
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator
import numpy as np


class SDRDevice(ABC):
    """Abstract base class for SDR hardware control"""

    def __init__(self, device_index: int = 0):
        self.device = None
        self.device_name = "Unknown"
        self.device_index = device_index
        self.frequency = 0
        self.sample_rate = 2.048e6  # Default 2.048 Msps
        self.gain = 'auto'
        self.is_capturing = False

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to SDR hardware"""

    @abstractmethod
    async def disconnect(self):
        """Disconnect from SDR hardware"""

    @abstractmethod
    async def set_frequency(self, freq: float):
        """Set center frequency in Hz"""

    @abstractmethod
    async def set_sample_rate(self, rate: float):
        """Set sample rate in Hz"""

    @abstractmethod
    async def set_gain(self, gain: Any):
        """Set gain (format depends on device)"""

    @abstractmethod
    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Read IQ samples from device"""

    async def stream(self, chunk_size: int) -> AsyncIterator[np.ndarray]:
        """Yield consecutive chunks of IQ samples until the consumer stops iterating.

        The default implementation repeatedly calls read_samples, which may drop
        samples between calls on devices that use synchronous reads. Drivers that
        support continuous capture override this to deliver gap-free data.
        """
        while True:
            samples = await self.read_samples(chunk_size)
            # Always give the event loop a turn, even if read_samples completed synchronously.
            await asyncio.sleep(0)
            yield samples

    async def set_freq_correction(self, ppm: int):
        """Set frequency correction in PPM (not every device supports this)"""
        raise NotImplementedError(f"{self.device_name} does not support PPM correction")

    async def set_bias_tee(self, enabled: bool):
        """Enable or disable antenna port power (bias tee)"""
        raise NotImplementedError(f"{self.device_name} does not support a bias tee")

    @staticmethod
    def list_devices() -> list:
        """Enumerate attached devices of this type without opening them"""
        return []

    async def get_info(self) -> dict:
        """Get device information"""
        return {
            "device_name": self.device_name,
            "device_index": self.device_index,
            "connected": self.device is not None,
            "frequency": self.frequency,
            "sample_rate": self.sample_rate,
            "gain": self.gain,
            "is_capturing": self.is_capturing
        }
