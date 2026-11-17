"""Installed-package remote-worker E2E (2.0.2 acceptance).

Drives the REAL dispatch pipeline on an installed candidate package —
never the source checkout:

    seed      — schema, users, licence (self-signed for this test
                install's pubkey), dagu provision, coordinator settings,
                fixture KRE + source repo, runner/runtime/project
    download  — fetch the bundled Linux dagu worker archive THROUGH the
                installed app (authenticated route, checksum-verified)
    run       — start the provisioned coordinator + the downloaded
                worker, dispatch an execution, reconcile to terminal,
                collect + normalize + rlabs; print the evidence record
    verify    — re-verify persisted execution/report after restart

Layout (all under $TO_E2E_ROOT, default /tmp/to-e2e):
    inst/          TO_INSTANCE_DIR for the installed app
    dagu-home/     coordinator DAGU_HOME (+data,+dags)
    dagu-cfg/      provisioned server.yaml + dagu binary + credentials
    repo.git       bare source repo the worker checks out
    kre/katalonc   fixture KRE executable (emits junit + execution.uuid)
    worker/        downloaded + extracted dagu worker
    ws/            worker-side workspace root

Run from the payload root with the package's OWN bundled python:

    python worker_e2e.py seed
    python worker_e2e.py download
    python worker_e2e.py run

The fixture KRE is honestly labelled: it is NOT licensed Katalon
Runtime Engine — the licensed-KRE leg is a recorded blocker. What it
proves is the packaged worker asset: coordinator dispatch → worker
execution → artifact return → normalize → reporting.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

for _cand in (os.getcwd(),
              *[str(p) for p in Path(__file__).resolve().parents]):
    if (Path(_cand) / "testorchestrator" / "__init__.py").is_file():
        sys.path.insert(0, _cand)
        break

ROOT = Path(os.environ.get("TO_E2E_ROOT", "/tmp/to-e2e"))
INST = ROOT / "inst"
DAGU_HOME = ROOT / "dagu-home"
DAGU_CFG = ROOT / "dagu-cfg"
REPO = ROOT / "repo.git"
KRE_BIN = ROOT / "kre" / "katalonc"
WS = ROOT / "ws"
WORKER_DIR = ROOT / "worker"
COORD_PORT = int(os.environ.get("TO_E2E_COORD_PORT", "50055"))
DAGU_PORT = int(os.environ.get("TO_E2E_DAGU_PORT", "8081"))
LABEL = "e2e-202"
WORKER_ID = "e2e-worker-202"
PASSWORD = "E2ePassword-12345"

JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="{suite}" id="{suite}" hostname="e2e"
           timestamp="{ts}" tests="{n}" failures="{f}" errors="0"
           skipped="0" time="{t}">
{cases}
</testsuite>
"""

FIXTURE_KRE = r'''#!/usr/bin/env python3
"""Fixture KRE — NOT licensed Katalon Runtime Engine. Accepts the
katalonc argv shape and emits a JUnit report + execution.uuid +
supporting evidence into -reportFolder (honest fixture marker)."""
import re, sys, time
from pathlib import Path

args = " ".join(sys.argv[1:])
m = re.search(r"-reportFolder=(\S+)", args)
suite = (re.search(r"-testSuite(?:Collection)?Path=(\S+)", args)
         or [None, "TS_Fixture"])[1]
rd = Path(m.group(1) if m else "Reports") / "20261117_120000"
rd.mkdir(parents=True, exist_ok=True)
(rd / "execution.uuid").write_text(
    "e2e20202-0000-4000-8000-0000000000e2")
cases = "".join(
    f'<testcase classname="{suite}" name="case_{i}" time="0.5"/>\n'
    for i in range(1, 4))
(rd / "JUnit_Report.xml").write_text("""<?xml version="1.0"?>
<testsuite name="%s" id="%s" hostname="e2e" timestamp="2026-11-17T12:00:00"
           tests="3" failures="0" errors="0" skipped="0" time="1.5">
%s</testsuite>
""" % (suite, suite, cases))
(rd / "execution0.log").write_text(
    "fixture-kre: run complete (not licensed KRE)\n")
(rd / "screen1.png").write_bytes(b"\x89PNG\x00e2e")
sys.exit(0)
'''


def _ctx():
    from testorchestrator import create_app
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    return app, app.app_context()


def _licence(app):
    """Self-signed licence valid for THIS test payload's pubkey only —
    the payload's release/licence_pubkey.pem is replaced in `seed`
    (test install, never a shipped artifact)."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat)
    from testorchestrator import licensing as L

    key_file = INST / "e2e-priv.pem"
    if key_file.exists():
        priv = load_priv(key_file.read_bytes())
    else:
        priv = Ed25519PrivateKey.generate()
        key_file.write_bytes(priv.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    pub_pem = priv.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    # test payload only: OUR verify key lands in release/
    # licence_pubkey.pem so _load_production_key() registers it in
    # every process (web, worker, this script) — never a shipped
    # artifact, the file is restored by any real install/upgrade.
    import testorchestrator as _to
    pem_dst = (Path(_to.__file__).resolve().parent.parent
               / "release" / "licence_pubkey.pem")
    if pem_dst.exists() or pem_dst.parent.exists():
        pem_dst.write_bytes(pub_pem)
    L.register_key("prod", pub_pem)
    def b64(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    payload = {"version": 1, "key_id": "prod",
               "licence_id": "e2e-202", "product": "TestOrchestrator",
               "customer": "e2e",
               "valid_from": "2020-01-01T00:00:00+00:00",
               "valid_until": "2099-01-01T00:00:00+00:00"}
    canon = json.dumps(payload, sort_keys=True,
                       separators=(",", ":")).encode()
    ok, msg = L.add("TO1." + b64(canon) + "." + b64(priv.sign(canon)))
    assert ok, msg
    return pub_pem


def load_priv(pem):
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key)
    return load_pem_private_key(pem, password=None)


def seed():
    from testorchestrator import settings as _s
    from testorchestrator.execution import (
        DiscoveredItem, GitBinding, Project, Runner, RuntimeConnection)
    from testorchestrator.extensions import db
    from testorchestrator.models import Role, User
    from testorchestrator.secrets_store import store_secret
    from testorchestrator.settings import mark_setup_complete

    for d in (INST, DAGU_HOME / "dags", DAGU_HOME / "data", DAGU_CFG,
              WS, KRE_BIN.parent, WORKER_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # fixture KRE + source repo
    KRE_BIN.write_text(FIXTURE_KRE)
    KRE_BIN.chmod(0o755)
    if not REPO.exists():
        subprocess.run(["git", "init", "--bare", str(REPO)],
                       check=True, capture_output=True)
        wc = ROOT / "repo-wc"
        subprocess.run(["git", "clone", str(REPO), str(wc)],
                       check=True, capture_output=True)
        (wc / "P.prj").write_text("<Project/>")
        (wc / "Test Suites").mkdir(exist_ok=True)
        (wc / "Test Suites" / "TS_1.ts").write_text("<TestSuite/>")
        for c in (["git", "add", "-A"],
                  ["git", "-c", "user.email=e@e", "-c", "user.name=e",
                   "commit", "-m", "seed"],
                  ["git", "push", "origin", "master"]):
            subprocess.run(c, cwd=wc, check=True, capture_output=True)

    app, ctx = _ctx()
    with ctx:
        db.create_all()
        if User.query.filter_by(username="e2e-admin").first() is None:
            u = User(username="e2e-admin", role=Role.ADMIN)
            u.set_password(PASSWORD)
            db.session.add(u)
        mark_setup_complete()
        pub_pem = _licence(app)

        # provisioned coordinator credential (provision_dagu's token)
        creds = (DAGU_CFG / "credentials")
        user, token = "dagu", ""
        if creds.exists():
            for line in creds.read_text().splitlines():
                k, _, v = line.partition("=")
                if k == "username":
                    user = v
                elif k == "password":
                    token = v
        store_secret(app, "e2e-dagu", "basic", token)
        _s.update({
            "dagu_endpoint": f"http://127.0.0.1:{DAGU_PORT}",
            "dagu_username": user,
            "dagu_credential_ref": "e2e-dagu",
            "dagu_coordinator_url": f"127.0.0.1:{COORD_PORT}",
            "external_enabled": True,
            "advertised_base_url": "http://localhost",
        })
        _s.set("dagu_mandatory", True)   # unregistered toggle key
        p = Project(name="e2e-proj", description="")
        db.session.add(p)
        db.session.flush()
        b = GitBinding(project_id=p.id, name="src", url=str(REPO),
                       ref="master")
        db.session.add(b)
        db.session.flush()
        di = DiscoveredItem(project_id=p.id, binding_id=b.id,
                            kind="suite", path="Test Suites/TS_1.ts",
                            name="TS_1", sha="seed", order_idx=0)
        r = Runner.query.filter_by(name="e2e-remote").first()
        if r is None:
            r = Runner(name="e2e-remote", connection_mode="remote",
                       os_arch="linux", max_slots=2)
            db.session.add(r)
        db.session.flush()
        rt = RuntimeConnection(
            name="kre-e2e", runner_id=r.id,
            executable_path=str(KRE_BIN),
            workspace_root=str(WS),
            worker_label=LABEL, enabled=True)
        db.session.add_all([di, rt])
        db.session.commit()
        print(json.dumps({"project": p.id, "binding": b.id,
                          "item": di.id, "runtime": rt.id}))
        print("PASS seeded e2e environment")


def download():
    """Fetch the bundled worker archive via the installed app's
    authenticated route — checksum compared to the served value."""
    import hashlib
    from testorchestrator import licensing as L
    app, ctx = _ctx()
    with ctx:
        L.register_key("prod", _licence(app))
        c = app.test_client()
        r = c.post("/login", data={"username": "e2e-admin",
                                   "password": PASSWORD},
                   follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code
        name = "dagu_2.27.0_linux_amd64.tar.gz"
        r = c.get(f"/system/infrastructure/dagu-worker/{name}")
        assert r.status_code == 200, r.status_code
        data = r.data
        sha = hashlib.sha256(data).hexdigest()
        out = WORKER_DIR / name
        out.write_bytes(data)
        print(f"PASS downloaded {len(data)} bytes via app route "
              f"sha256={sha}")
        import tarfile
        with tarfile.open(out) as t:
            t.extractall(WORKER_DIR, filter="data")
        print(f"PASS extracted worker: {list(WORKER_DIR.iterdir())}")


def run():
    import requests as _rq
    from testorchestrator import dispatch, licensing as L
    from testorchestrator.execution import (
        DiscoveredItem, Execution, ExecutionState, RuntimeConnection)
    from testorchestrator.extensions import db

    app, ctx = _ctx()
    with ctx:
        L.register_key("prod", _licence(app))
        # coordinator
        dagu = DAGU_CFG / "dagu"
        server = subprocess.Popen(
            [str(dagu), "start-all",
             "--host", "127.0.0.1", "--port", str(DAGU_PORT),
             f"--coordinator.host=127.0.0.1",
             f"--coordinator.port={COORD_PORT}",
             f"--coordinator.advertise=127.0.0.1",
             "--config", str(DAGU_CFG / "server.yaml"),
             "--dags", str(DAGU_HOME / "dags")],
            env={**os.environ, "DAGU_HOME": str(DAGU_HOME),
                # dagu derives its temp DAG dir as $TMPDIR/dagu — keep
                # it inside our root (a stray file can shadow /tmp/dagu)
                "TMPDIR": str(ROOT / "tmp")},
            stdout=open(ROOT / "coordinator.log", "w"),
            stderr=subprocess.STDOUT)
        worker = None
        try:
            for _ in range(60):
                try:
                    if _rq.get(f"http://127.0.0.1:{DAGU_PORT}/api/v1/"
                               "health", timeout=2).ok:
                        break
                except Exception:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("coordinator did not come up")
            print("PASS coordinator up "
                  f"(http://127.0.0.1:{DAGU_PORT}, grpc :{COORD_PORT})")

            # downloaded worker
            dagu_w = WORKER_DIR / "dagu"
            ver = subprocess.run([str(dagu_w), "version"],
                                 capture_output=True, text=True)
            print(f"worker binary version: {ver.stdout.strip()}")
            worker = subprocess.Popen(
                [str(dagu_w), "worker",
                 f"--worker.coordinators=127.0.0.1:{COORD_PORT}",
                 f"--worker.labels", f"kre={LABEL}",
                 "--worker.id", WORKER_ID,
                 # health-check HTTP server off — test envs may have
                 # stale workers holding the default :8092
                 "--worker.health-port=0"],
                env={**os.environ, "DAGU_HOME": str(ROOT / "worker-home"),
                    "TMPDIR": str(ROOT / "tmp-w")},
                stdout=open(ROOT / "worker.log", "w"),
                stderr=subprocess.STDOUT)
            time.sleep(3)
            from testorchestrator.adapters.dagu import DaguAdapter
            workers = DaguAdapter.from_settings().workers()
            ids = [w.get("id") for w in workers]
            print(f"registered workers: {ids}")
            assert WORKER_ID in ids, workers

            # dispatch
            di = DiscoveredItem.query.first()
            rt = RuntimeConnection.query.first()
            ex = dispatch.create_execution(
                "e2e-admin", None,
                run_config={"runtime_id": rt.id},
                source_item=di)
            print(f"dispatched execution {ex.id} "
                  f"transport={ex.snapshot().get('dispatch_transport')}")

            deadline = time.time() + 300
            while time.time() < deadline:
                dispatch.worker_once()
                db.session.refresh(ex)
                if ex.state in (ExecutionState.COMPLETED,
                                ExecutionState.FAILED,
                                ExecutionState.CANCELED):
                    break
                time.sleep(2)
            db.session.refresh(ex)
            results = [{
                "case": r.case_name, "status": r.status.value}
                for r in ex.results]
            artifacts = [{"kind": a.kind, "path": a.rel_path}
                         for a in ex.artifacts]
            record = {
                "execution_id": ex.id, "state": ex.state.value,
                "outcome": ex.outcome.value if ex.outcome else None,
                "error": ex.error_text[:200], "results": results,
                "artifacts": artifacts,
                "report_dir": ex.report_dir,
            }
            # reporting-labs generation for the returned execution
            if ex.state == ExecutionState.COMPLETED:
                from testorchestrator.adapters import reporting_labs
                res = reporting_labs.generate_for(ex)
                record["rlabs"] = res.get("status")
                db.session.commit()
                record["rl_status"] = ex.rl_status
            print(json.dumps(record, indent=2))
            assert ex.state == ExecutionState.COMPLETED, record
            assert len(results) == 3
            assert any(a["kind"] == "junit" for a in artifacts)
            print("PASS remote worker dispatch -> result return")
        finally:
            for p in (worker, server):
                if p:
                    p.terminate()
                    try:
                        p.wait(10)
                    except Exception:
                        p.kill()


def verify():
    from testorchestrator import licensing as L
    from testorchestrator.execution import Execution
    from testorchestrator.extensions import db
    app, ctx = _ctx()
    with ctx:
        L.register_key("prod", _licence(app))
        ex = Execution.query.order_by(
            Execution.created_at.desc()).first()
        assert ex is not None and ex.state.value == "completed"
        assert ex.rl_status == "ready"
        assert Path(ex.report_dir or "").is_dir()
        print(f"PASS persisted execution {ex.id}: {ex.state.value}, "
              f"rlabs={ex.rl_status}")


if __name__ == "__main__":
    {"seed": seed, "download": download, "run": run,
     "verify": verify}[sys.argv[1]]()
