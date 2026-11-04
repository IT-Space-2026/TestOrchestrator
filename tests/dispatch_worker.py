"""Worker entrypoint for multi-process dispatch contention tests.

`python tests/dispatch_worker.py <instance_dir> <wid> <ex_id>` — each
process builds its own app + DB connection to the SAME sqlite file,
waits on the `go` sentinel, then runs dispatch.run_execution on the
given execution id and records the resulting state. Real cross-process
races, not threads sharing one engine.
"""

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main():
    tmp = Path(sys.argv[1])
    wid = sys.argv[2]
    ex_id = sys.argv[3]
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
        from testorchestrator.execution import Execution
        try:
            dispatch.run_execution(ex_id)
            state = Execution.query.get(ex_id)
            out = state.state.value if state else "MISSING"
        except Exception as exc:                    # pragma: no cover
            out = f"ERR:{type(exc).__name__}"
    (tmp / f"result-{wid}").write_text(out)


if __name__ == "__main__":
    main()
