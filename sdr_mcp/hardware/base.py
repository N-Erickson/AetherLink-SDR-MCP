"""
Base class for SDR hardware devices
"""

from abc import ABC, abstractmethod
from typing import Any, Optional
import numpy as np

class SDRDevice(ABC):
    """Abstract base class for SDR hardware control"""
    
    def __init__(self):
        self.device = None
        self.device_name = "Unknown"
        self.frequency = 0
        self.sample_rate = 2.048e6  # Default 2.048 Msps
        self.gain = 'auto'
        self.is_capturing = False
        
    @abstractmethod
    async def connect(self) -> bool:
        """Connect to SDR hardware"""
        pass
        
    @abstractmethod
    async def disconnect(self):
        """Disconnect from SDR hardware"""
        pass
        
    @abstractmethod
    async def set_frequency(self, freq: float):
        """Set center frequency in Hz"""
        pass
        
    @abstractmethod
    async def set_sample_rate(self, rate: float):
        """Set sample rate in Hz"""
        pass
        
    @abstractmethod
    async def set_gain(self, gain: Any):
        """Set gain (format depends on device)"""
        pass
        
    @abstractmethod
    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Read IQ samples from device"""
        pass
        
    async def get_info(self) -> dict:
        """Get device information"""
        return {
            "device_name": self.device_name,
            "connected": self.device is not None,
            "frequency": self.frequency,
            "sample_rate": self.sample_rate,
            "gain": self.gain,
            "is_capturing": self.is_capturing
        }