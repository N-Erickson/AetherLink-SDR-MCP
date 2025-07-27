#!/usr/bin/env python3
"""
Example client for testing the SDR MCP server
"""

import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def test_sdr_server():
    """Test basic SDR MCP server functionality"""
    
    # Server command - adjust path as needed
    server_params = StdioServerParameters(
        command="python",
        args=["sdr_mcp/server.py"]
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the session
            await session.initialize()
            
            # List available tools
            tools = await session.list_tools()
            print("Available tools:")
            for tool in tools:
                print(f"  - {tool.name}: {tool.description}")
            print()
            
            # Connect to SDR
            print("Connecting to RTL-SDR...")
            result = await session.call_tool("sdr_connect", {"device_type": "rtlsdr"})
            print(f"Result: {result[0].text}")
            print()
            
            # Get status
            print("Getting SDR status...")
            result = await session.call_tool("sdr_get_status", {})
            status = json.loads(result[0].text)
            print(f"Status: {json.dumps(status, indent=2)}")
            print()
            
            # Set frequency to 100.1 MHz (FM radio)
            print("Setting frequency to 100.1 MHz...")
            result = await session.call_tool("sdr_set_frequency", {"frequency": 100.1e6})
            print(f"Result: {result[0].text}")
            print()
            
            # Perform spectrum analysis
            print("Performing spectrum analysis...")
            result = await session.call_tool("spectrum_analyze", {
                "bandwidth": 200000,  # 200 kHz
                "fft_size": 1024
            })
            print(f"Analysis:\n{result[0].text}")
            print()
            
            # Switch to ADS-B frequency and start tracking
            print("Starting aircraft tracking...")
            result = await session.call_tool("aviation_track_aircraft", {})
            print(f"Result: {result[0].text}")
            
            # Wait a bit for some aircraft data
            await asyncio.sleep(5)
            
            # Get tracked aircraft
            print("\nGetting tracked aircraft...")
            result = await session.call_tool("aviation_get_aircraft", {})
            print(f"Aircraft:\n{result[0].text}")
            
            # Read aircraft resource
            print("\nReading aircraft resource...")
            resources = await session.list_resources()
            for resource in resources:
                if resource.uri == "aviation://aircraft":
                    content = await session.read_resource(resource.uri)
                    data = json.loads(content.text)
                    print(f"Resource data: {json.dumps(data, indent=2)}")
            
            # Stop tracking
            print("\nStopping aircraft tracking...")
            result = await session.call_tool("aviation_stop_tracking", {})
            print(f"Result: {result[0].text}")
            
            # Disconnect
            print("\nDisconnecting from SDR...")
            result = await session.call_tool("sdr_disconnect", {})
            print(f"Result: {result[0].text}")

async def interactive_demo():
    """Interactive demo showing conversation-style usage"""
    
    server_params = StdioServerParameters(
        command="python",
        args=["sdr_mcp/server.py"]
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            print("SDR MCP Interactive Demo")
            print("=" * 50)
            
            # Example prompts that would come from an LLM
            prompts = [
                ("Connect to my RTL-SDR", "sdr_connect", {"device_type": "rtlsdr"}),
                ("What's the current status?", "sdr_get_status", {}),
                ("Scan the FM broadcast band", "spectrum_analyze", {"bandwidth": 2e6, "fft_size": 2048}),
                ("Track aircraft in my area", "aviation_track_aircraft", {}),
                ("Show me the aircraft you're tracking", "aviation_get_aircraft", {}),
            ]
            
            for prompt, tool, args in prompts:
                print(f"\nUser: {prompt}")
                result = await session.call_tool(tool, args)
                print(f"Assistant: {result[0].text}")
                
                if tool == "aviation_track_aircraft":
                    await asyncio.sleep(3)  # Give it time to collect data

if __name__ == "__main__":
    # Run the basic test
    asyncio.run(test_sdr_server())
    
    # Uncomment to run interactive demo
    # asyncio.run(interactive_demo())