"""
AetherLink - Software Defined Radio Model Context Protocol Server
"""

__version__ = "0.1.2"
__author__ = "N-Erickson"

# Lazy imports to avoid circular dependencies when running as module
__all__ = ["SDRMCPServer", "main", "run", "__version__"]

def __getattr__(name):
    """Lazy import to avoid circular dependencies"""
    if name in ("SDRMCPServer", "main", "run"):
        from .server import SDRMCPServer, main, run
        return {"SDRMCPServer": SDRMCPServer, "main": main, "run": run}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
