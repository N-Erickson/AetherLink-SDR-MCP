"""
HackRF hardware support for SDR-MCP
"""

import asyncio
import numpy as np
from typing import Optional, Dict, Any, Callable
import logging
from enum import Enum
from dataclasses import dataclass

try:
    import libhackrf
    HACKRF_AVAILABLE = True
except ImportError:
    HACKRF_AVAILABLE = False
    logging.warning("HackRF library not available. Install with: pip install pyhackrf")

from .base import SDRDevice

logger = logging.getLogger(__name__)

class HackRFMode(Enum):
    """HackRF operating modes"""
    RECEIVE = "receive"
    TRANSMIT = "transmit"
    IDLE = "idle"

@dataclass
class HackRFConfig:
    """HackRF configuration parameters"""
    # Frequency range: 1 MHz - 6 GHz
    min_frequency: float = 1e6
    max_frequency: float = 6e9
    
    # Sample rate range: 2-20 Msps
    min_sample_rate: float = 2e6
    max_sample_rate: float = 20e6
    
    # Gain settings
    lna_gain_steps: list = None  # 0, 8, 16, 24, 32, 40 dB
    vga_gain_steps: list = None  # 0-62 dB in 2dB steps
    tx_vga_gain_steps: list = None  # 0-47 dB in 1dB steps
    
    # Amp enable
    amp_enable: bool = False  # 14dB amp
    
    def __post_init__(self):
        if self.lna_gain_steps is None:
            self.lna_gain_steps = [0, 8, 16, 24, 32, 40]
        if self.vga_gain_steps is None:
            self.vga_gain_steps = list(range(0, 63, 2))
        if self.tx_vga_gain_steps is None:
            self.tx_vga_gain_steps = list(range(0, 48))

class HackRFDevice(SDRDevice):
    """HackRF One hardware implementation"""
    
    def __init__(self, device_index: int = 0):
        super().__init__()
        self.device_name = "HackRF One"
        self.device_index = device_index
        self.config = HackRFConfig()
        self.mode = HackRFMode.IDLE
        
        # HackRF specific parameters
        self.lna_gain = 16  # dB
        self.vga_gain = 20  # dB
        self.tx_vga_gain = 0  # dB
        self.amp_enable = False
        
        # Callbacks
        self.rx_callback = None
        self.tx_callback = None
        
        # Buffers
        self.rx_buffer = asyncio.Queue(maxsize=100)
        self.tx_buffer = asyncio.Queue(maxsize=100)
        
    async def connect(self) -> bool:
        """Connect to HackRF device"""
        if not HACKRF_AVAILABLE:
            logger.error("HackRF library not available")
            return False
            
        try:
            # Initialize HackRF library
            result = libhackrf.hackrf_init()
            if result != 0:
                logger.error(f"Failed to initialize HackRF library: {result}")
                return False
                
            # Open device
            self.device = libhackrf.hackrf_open_by_serial(None)  # Open first device
            if not self.device:
                logger.error("Failed to open HackRF device")
                return False
                
            # Get device info
            info = self._get_device_info()
            logger.info(f"Connected to HackRF: {info}")
            
            # Set initial configuration
            await self.set_sample_rate(self.sample_rate)
            await self.set_frequency(self.frequency)
            await self._update_gains()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to HackRF: {e}")
            return False
            
    async def disconnect(self):
        """Disconnect from HackRF device"""
        if self.device:
            try:
                # Stop streaming if active
                if self.mode != HackRFMode.IDLE:
                    await self.stop_streaming()
                    
                # Close device
                libhackrf.hackrf_close(self.device)
                self.device = None
                
                # Exit library
                libhackrf.hackrf_exit()
                
            except Exception as e:
                logger.error(f"Error disconnecting HackRF: {e}")
                
    async def set_frequency(self, freq: float):
        """Set center frequency in Hz"""
        if not self.device:
            raise RuntimeError("Device not connected")
            
        # Validate frequency
        if not (self.config.min_frequency <= freq <= self.config.max_frequency):
            raise ValueError(f"Frequency {freq/1e6:.3f} MHz out of range")
            
        self.frequency = freq
        
        # Set frequency on device
        result = libhackrf.hackrf_set_freq(self.device, int(freq))
        if result != 0:
            raise RuntimeError(f"Failed to set frequency: {result}")
            
        logger.debug(f"Set frequency to {freq/1e6:.3f} MHz")
        
    async def set_sample_rate(self, rate: float):
        """Set sample rate in Hz"""
        if not self.device:
            raise RuntimeError("Device not connected")
            
        # Validate sample rate
        if not (self.config.min_sample_rate <= rate <= self.config.max_sample_rate):
            raise ValueError(f"Sample rate {rate/1e6:.3f} Msps out of range")
            
        self.sample_rate = rate
        
        # Set sample rate on device
        result = libhackrf.hackrf_set_sample_rate(self.device, rate)
        if result != 0:
            raise RuntimeError(f"Failed to set sample rate: {result}")
            
        # Also set baseband filter bandwidth (typically 0.75 * sample_rate)
        bandwidth = int(0.75 * rate)
        result = libhackrf.hackrf_set_baseband_filter_bandwidth(self.device, bandwidth)
        if result != 0:
            logger.warning(f"Failed to set filter bandwidth: {result}")
            
        logger.debug(f"Set sample rate to {rate/1e6:.3f} Msps")
        
    async def set_gain(self, gain: Any):
        """Set gain (dict with lna_gain, vga_gain, or 'auto')"""
        if isinstance(gain, str) and gain.lower() == 'auto':
            # Auto gain for HackRF (middle values)
            self.lna_gain = 24
            self.vga_gain = 30
            self.gain = 'auto'
        elif isinstance(gain, dict):
            # Manual gain settings
            if 'lna_gain' in gain:
                # Find closest valid LNA gain
                self.lna_gain = min(self.config.lna_gain_steps, 
                                   key=lambda x: abs(x - gain['lna_gain']))
            if 'vga_gain' in gain:
                # Find closest valid VGA gain
                self.vga_gain = min(self.config.vga_gain_steps,
                                   key=lambda x: abs(x - gain['vga_gain']))
            if 'amp_enable' in gain:
                self.amp_enable = bool(gain['amp_enable'])
            self.gain = f"LNA:{self.lna_gain} VGA:{self.vga_gain}"
        elif isinstance(gain, (int, float)):
            # Single gain value - distribute between LNA and VGA
            total_gain = float(gain)
            self.lna_gain = min(self.config.lna_gain_steps,
                               key=lambda x: abs(x - min(total_gain, 40)))
            self.vga_gain = max(0, min(62, int(total_gain - self.lna_gain)))
            self.gain = f"Total:{total_gain}"
        else:
            raise ValueError(f"Invalid gain format: {gain}")
            
        await self._update_gains()
        
    async def _update_gains(self):
        """Update gain settings on device"""
        if not self.device:
            return
            
        # Set LNA gain
        result = libhackrf.hackrf_set_lna_gain(self.device, self.lna_gain)
        if result != 0:
            logger.warning(f"Failed to set LNA gain: {result}")
            
        # Set VGA gain
        result = libhackrf.hackrf_set_vga_gain(self.device, self.vga_gain)
        if result != 0:
            logger.warning(f"Failed to set VGA gain: {result}")
            
        # Set amp enable
        result = libhackrf.hackrf_set_amp_enable(self.device, 1 if self.amp_enable else 0)
        if result != 0:
            logger.warning(f"Failed to set amp enable: {result}")
            
        logger.debug(f"Set gains - LNA:{self.lna_gain} VGA:{self.vga_gain} Amp:{self.amp_enable}")
        
    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Read IQ samples from HackRF"""
        if not self.device:
            raise RuntimeError("Device not connected")
            
        # Start RX if not already running
        if self.mode != HackRFMode.RECEIVE:
            await self.start_rx()
            
        # Calculate how many buffers we need
        samples_per_buffer = 262144 // 2  # HackRF buffer size / 2 (I+Q)
        num_buffers = (num_samples + samples_per_buffer - 1) // samples_per_buffer
        
        # Collect samples
        samples = []
        samples_collected = 0
        
        for _ in range(num_buffers):
            # Get buffer from queue
            buffer = await self.rx_buffer.get()
            
            # Convert from int8 to complex float
            iq_samples = self._convert_samples(buffer)
            samples.append(iq_samples)
            samples_collected += len(iq_samples)
            
            if samples_collected >= num_samples:
                break
                
        # Concatenate and trim to requested size
        all_samples = np.concatenate(samples)
        return all_samples[:num_samples]
        
    async def write_samples(self, samples: np.ndarray):
        """Write IQ samples to HackRF for transmission"""
        if not self.device:
            raise RuntimeError("Device not connected")
            
        # Validate TX parameters
        if self.frequency < 10e6:
            logger.warning("Transmitting below 10 MHz may damage the HackRF!")
            
        # Start TX if not already running
        if self.mode != HackRFMode.TRANSMIT:
            await self.start_tx()
            
        # Convert samples to int8 format
        tx_data = self._convert_to_int8(samples)
        
        # Send in chunks
        chunk_size = 262144  # HackRF buffer size
        for i in range(0, len(tx_data), chunk_size):
            chunk = tx_data[i:i+chunk_size]
            await self.tx_buffer.put(chunk)
            
    def _convert_samples(self, buffer: bytes) -> np.ndarray:
        """Convert HackRF int8 samples to complex float"""
        # HackRF provides interleaved I/Q as signed 8-bit integers
        iq_array = np.frombuffer(buffer, dtype=np.int8)
        
        # Separate I and Q, convert to float and scale
        i = iq_array[0::2].astype(np.float32) / 127.0
        q = iq_array[1::2].astype(np.float32) / 127.0
        
        # Combine into complex samples
        return i + 1j * q
        
    def _convert_to_int8(self, samples: np.ndarray) -> np.ndarray:
        """Convert complex float samples to HackRF int8 format"""
        # Scale and convert to int8
        i = np.clip(samples.real * 127, -127, 127).astype(np.int8)
        q = np.clip(samples.imag * 127, -127, 127).astype(np.int8)
        
        # Interleave I and Q
        iq_data = np.empty(len(samples) * 2, dtype=np.int8)
        iq_data[0::2] = i
        iq_data[1::2] = q
        
        return iq_data
        
    async def start_rx(self):
        """Start receive mode"""
        if self.mode == HackRFMode.RECEIVE:
            return
            
        # Stop any current streaming
        if self.mode != HackRFMode.IDLE:
            await self.stop_streaming()
            
        # Set RX callback
        def rx_callback(hackrf_transfer):
            # Queue the buffer for processing
            buffer = bytes(hackrf_transfer.buffer[:hackrf_transfer.valid_length])
            try:
                self.rx_buffer.put_nowait(buffer)
            except asyncio.QueueFull:
                # Drop oldest buffer
                try:
                    self.rx_buffer.get_nowait()
                    self.rx_buffer.put_nowait(buffer)
                except:
                    pass
            return 0
            
        # Start RX streaming
        self.rx_callback = libhackrf.hackrf_rx_callback(rx_callback)
        result = libhackrf.hackrf_start_rx(self.device, self.rx_callback, None)
        if result != 0:
            raise RuntimeError(f"Failed to start RX: {result}")
            
        self.mode = HackRFMode.RECEIVE
        self.is_capturing = True
        logger.info("Started HackRF RX mode")
        
    async def start_tx(self):
        """Start transmit mode"""
        if self.mode == HackRFMode.TRANSMIT:
            return
            
        # Stop any current streaming
        if self.mode != HackRFMode.IDLE:
            await self.stop_streaming()
            
        # Set TX VGA gain
        result = libhackrf.hackrf_set_txvga_gain(self.device, self.tx_vga_gain)
        if result != 0:
            logger.warning(f"Failed to set TX VGA gain: {result}")
            
        # Set TX callback
        def tx_callback(hackrf_transfer):
            # Get next buffer from queue
            try:
                buffer = self.tx_buffer.get_nowait()
                hackrf_transfer.buffer[:len(buffer)] = buffer
                hackrf_transfer.valid_length = len(buffer)
            except asyncio.QueueEmpty:
                # No data available - send zeros
                hackrf_transfer.valid_length = 0
            return 0
            
        # Start TX streaming
        self.tx_callback = libhackrf.hackrf_tx_callback(tx_callback)
        result = libhackrf.hackrf_start_tx(self.device, self.tx_callback, None)
        if result != 0:
            raise RuntimeError(f"Failed to start TX: {result}")
            
        self.mode = HackRFMode.TRANSMIT
        self.is_capturing = True
        logger.info("Started HackRF TX mode")
        
    async def stop_streaming(self):
        """Stop current streaming mode"""
        if self.mode == HackRFMode.IDLE:
            return
            
        # Stop streaming
        if self.mode == HackRFMode.RECEIVE:
            result = libhackrf.hackrf_stop_rx(self.device)
        else:  # TRANSMIT
            result = libhackrf.hackrf_stop_tx(self.device)
            
        if result != 0:
            logger.warning(f"Failed to stop streaming: {result}")
            
        self.mode = HackRFMode.IDLE
        self.is_capturing = False
        
        # Clear buffers
        while not self.rx_buffer.empty():
            self.rx_buffer.get_nowait()
        while not self.tx_buffer.empty():
            self.tx_buffer.get_nowait()
            
        logger.info("Stopped HackRF streaming")
        
    def _get_device_info(self) -> Dict[str, Any]:
        """Get HackRF device information"""
        if not self.device:
            return {}
            
        info = {
            "device": "HackRF One",
            "board_id": "Unknown",
            "version": "Unknown",
            "serial": "Unknown"
        }
        
        try:
            # Get board ID
            board_id = libhackrf.hackrf_board_id_read(self.device)
            info["board_id"] = f"{board_id:#04x}"
            
            # Get version string
            version = libhackrf.hackrf_version_string_read(self.device)
            info["version"] = version
            
            # Get serial number
            serial = libhackrf.hackrf_board_serial_read(self.device)
            info["serial"] = f"{serial:#016x}"
            
        except Exception as e:
            logger.debug(f"Error getting device info: {e}")
            
        return info
        
    async def set_tx_gain(self, gain: int):
        """Set transmit VGA gain (0-47 dB)"""
        if not (0 <= gain <= 47):
            raise ValueError(f"TX VGA gain must be 0-47 dB, got {gain}")
            
        self.tx_vga_gain = gain
        
        if self.device and self.mode == HackRFMode.TRANSMIT:
            result = libhackrf.hackrf_set_txvga_gain(self.device, gain)
            if result != 0:
                logger.warning(f"Failed to set TX VGA gain: {result}")
                
    async def get_temperature(self) -> float:
        """Get HackRF temperature in Celsius (if supported)"""
        # Note: Only available on some HackRF firmware versions
        # This is a placeholder - actual implementation depends on libhackrf bindings
        return 0.0
        
    def validate_tx_safety(self, frequency: float, power_dbm: float = None) -> bool:
        """Validate transmission parameters for safety"""
        # Safety checks
        if frequency < 10e6:
            logger.error("Frequency below 10 MHz may damage HackRF!")
            return False
            
        # Check for restricted bands (simplified - add more as needed)
        restricted_bands = [
            (108e6, 137e6),    # Aviation
            (406e6, 406.1e6),  # Emergency beacons
            (1.215e9, 1.39e9), # GPS/GNSS
        ]
        
        for low, high in restricted_bands:
            if low <= frequency <= high:
                logger.error(f"Frequency {frequency/1e6:.3f} MHz is in restricted band!")
                return False
                
        return True

# Fallback mock implementation if HackRF not available
if not HACKRF_AVAILABLE:
    class HackRFDevice(SDRDevice):
        """Mock HackRF implementation for testing"""
        
        def __init__(self, device_index: int = 0):
            super().__init__()
            self.device_name = "HackRF One (Mock)"
            self.mode = HackRFMode.IDLE
            logger.warning("Using mock HackRF implementation")
            
        async def connect(self) -> bool:
            logger.info("Mock HackRF connected")
            return True
            
        async def disconnect(self):
            logger.info("Mock HackRF disconnected")
            
        async def set_frequency(self, freq: float):
            self.frequency = freq
            logger.info(f"Mock HackRF set frequency to {freq/1e6:.3f} MHz")
            
        async def set_sample_rate(self, rate: float):
            self.sample_rate = rate
            logger.info(f"Mock HackRF set sample rate to {rate/1e6:.3f} Msps")
            
        async def set_gain(self, gain: Any):
            self.gain = gain
            logger.info(f"Mock HackRF set gain to {gain}")
            
        async def read_samples(self, num_samples: int) -> np.ndarray:
            # Generate mock samples (noise)
            return np.random.randn(num_samples) + 1j * np.random.randn(num_samples)
            
        async def write_samples(self, samples: np.ndarray):
            logger.info(f"Mock HackRF would transmit {len(samples)} samples")
            
        def validate_tx_safety(self, frequency: float, power_dbm: float = None) -> bool:
            return True