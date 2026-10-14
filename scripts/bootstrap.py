#!/usr/bin/env python3
"""Bootstrap script for SŪTRA Docker container."""
import os
import sys
import subprocess
from pathlib import Path

def print_header():
    print("╔══════════════════════════════════════╗")
    print("║         SŪTRA STARTUP          ║")
    print("╚══════════════════════════════════════╝\n")

def main():
    print_header()
    
    # Run the doctor command to validate environment
    try:
        subprocess.run(["sutra", "doctor"], check=True)
    except subprocess.CalledProcessError:
        print("\n[!] Environment checks failed, but attempting to start anyway...")

    print("\nStarting SŪTRA...\n")
    
    # If no arguments provided, default to starting control plane
    args = sys.argv[1:]
    
    # Check if TLS certs are needed for control plane
    if "control-plane" in args:
        certs_dir = Path("/data/certs")
        cert_file = os.environ.get("SUTRA_TLS_CERTFILE", os.environ.get("AGENTBRIDGE_TLS_CERTFILE", str(certs_dir / "cert.pem")))
        key_file = os.environ.get("SUTRA_TLS_KEYFILE", os.environ.get("AGENTBRIDGE_TLS_KEYFILE", str(certs_dir / "key.pem")))
        
        if not Path(cert_file).exists() or not Path(key_file).exists():
            print(f"[!] TLS certificates not found at {cert_file}. Generating dev certificates...")
            certs_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["sutra", "dev-certificate", "--output-dir", str(certs_dir)], check=True)
            os.environ["AGENTBRIDGE_TLS_CERTFILE"] = str(certs_dir / "localhost-cert.pem")
            os.environ["AGENTBRIDGE_TLS_KEYFILE"] = str(certs_dir / "localhost-key.pem")

    # Replace the current process with the agentbridge CLI
    command = ["sutra"] + args
    
    # Use os.execvp to replace the current process
    try:
        os.execvp("sutra", command)
    except Exception as e:
        print(f"Failed to start SŪTRA: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
