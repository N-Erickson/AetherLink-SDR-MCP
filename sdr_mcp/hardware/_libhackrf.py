"""
Minimal ctypes binding for libhackrf, the C host library that ships with the
HackRF tools (``brew install hackrf`` on macOS, ``apt install libhackrf0`` on
Debian/Ubuntu).

Only the functions AetherLink needs are declared. Missing symbols in older
library builds are tolerated: the attribute is simply absent on ``lib``.
"""

import ctypes
import ctypes.util
import logging
import os
import sys
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    c_char_p,
    c_double,
    c_int,
    c_uint8,
    c_uint32,
    c_uint64,
    c_void_p,
)

logger = logging.getLogger(__name__)

HACKRF_SUCCESS = 0
HACKRF_TRUE = 1
TRANSFER_BUFFER_BYTES = 262144  # libhackrf transfer size (bytes of interleaved int8 I/Q)


class hackrf_device(Structure):
    pass


hackrf_device_p = POINTER(hackrf_device)


class hackrf_transfer(Structure):
    _fields_ = [
        ("device", hackrf_device_p),
        ("buffer", POINTER(c_uint8)),
        ("buffer_length", c_int),
        ("valid_length", c_int),
        ("rx_ctx", c_void_p),
        ("tx_ctx", c_void_p),
    ]


class read_partid_serialno_t(Structure):
    _fields_ = [
        ("part_id", c_uint32 * 2),
        ("serial_no", c_uint32 * 4),
    ]


class hackrf_device_list_t(Structure):
    _fields_ = [
        ("serial_numbers", POINTER(c_char_p)),
        ("usb_board_ids", POINTER(c_int)),
        ("usb_device_index", POINTER(c_int)),
        ("devicecount", c_int),
        ("usb_devices", POINTER(c_void_p)),
        ("usb_devicecount", c_int),
    ]


# int callback(hackrf_transfer* transfer) -- return 0 to keep streaming
sample_block_cb_fn = CFUNCTYPE(c_int, POINTER(hackrf_transfer))

_PROTOTYPES = {
    "hackrf_init": (c_int, []),
    "hackrf_exit": (c_int, []),
    "hackrf_open": (c_int, [POINTER(hackrf_device_p)]),
    "hackrf_open_by_serial": (c_int, [c_char_p, POINTER(hackrf_device_p)]),
    "hackrf_close": (c_int, [hackrf_device_p]),
    "hackrf_device_list": (POINTER(hackrf_device_list_t), []),
    "hackrf_device_list_open": (c_int, [POINTER(hackrf_device_list_t), c_int, POINTER(hackrf_device_p)]),
    "hackrf_device_list_free": (None, [POINTER(hackrf_device_list_t)]),
    "hackrf_start_rx": (c_int, [hackrf_device_p, sample_block_cb_fn, c_void_p]),
    "hackrf_stop_rx": (c_int, [hackrf_device_p]),
    "hackrf_start_tx": (c_int, [hackrf_device_p, sample_block_cb_fn, c_void_p]),
    "hackrf_stop_tx": (c_int, [hackrf_device_p]),
    "hackrf_is_streaming": (c_int, [hackrf_device_p]),
    "hackrf_set_freq": (c_int, [hackrf_device_p, c_uint64]),
    "hackrf_set_sample_rate": (c_int, [hackrf_device_p, c_double]),
    "hackrf_set_baseband_filter_bandwidth": (c_int, [hackrf_device_p, c_uint32]),
    "hackrf_compute_baseband_filter_bw": (c_uint32, [c_uint32]),
    "hackrf_set_lna_gain": (c_int, [hackrf_device_p, c_uint32]),
    "hackrf_set_vga_gain": (c_int, [hackrf_device_p, c_uint32]),
    "hackrf_set_txvga_gain": (c_int, [hackrf_device_p, c_uint32]),
    "hackrf_set_amp_enable": (c_int, [hackrf_device_p, c_uint8]),
    "hackrf_set_antenna_enable": (c_int, [hackrf_device_p, c_uint8]),
    "hackrf_board_id_read": (c_int, [hackrf_device_p, POINTER(c_uint8)]),
    "hackrf_board_id_name": (c_char_p, [c_int]),
    "hackrf_version_string_read": (c_int, [hackrf_device_p, c_char_p, c_uint8]),
    "hackrf_board_partid_serialno_read": (c_int, [hackrf_device_p, POINTER(read_partid_serialno_t)]),
    "hackrf_error_name": (c_char_p, [c_int]),
}


def _candidates():
    env = os.environ.get("HACKRF_LIBRARY")
    if env:
        yield env
    found = ctypes.util.find_library("hackrf")
    if found:
        yield found
    if sys.platform == "darwin":
        yield from (
            "libhackrf.dylib",
            "libhackrf.0.dylib",
            "/opt/homebrew/lib/libhackrf.dylib",
            "/usr/local/lib/libhackrf.dylib",
        )
    elif sys.platform.startswith("win"):
        yield from ("hackrf.dll", "libhackrf.dll")
    else:
        yield from ("libhackrf.so.0", "libhackrf.so", "/usr/lib/libhackrf.so.0", "/usr/local/lib/libhackrf.so.0")


def _load():
    errors = []
    for candidate in _candidates():
        try:
            handle = ctypes.CDLL(candidate)
        except OSError as e:
            errors.append(f"{candidate}: {e}")
            continue
        for name, (restype, argtypes) in _PROTOTYPES.items():
            fn = getattr(handle, name, None)
            if fn is None:
                continue
            fn.restype = restype
            fn.argtypes = argtypes
        logger.debug(f"Loaded libhackrf from {candidate}")
        return handle, None
    return None, "libhackrf shared library not found" + (f" ({errors[-1]})" if errors else "")


lib, load_error = _load()


def available() -> bool:
    return lib is not None


def error_name(code: int) -> str:
    """Human-readable libhackrf error name for a return code."""
    if lib is not None and hasattr(lib, "hackrf_error_name"):
        try:
            name = lib.hackrf_error_name(int(code))
            if name:
                return name.decode(errors="replace")
        except Exception:
            pass
    return f"error {code}"
