"""Worker entrypoint for the multi-process schedule contention test.

Spawned as `python tests/contention_worker.py <instance_dir> <worker_id>` —
each process builds its own app + own DB connection to the SAME sqlite
file, waits on the `go` sentinel file, then fires due schedules and
records its fired count. This is a real cross-process race, not a
thread sharing one engine.
"""

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Test-side injection: contention workers run a licensed scenario —
# bypass is injected here (test code), never via a production env var.
from testorchestrator import licensing
licensing.usable = lambda *a, **k: True


def main():
    tmp = Path(sys.argv[1])
    wid = sys.argv[2]
    go = tmp / "go"
    deadline = time.time() + 30
    while not go.exists():
        if time.time() > deadline:
            (tmp / f"result-{wid}").write_text("TIMEOUT")
            return
        time.sleep(0.005)
    from tests.conftest import make_app
    app = make_app(tmp)
    with app.app_context():
        from testorchestrator import dispatch
        try:
            n = dispatch.fire_due_schedules()
        except Exception as exc:                    # pragma: no cover
            n = f"ERR:{type(exc).__name__}"
    (tmp / f"result-{wid}").write_text(str(n))


if __name__ == "__main__":
    main()
