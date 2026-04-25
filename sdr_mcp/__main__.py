"""Allow running as python -m sdr_mcp"""
import asyncio
from .server import main

asyncio.run(main())
