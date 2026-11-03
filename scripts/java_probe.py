#!/usr/bin/env python3
"""Standalone Allure-Java probe launcher (§13 installer path).

Loads ``testorchestrator/allure_runtime.py`` by file path — the probe
must run WITHOUT importing the application package (no Flask/SQLAlchemy
needed), so the installer can run it under the service account before
or without the app venv:

    python3 scripts/java_probe.py --runtime <payload>/testorchestrator/allure_runtime.py --detect
    python3 scripts/java_probe.py --runtime <...>/allure_runtime.py --java-home /usr/lib/jvm/java-21

Prints JSON: a candidate list for --detect, a probe record otherwise
(exit 1 when the probe fails).
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load_runtime(path: str):
    spec = importlib.util.spec_from_file_location(
        "allure_runtime_standalone", Path(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True,
                    help="path to allure_runtime.py in the payload")
    ap.add_argument("--java-home", default="")
    ap.add_argument("--detect", action="store_true",
                    help="list detected JAVA_HOME candidates")
    args = ap.parse_args()
    mod = load_runtime(args.runtime)
    if args.detect:
        print(json.dumps([str(h) for h in mod.detect_java_homes()]))
        return 0
    record = mod.probe(args.java_home or None)
    print(json.dumps(record))
    return 0 if record.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
