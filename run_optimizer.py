#!/usr/bin/env python3
"""
TradingView Strategy Optimizer Launcher
"""
import subprocess
import sys
import os

def main():
    print("🚀 Starting TradingView Strategy Optimizer...")
    print()
    print("Make sure you have installed the requirements:")
    print("pip install -r requirements.txt")
    print("playwright install")
    print()

    # Check if streamlit is installed
    try:
        import streamlit
    except ImportError:
        print("❌ Streamlit not found. Please install requirements:")
        print("pip install -r requirements.txt")
        return

    print("📱 Opening web interface...")
    print("The interface will open in your default web browser.")
    print("If it doesn't open automatically, visit: http://localhost:8501")
    print()

    # Run streamlit
    try:
        subprocess.run([sys.executable, "-m", "streamlit", "run", "streamlit_app.py"],
                      cwd=os.getcwd())
    except KeyboardInterrupt:
        print("\n👋 Optimizer closed.")
    except Exception as e:
        print(f"❌ Error starting interface: {e}")

if __name__ == "__main__":
    main()