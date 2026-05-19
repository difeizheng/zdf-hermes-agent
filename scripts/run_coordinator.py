"""Coordinator entry point.

Usage:
    python scripts/run_coordinator.py [--port 9100] [--dev]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add repo root to path so coordinator package is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Task Coordinator")
    parser.add_argument("--port", type=int, default=9100, help="Port to listen on")
    parser.add_argument("--dev", action="store_true", help="Enable reload mode")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.dev else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Run: pip install uvicorn", file=sys.stderr)
        sys.exit(1)

    reload = args.dev
    uvicorn.run(
        "coordinator.server:app",
        host="0.0.0.0",
        port=args.port,
        reload=reload,
        log_level="debug" if args.dev else "info",
    )


if __name__ == "__main__":
    main()
