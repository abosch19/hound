"""Permite `python -m hound` (lo usa el servicio de Windows: multiprocessing funciona mejor así)."""

import multiprocessing
import sys

from .cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
