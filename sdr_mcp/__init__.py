"""
AetherLink - Software Defined Radio Model Context Protocol Server
"""

__version__ = "0.1.0"
__author__ = "Your Name"

# Lazy imports to avoid circular dependencies when running as module
__all__ = ["SDRMCPServer", "main", "__version__"]

def __getattr__(name):
    """Lazy import to avoid circular dependencies"""
    if name in ("SDRMCPServer", "main"):
        from .server import SDRMCPServer, main
        return SDRMCPServer if name == "SDRMCPServer" else main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
