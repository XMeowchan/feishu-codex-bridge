#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    bridge_path = base_dir / "bridge.py"
    config_path = base_dir / "bridge.toml"

    if not bridge_path.exists():
        print(f"bridge.py not found: {bridge_path}", file=sys.stderr)
        return 1

    if not config_path.exists():
        print(f"bridge.toml not found: {config_path}", file=sys.stderr)
        return 1

    python_bin = sys.executable
    if not python_bin:
        print("Python interpreter is not available", file=sys.stderr)
        return 1

    command = [python_bin, str(bridge_path), "--config", str(config_path)]
    print(f"Starting Feishu Codex Bridge from {base_dir}")
    print(f"Using config: {config_path}")
    try:
        completed = subprocess.run(command, cwd=base_dir)
    except KeyboardInterrupt:
        print()
        print("Bridge launcher interrupted.")
        return 130
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
