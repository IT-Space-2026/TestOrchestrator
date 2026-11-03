"""Frozen entry point — toctl <command> (see testorchestrator/cli.py)."""
import sys
from testorchestrator.cli import main

if __name__ == "__main__":
    sys.exit(main())
