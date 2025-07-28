"""
Basic tests for AetherLink SDR MCP
"""

import pytest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_import():
    """Test that the package can be imported"""
    import sdr_mcp
    assert sdr_mcp.__version__ == "0.1.0"


def test_server_creation():
    """Test that server can be created"""
    from sdr_mcp.server import SDRMCPServer
    server = SDRMCPServer()
    assert server is not None
    assert server.server.name == "sdr-mcp"


def test_mock_rtlsdr():
    """Test mock RTL-SDR device"""
    from sdr_mcp.hardware.rtlsdr import RTLSDRDevice
    device = RTLSDRDevice()
    assert device.device_name == "RTL-SDR" or device.device_name == "RTL-SDR (Mock)"


def test_spectrum_analyzer():
    """Test spectrum analyzer creation"""
    from sdr_mcp.analysis.spectrum import SpectrumAnalyzer
    analyzer = SpectrumAnalyzer()
    assert analyzer.fft_size == 2048


def test_adsb_decoder():
    """Test ADS-B decoder creation"""
    from sdr_mcp.server import ADSBDecoder
    decoder = ADSBDecoder()
    assert len(decoder.aircraft) == 0
    assert decoder.message_count == 0


if __name__ == "__main__":
    pytest.main([__file__])