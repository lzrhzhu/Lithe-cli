"""Support ``python -m lithe_cli`` alongside the console script."""

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
