"""
RTL-SDR hardware support for SDR-MCP
"""

import asyncio
import numpy as np
import logging
from typing import Optional, Any

try:
    import rtlsdr
    RTLSDR_AVAILABLE = True
except ImportError:
    RTLSDR_AVAILABLE = False
    logging.warning("RTL-SDR library not available. Install with: pip install pyrtlsdr")

from .base import SDRDevice

logger = logging.getLogger(__name__)

class RTLSDRDevice(SDRDevice):
    """RTL-SDR hardware implementation"""
    
    def __init__(self):
        super().__init__()
        self.device_name = "RTL-SDR"
        
        # RTL-SDR specific limits
        self.min_frequency = 24e6      # 24 MHz
        self.max_frequency = 1.766e9   # 1.766 GHz
        self.min_sample_rate = 225e3   # 225 ksps
        self.max_sample_rate = 3.2e6   # 3.2 Msps
        
    async def connect(self) -> bool:
        """Connect to RTL-SDR device"""
        if not RTLSDR_AVAILABLE:
            logger.error("RTL-SDR library not available")
            return False
            
        try:
            # Find and open first available device
            self.device = rtlsdr.RtlSdr()
            
            # Set initial parameters
            self.device.sample_rate = self.sample_rate
            self.device.center_freq = self.frequency
            self.device.gain = self.gain
            
            # Get device info
            logger.info(f"Connected to RTL-SDR: {self.device.get_tuner_type()}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to RTL-SDR: {e}")
            self.device = None
            return False
            
    async def disconnect(self):
        """Disconnect from RTL-SDR device"""
        if self.device:
            try:
                self.device.close()
                self.device = None
                logger.info("Disconnected from RTL-SDR")
            except Exception as e:
                logger.error(f"Error disconnecting RTL-SDR: {e}")
                
    async def set_frequency(self, freq: float):
        """Set center frequency in Hz"""
        # Validate frequency range
        if not (self.min_frequency <= freq <= self.max_frequency):
            raise ValueError(
                f"Frequency {freq/1e6:.3f} MHz out of RTL-SDR range "
                f"({self.min_frequency/1e6:.1f}-{self.max_frequency/1e6:.1f} MHz)"
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
                
    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Read IQ samples from RTL-SDR"""
        if not self.device:
            raise RuntimeError("Device not connected")
            
        # RTL-SDR returns complex64 samples
        self.is_capturing = True
        try:
            # Use asyncio to avoid blocking
            samples = await asyncio.to_thread(
                self.device.read_samples, 
                num_samples
            )
            return samples
        finally:
            self.is_capturing = False
            
    async def get_info(self) -> dict:
        """Get extended device information"""
        info = await super().get_info()
        
        if self.device:
            try:
                info.update({
                    "tuner_type": self.device.get_tuner_type(),
                    "tuner_gains": self.device.get_gains(),
                    "freq_correction": self.device.freq_correction,
                    "sample_rate_range": f"{self.min_sample_rate/1e6:.3f}-{self.max_sample_rate/1e6:.1f} Msps",
                    "frequency_range": f"{self.min_frequency/1e6:.1f}-{self.max_frequency/1e6:.1f} MHz"
                })
            except Exception as e:
                logger.debug(f"Could not get extended info: {e}")
                
        return info

# Mock implementation for testing without hardware
class MockRTLSDRDevice(SDRDevice):
    """Mock RTL-SDR for testing"""
    
    def __init__(self):
        super().__init__()
        self.device_name = "RTL-SDR (Mock)"
        logger.info("Using mock RTL-SDR implementation")
        
    async def connect(self) -> bool:
        self.device = True  # Fake connection
        logger.info("Mock RTL-SDR connected")
        return True
        
    async def disconnect(self):
        self.device = None
        logger.info("Mock RTL-SDR disconnected")
        
    async def set_frequency(self, freq: float):
        self.frequency = freq
        logger.debug(f"Mock RTL-SDR set frequency to {freq/1e6:.3f} MHz")
        
    async def set_sample_rate(self, rate: float):
        self.sample_rate = rate
        logger.debug(f"Mock RTL-SDR set sample rate to {rate/1e6:.3f} Msps")
        
    async def set_gain(self, gain: Any):
        self.gain = gain
        logger.debug(f"Mock RTL-SDR set gain to {gain}")
        
    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Generate mock samples with some signals"""
        # Generate noise
        noise = (np.random.randn(num_samples) + 
                1j * np.random.randn(num_samples)) * 0.1
        
        # Add a few tones for testing
        t = np.arange(num_samples) / self.sample_rate
        
        # Add some signals at different frequencies
        signal = noise
        signal += 0.5 * np.exp(2j * np.pi * 1e3 * t)    # 1 kHz tone
        signal += 0.3 * np.exp(2j * np.pi * 10e3 * t)   # 10 kHz tone
        signal += 0.2 * np.exp(2j * np.pi * -5e3 * t)   # -5 kHz tone
        
        return signal.astype(np.complex64)

# Use mock if hardware not available
if not RTLSDR_AVAILABLE:
    RTLSDRDevice = MockRTLSDRDevice