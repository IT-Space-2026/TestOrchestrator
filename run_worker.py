"""Windows dispatch/outbox worker entry point.

Task Scheduler has no working-directory or env-file support; running this file
puts the payload root on sys.path so the bundled source-layout package imports
identically to ``toctl worker`` on the frozen Linux build.
"""
import sys

from testorchestrator.cli import main

if __name__ == "__main__":
    sys.exit(main(["worker"] + sys.argv[1:]))
