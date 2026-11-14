"""Module entry point: ``python -m testorchestrator <command>``.

Makes the toctl CLI available on source-layout installs (e.g. the Windows
bundled-python payload) where no frozen ``toctl`` binary exists.
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
