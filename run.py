"""
CodeCom V2 — Quick Start Entry Point

Run this file directly to start the CLI without installing the package:
    python run.py

This is equivalent to running 'codecom' after pip install -e .
Both invoke codecom.cli:main().

Optional arguments:
    python run.py --dir /path/to/project    # Set working directory
    python run.py --config /path/to/cfg     # Use custom config file
    python run.py --no-approval             # Skip approval gates (dangerous!)
"""

from codecom.cli import main

if __name__ == "__main__":
    main()
