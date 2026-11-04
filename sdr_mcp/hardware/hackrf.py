"""
HackRF hardware support for SDR-MCP

Talks to the system libhackrf library directly through a small ctypes binding
(see _libhackrf.py). No Python HackRF package is required; install the HackRF
host tools so the shared library is present.
"""

import asyncio
import ctypes
import logging
import queue
import threading
from enum import Enum
from typing import Any, Dict, Optional

import numpy as np

from . import _libhackrf as hrf
from .base import SDRDevice

logger = logging.getLogger(__name__)

HACKRF_AVAILABLE = hrf.available()
HACKRF_IMPORT_ERROR: Optional[str] = hrf.load_error
HACKRF_INSTALL_HINT = (
    "Install the HackRF host library: 'brew install hackrf' (macOS) or "
    "'sudo apt install hackrf libhackrf0' (Debian/Ubuntu)."
)
if not HACKRF_AVAILABLE:
    logger.info(f"HackRF support disabled: {HACKRF_IMPORT_ERROR}. {HACKRF_INSTALL_HINT}")


class HackRFMode(Enum):
    """HackRF operating modes"""
    RECEIVE = "receive"
    TRANSMIT = "transmit"
    IDLE = "idle"


LNA_GAIN_STEPS = (0, 8, 16, 24, 32, 40)      # dB
VGA_GAIN_STEPS = tuple(range(0, 63, 2))      # dB
TX_VGA_GAIN_MAX = 47                         # dB
MIN_FREQUENCY = 1e6
MAX_FREQUENCY = 6e9
MIN_SAMPLE_RATE = 2e6
MAX_SAMPLE_RATE = 20e6


def _check(result: int, what: str):
    if result != hrf.HACKRF_SUCCESS:
        raise RuntimeError(f"{what} failed: {hrf.error_name(result)}")


class HackRFDevice(SDRDevice):
    """HackRF One hardware implementation"""

    def __init__(self, device_index: int = 0):
        super().__init__(device_index)
        self.device_name = "HackRF One"
        self.mode = HackRFMode.IDLE

        self.min_frequency = MIN_FREQUENCY
        self.max_frequency = MAX_FREQUENCY
        self.min_sample_rate = MIN_SAMPLE_RATE
        self.max_sample_rate = MAX_SAMPLE_RATE

        # HackRF specific parameters
        self.lna_gain = 16  # dB
        self.vga_gain = 20  # dB
        self.tx_vga_gain = 0  # dB
        self.amp_enable = False
        self.serial: Optional[str] = None
        self.firmware: Optional[str] = None
        self.bias_tee = False

        # Native handle plus references that must outlive the C callbacks
        self._dev = hrf.hackrf_device_p()
        self._rx_cb = None
        self._tx_cb = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # RX hands buffers to the event loop; TX is drained by libhackrf's thread,
        # so it uses a thread-safe queue.
        self._rx_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._rx_dropped = 0
        self._tx_queue: "queue.Queue[bytes]" = queue.Queue()
        self._tx_underruns = 0
        self._tx_lock = threading.Lock()

    @staticmethod
    def list_devices() -> list:
        """Enumerate attached HackRF boards (index, serial) without opening them"""
        if not HACKRF_AVAILABLE or not hasattr(hrf.lib, "hackrf_device_list"):
            return []
        lib = hrf.lib
        devices = []
        try:
            if lib.hackrf_init() != hrf.HACKRF_SUCCESS:
                return devices
            dev_list = lib.hackrf_device_list()
            if dev_list:
                try:
                    for index in range(dev_list.contents.devicecount):
                        serial = dev_list.contents.serial_numbers[index]
                        devices.append({
                            "type": "hackrf",
                            "index": index,
                            "name": "HackRF",
                            "serial": serial.decode(errors="replace") if serial else None,
                        })
                finally:
                    if hasattr(lib, "hackrf_device_list_free"):
                        lib.hackrf_device_list_free(dev_list)
        except Exception as e:
            logger.debug(f"HackRF enumeration failed: {e}")
        finally:
            try:
                lib.hackrf_exit()
            except Exception:
                pass
        return devices

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> bool:
        """Connect to HackRF device"""
        if not HACKRF_AVAILABLE:
            logger.error(f"HackRF library not available: {HACKRF_IMPORT_ERROR}")
            return False

        lib = hrf.lib
        try:
            _check(lib.hackrf_init(), "hackrf_init")

            self._dev = hrf.hackrf_device_p()
            if hasattr(lib, "hackrf_device_list"):
                dev_list = lib.hackrf_device_list()
                if not dev_list:
                    raise RuntimeError("hackrf_device_list returned NULL")
                try:
                    count = dev_list.contents.devicecount
                    if count <= self.device_index:
                        raise RuntimeError(
                            f"HackRF device index {self.device_index} not found ({count} device(s) attached)"
                        )
                    _check(
                        lib.hackrf_device_list_open(dev_list, self.device_index, ctypes.byref(self._dev)),
                        "hackrf_device_list_open",
                    )
                finally:
                    if hasattr(lib, "hackrf_device_list_free"):
                        lib.hackrf_device_list_free(dev_list)
            else:
                if self.device_index != 0:
                    raise RuntimeError("This libhackrf build cannot open devices by index")
                _check(lib.hackrf_open(ctypes.byref(self._dev)), "hackrf_open")

            if not self._dev:
                raise RuntimeError("HackRF open returned a NULL device handle")

            self.device = self._dev
            self._loop = asyncio.get_running_loop()

            info = self._get_device_info()
            self.serial = info.get("serial")
            self.firmware = info.get("version")
            logger.info(f"Connected to HackRF: {info}")

            # Set initial configuration
            await self.set_sample_rate(self.sample_rate)
            await self.set_frequency(self.frequency or 100e6)
            await self._update_gains()
            return True

        except Exception as e:
            logger.error(f"Failed to connect to HackRF: {e}")
            if self._dev:
                try:
                    lib.hackrf_close(self._dev)
                except Exception:
                    pass
            try:
                lib.hackrf_exit()
            except Exception:
                pass
            self._dev = hrf.hackrf_device_p()
            self.device = None
            return False

    async def disconnect(self):
        """Disconnect from HackRF device"""
        if not self.device:
            return
        lib = hrf.lib
        try:
            if self.mode != HackRFMode.IDLE:
                await self.stop_streaming()
            lib.hackrf_close(self._dev)
            lib.hackrf_exit()
            logger.info("Disconnected from HackRF")
        except Exception as e:
            logger.error(f"Error disconnecting HackRF: {e}")
        finally:
            self._dev = hrf.hackrf_device_p()
            self.device = None
            self.mode = HackRFMode.IDLE
            self.is_capturing = False

    # ------------------------------------------------------------------ tuning

    async def set_frequency(self, freq: float):
        """Set center frequency in Hz"""
        if not (self.min_frequency <= freq <= self.max_frequency):
            raise ValueError(
                f"Frequency {freq/1e6:.3f} MHz out of HackRF range "
                f"({self.min_frequency/1e6:.0f} MHz - {self.max_frequency/1e9:.0f} GHz)"
            )
        self.frequency = freq
        if self.device:
            _check(hrf.lib.hackrf_set_freq(self._dev, int(freq)), "hackrf_set_freq")
            logger.debug(f"Set frequency to {freq/1e6:.3f} MHz")

    async def set_sample_rate(self, rate: float):
        """Set sample rate in Hz"""
        if not (self.min_sample_rate <= rate <= self.max_sample_rate):
            raise ValueError(
                f"Sample rate {rate/1e6:.3f} Msps out of HackRF range "
                f"({self.min_sample_rate/1e6:.0f}-{self.max_sample_rate/1e6:.0f} Msps)"
            )
        self.sample_rate = rate
        if self.device:
            lib = hrf.lib
            _check(lib.hackrf_set_sample_rate(self._dev, float(rate)), "hackrf_set_sample_rate")
            # Baseband filter: ~75% of the sample rate, snapped to a supported value
            bandwidth = int(0.75 * rate)
            if hasattr(lib, "hackrf_compute_baseband_filter_bw"):
                bandwidth = int(lib.hackrf_compute_baseband_filter_bw(bandwidth))
            result = lib.hackrf_set_baseband_filter_bandwidth(self._dev, bandwidth)
            if result != hrf.HACKRF_SUCCESS:
                logger.warning(f"Failed to set filter bandwidth: {hrf.error_name(result)}")
            logger.debug(f"Set sample rate to {rate/1e6:.3f} Msps (filter {bandwidth/1e6:.2f} MHz)")

    async def set_gain(self, gain: Any):
        """Set gain: a dict with lna_gain/vga_gain/amp_enable, a total dB value, or 'auto'"""
        if isinstance(gain, str) and gain.lower() == 'auto':
            self.lna_gain = 24
            self.vga_gain = 30
            self.gain = 'auto'
        elif isinstance(gain, dict):
            if 'lna_gain' in gain:
                self.lna_gain = min(LNA_GAIN_STEPS, key=lambda x: abs(x - float(gain['lna_gain'])))
            if 'vga_gain' in gain:
                self.vga_gain = min(VGA_GAIN_STEPS, key=lambda x: abs(x - float(gain['vga_gain'])))
            if 'amp_enable' in gain:
                self.amp_enable = bool(gain['amp_enable'])
            self.gain = f"LNA:{self.lna_gain} VGA:{self.vga_gain}" + (" AMP" if self.amp_enable else "")
        elif isinstance(gain, (int, float)) and not isinstance(gain, bool):
            # Single value: fill the LNA first, remainder on the VGA
            total_gain = float(gain)
            self.lna_gain = min(LNA_GAIN_STEPS, key=lambda x: abs(x - min(total_gain, 40)))
            remainder = max(0.0, min(62.0, total_gain - self.lna_gain))
            self.vga_gain = min(VGA_GAIN_STEPS, key=lambda x: abs(x - remainder))
            self.gain = total_gain
        else:
            raise ValueError(f"Invalid gain format: {gain!r}")

        await self._update_gains()

    async def _update_gains(self):
        """Push gain settings to the device"""
        if not self.device:
            return
        lib = hrf.lib
        for fn_name, value in (
            ("hackrf_set_lna_gain", self.lna_gain),
            ("hackrf_set_vga_gain", self.vga_gain),
            ("hackrf_set_amp_enable", 1 if self.amp_enable else 0),
        ):
            result = getattr(lib, fn_name)(self._dev, int(value))
            if result != hrf.HACKRF_SUCCESS:
                logger.warning(f"{fn_name} failed: {hrf.error_name(result)}")
        logger.debug(f"Set gains - LNA:{self.lna_gain} VGA:{self.vga_gain} Amp:{self.amp_enable}")

    async def set_bias_tee(self, enabled: bool):
        """Enable or disable antenna port power (HackRF 'antenna enable', ~3.3 V / 50 mA)"""
        if not self.device:
            raise RuntimeError("Device not connected")
        if not hasattr(hrf.lib, "hackrf_set_antenna_enable"):
            raise RuntimeError("This libhackrf build has no hackrf_set_antenna_enable")
        _check(hrf.lib.hackrf_set_antenna_enable(self._dev, 1 if enabled else 0), "hackrf_set_antenna_enable")
        self.bias_tee = bool(enabled)
        logger.info(f"HackRF antenna port power {'enabled' if enabled else 'disabled'}")

    async def set_tx_gain(self, gain: int):
        """Set transmit VGA gain (0-47 dB)"""
        if not (0 <= gain <= TX_VGA_GAIN_MAX):
            raise ValueError(f"TX VGA gain must be 0-{TX_VGA_GAIN_MAX} dB, got {gain}")
        self.tx_vga_gain = int(gain)
        if self.device:
            result = hrf.lib.hackrf_set_txvga_gain(self._dev, self.tx_vga_gain)
            if result != hrf.HACKRF_SUCCESS:
                logger.warning(f"Failed to set TX VGA gain: {hrf.error_name(result)}")

    # ------------------------------------------------------------------ receive

    def _enqueue_rx(self, data: bytes):
        if self._rx_queue.full():
            try:
                self._rx_queue.get_nowait()
                self._rx_dropped += 1
            except asyncio.QueueEmpty:
                pass
        self._rx_queue.put_nowait(data)

    async def start_rx(self):
        """Start receive mode"""
        if self.mode == HackRFMode.RECEIVE:
            return
        if not self.device:
            raise RuntimeError("Device not connected")
        if self.mode != HackRFMode.IDLE:
            await self.stop_streaming()

        loop = asyncio.get_running_loop()
        self._loop = loop
        self._rx_dropped = 0
        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()

        def _rx_callback(transfer_p):
            # Called on libhackrf's USB thread: copy out and hand to the event loop.
            transfer = transfer_p.contents
            data = ctypes.string_at(transfer.buffer, transfer.valid_length)
            try:
                loop.call_soon_threadsafe(self._enqueue_rx, data)
            except RuntimeError:
                return -1  # event loop gone; stop streaming
            return 0

        self._rx_cb = hrf.sample_block_cb_fn(_rx_callback)
        _check(hrf.lib.hackrf_start_rx(self._dev, self._rx_cb, None), "hackrf_start_rx")
        self.mode = HackRFMode.RECEIVE
        self.is_capturing = True
        logger.info("Started HackRF RX mode")

    async def read_samples(self, num_samples: int) -> np.ndarray:
        """Read IQ samples from HackRF"""
        if not self.device:
            raise RuntimeError("Device not connected")
        if self.mode != HackRFMode.RECEIVE:
            await self.start_rx()

        chunks = []
        collected = 0
        while collected < num_samples:
            try:
                data = await asyncio.wait_for(self._rx_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                raise RuntimeError("HackRF produced no samples for 5 seconds")
            iq = self._convert_samples(data)
            chunks.append(iq)
            collected += len(iq)

        return np.concatenate(chunks)[:num_samples]

    # ------------------------------------------------------------------ transmit

    async def start_tx(self):
        """Start transmit mode"""
        if self.mode == HackRFMode.TRANSMIT:
            return
        if not self.device:
            raise RuntimeError("Device not connected")
        if self.mode != HackRFMode.IDLE:
            await self.stop_streaming()

        lib = hrf.lib
        result = lib.hackrf_set_txvga_gain(self._dev, self.tx_vga_gain)
        if result != hrf.HACKRF_SUCCESS:
            logger.warning(f"Failed to set TX VGA gain: {hrf.error_name(result)}")
        self._tx_underruns = 0

        def _tx_callback(transfer_p):
            # Called on libhackrf's USB thread. Always fill the whole buffer.
            transfer = transfer_p.contents
            length = transfer.buffer_length
            try:
                chunk = self._tx_queue.get_nowait()
            except queue.Empty:
                chunk = b""
                self._tx_underruns += 1
            n = min(len(chunk), length)
            if n:
                ctypes.memmove(transfer.buffer, chunk, n)
            if n < length:
                base = ctypes.cast(transfer.buffer, ctypes.c_void_p).value
                ctypes.memset(base + n, 0, length - n)
            transfer.valid_length = length
            return 0

        self._tx_cb = hrf.sample_block_cb_fn(_tx_callback)
        _check(lib.hackrf_start_tx(self._dev, self._tx_cb, None), "hackrf_start_tx")
        self.mode = HackRFMode.TRANSMIT
        self.is_capturing = True
        logger.info("Started HackRF TX mode")

    async def write_samples(self, samples: np.ndarray):
        """Queue IQ samples for transmission (starts TX if needed)"""
        if not self.device:
            raise RuntimeError("Device not connected")
        if self.frequency < 10e6:
            logger.warning("Transmitting below 10 MHz may damage the HackRF!")

        tx_data = self._convert_to_int8(samples).tobytes()
        chunk_size = hrf.TRANSFER_BUFFER_BYTES
        for i in range(0, len(tx_data), chunk_size):
            self._tx_queue.put(tx_data[i:i + chunk_size])

        if self.mode != HackRFMode.TRANSMIT:
            await self.start_tx()

    async def transmit(self, samples: np.ndarray, timeout: Optional[float] = None):
        """Transmit samples and return once they have left the device, then go idle."""
        await self.write_samples(samples)
        expected = len(samples) / self.sample_rate
        deadline = asyncio.get_running_loop().time() + (timeout or expected + 5.0)
        try:
            while not self._tx_queue.empty():
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError("HackRF transmit timed out draining the sample queue")
                await asyncio.sleep(0.02)
            # libhackrf keeps a few transfers in flight after the queue empties
            await asyncio.sleep(0.5)
        finally:
            await self.stop_streaming()

    # ------------------------------------------------------------------ helpers

    async def stop_streaming(self):
        """Stop current streaming mode"""
        if self.mode == HackRFMode.IDLE:
            return
        lib = hrf.lib
        if self.mode == HackRFMode.RECEIVE:
            result = lib.hackrf_stop_rx(self._dev)
        else:
            result = lib.hackrf_stop_tx(self._dev)
        if result != hrf.HACKRF_SUCCESS:
            logger.warning(f"Failed to stop streaming: {hrf.error_name(result)}")

        self.mode = HackRFMode.IDLE
        self.is_capturing = False

        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()
        while not self._tx_queue.empty():
            try:
                self._tx_queue.get_nowait()
            except queue.Empty:
                break
        if self._rx_dropped:
            logger.warning(f"HackRF RX dropped {self._rx_dropped} buffer(s); consumer fell behind")
        if self._tx_underruns:
            logger.debug(f"HackRF TX padded {self._tx_underruns} buffer(s) with silence")
        logger.info("Stopped HackRF streaming")

    @staticmethod
    def _convert_samples(buffer: bytes) -> np.ndarray:
        """Convert HackRF interleaved int8 I/Q to complex64"""
        iq = np.frombuffer(buffer, dtype=np.int8).astype(np.float32) / 127.0
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    @staticmethod
    def _convert_to_int8(samples: np.ndarray) -> np.ndarray:
        """Convert complex float samples to HackRF interleaved int8"""
        i = np.clip(np.real(samples) * 127, -127, 127).astype(np.int8)
        q = np.clip(np.imag(samples) * 127, -127, 127).astype(np.int8)
        iq_data = np.empty(len(samples) * 2, dtype=np.int8)
        iq_data[0::2] = i
        iq_data[1::2] = q
        return iq_data

    def _get_device_info(self) -> Dict[str, Any]:
        """Read board id, firmware version and serial number"""
        info: Dict[str, Any] = {"device": "HackRF"}
        if not self.device:
            return info
        lib = hrf.lib
        try:
            board_id = ctypes.c_uint8(0)
            if lib.hackrf_board_id_read(self._dev, ctypes.byref(board_id)) == hrf.HACKRF_SUCCESS:
                name = None
                if hasattr(lib, "hackrf_board_id_name"):
                    raw = lib.hackrf_board_id_name(board_id.value)
                    name = raw.decode(errors="replace") if raw else None
                info["board_id"] = name or f"{board_id.value:#04x}"
                if name:
                    self.device_name = name
        except Exception as e:
            logger.debug(f"board id read failed: {e}")
        try:
            version = ctypes.create_string_buffer(255)
            if lib.hackrf_version_string_read(self._dev, version, 255) == hrf.HACKRF_SUCCESS:
                info["version"] = version.value.decode(errors="replace")
        except Exception as e:
            logger.debug(f"version read failed: {e}")
        try:
            ids = hrf.read_partid_serialno_t()
            if lib.hackrf_board_partid_serialno_read(self._dev, ctypes.byref(ids)) == hrf.HACKRF_SUCCESS:
                info["serial"] = "".join(f"{n:08x}" for n in ids.serial_no)
        except Exception as e:
            logger.debug(f"serial read failed: {e}")
        return info

    async def get_info(self) -> dict:
        info = await super().get_info()
        info.update({
            "mode": self.mode.value,
            "lna_gain": self.lna_gain,
            "vga_gain": self.vga_gain,
            "tx_vga_gain": self.tx_vga_gain,
            "amp_enable": self.amp_enable,
            "serial": self.serial,
            "firmware": self.firmware,
        })
        return info

    def validate_tx_safety(self, frequency: float, power_dbm: float = None) -> bool:
        """Validate transmission parameters for safety"""
        from ..utils.validators import is_restricted_frequency

        if frequency < 10e6:
            logger.error("Frequency below 10 MHz may damage HackRF!")
            return False
        if frequency > 6e9:
            logger.error("Frequency above 6 GHz is outside HackRF range!")
            return False
        if is_restricted_frequency(frequency):
            logger.error(f"Frequency {frequency/1e6:.3f} MHz is in a restricted band!")
            return False
        if power_dbm is not None and power_dbm > 15.0:
            logger.error(f"TX power {power_dbm} dBm exceeds safe limit!")
            return False
        return True
