#!/usr/bin/env python3
"""Start Market Data Dashboard.

This script starts the FastAPI dashboard application.
"""
import os
import sys

# Change to dashboard directory
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)

# Load .env files
try:
    from dotenv import load_dotenv
    
    # Load local .env first as defaults (do not override explicit parent env)
    local_env = os.path.join(script_dir, ".env")
    if os.path.exists(local_env):
        load_dotenv(local_env, override=False)
        print(f"Loaded environment from: {local_env}")
    
    # Load parent .env as fallback
    parent_env = os.path.join(script_dir, "..", "market_data", ".env")
    if os.path.exists(parent_env):
        load_dotenv(parent_env, override=False)  # Don't override local settings
        print(f"Loaded fallback environment from: {parent_env}")
except (ImportError, Exception) as e:
    print(f"Note: Could not load .env: {e}")

# Import and run uvicorn
if __name__ == "__main__":
    import uvicorn
    
    # Get configuration from environment
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    reload = os.getenv("DASHBOARD_RELOAD", "false").lower() == "true"  # Disable reload for stability
    
    api_url = os.getenv("MARKET_DATA_API_URL", "http://localhost:8004")
    
    print(f"Starting Dashboard on {host}:{port}")
    print(f"Working directory: {os.getcwd()}")
    print(f"Market Data API: {api_url}")
    print("")
    
    # Start the server
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )
