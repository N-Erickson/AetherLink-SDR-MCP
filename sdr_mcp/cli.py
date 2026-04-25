"""
Lightweight CLI entry point for AetherLink.

This module avoids importing the full server stack so that
--help, --version, and --setup work even when system SDR
libraries are missing or broken.
"""

import sys


def run():
    """Console script entry point."""
    if "--version" in sys.argv:
        from . import __version__
        print(f"aetherlink {__version__}")

    elif "--help" in sys.argv or "-h" in sys.argv:
        print("aetherlink - SDR MCP Server")
        print()
        print("Usage:")
        print("  aetherlink           Start the MCP server (stdio)")
        print("  aetherlink --setup   Configure Claude Desktop")
        print("  aetherlink --version Show version")

    elif "--setup" in sys.argv or "setup" in sys.argv[1:2]:
        from .server import setup_claude_desktop
        setup_claude_desktop()

    else:
        import asyncio
        from .server import main
        asyncio.run(main())


if __name__ == "__main__":
    run()
