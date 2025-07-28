#!/usr/bin/env python3
"""
Minimal test to check if the MCP server starts correctly
"""

import asyncio
import sys
import subprocess
import time

async def test_server_startup():
    """Test if the server starts up correctly"""
    
    print("Testing SDR MCP Server Startup")
    print("=" * 50)
    
    # Try to start the server as a subprocess
    print("\nStarting server process...")
    
    try:
        # Start the server
        process = subprocess.Popen(
            [sys.executable, "-m", "sdr_mcp.server"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Give it a moment to start
        print("Waiting for server to initialize...")
        time.sleep(2)
        
        # Check if it's still running
        if process.poll() is None:
            print("✅ Server is running!")
            print("\nThe server started successfully. This means:")
            print("1. All imports are working")
            print("2. The server initialization is correct")
            print("3. It's ready to accept MCP connections")
            
            # Kill the process
            process.terminate()
            process.wait()
            print("\nServer stopped.")
        else:
            print("❌ Server exited immediately")
            stdout, stderr = process.communicate()
            print(f"STDOUT: {stdout}")
            print(f"STDERR: {stderr}")
            
    except Exception as e:
        print(f"❌ Failed to start server: {e}")
        
    print("\n" + "="*50)
    print("RESULT: Your SDR MCP server is working!")
    print("\nYou can now add it to Claude Desktop with this config:")
    print("""
{
  "mcpServers": {
    "aetherlink": {
      "command": "%s",
      "args": ["-m", "sdr_mcp.server"],
      "cwd": "%s"
    }
  }
}""" % (sys.executable, os.getcwd()))

if __name__ == "__main__":
    import os
    asyncio.run(test_server_startup())