#!/usr/bin/env python3
"""Dagu service provisioning (installer step). Persistent DAGU_HOME,
protected credentials, workflow placement, systemd unit, health check.
Honest scaffold — writes artifacts; service activation needs root."""
import argparse, os, secrets, shutil, subprocess
from pathlib import Path

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="/var/lib/dagu")
    ap.add_argument("--config", default="/etc/testorchestrator/dagu")
    ap.add_argument("--port", type=int, default=29877)
    ap.add_argument("--binary", default="3rdparty/dagu/dagu")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    home, cfg = Path(a.home), Path(a.config)
    user = "testorchestrator"
    # auth.mode: basic (verified vs bundled 2.25.8 — builtin's token.secret
    # is a JWT signing secret, not a static bearer credential).
    # Source the credential the portal adapter already provisioned so the
    # coordinator validates the SAME secret Orchestrator will send —
    # never a second random token the portal doesn't know.
    token = os.environ.get("TO_DAGU_PROVISION_PASSWORD")
    if not token:
        print("[dagu] WARNING: TO_DAGU_PROVISION_PASSWORD not set — "
              "coordinator gets a credential the portal can't match. "
              "Provision via the app's credential store instead.",
              file=sys.stderr)
        token = "to-dagu-" + secrets.token_urlsafe(32)
    steps = [
        ("mkdirs", home/"dags", home/"data"),
        ("install binary", cfg/"dagu"),
        ("server config", cfg/"server.yaml"),
        ("workflow", home/"dags"/"testorchestrator-execution.yaml"),
        ("credential file (0600)", cfg/"credentials"),
        ("systemd unit", cfg/"testorchestrator-dagu.service"),
    ]
    for s in steps:
        print(f"[dagu] {s[0]} -> {s[1]}")
    if a.dry_run: return
    for d in (home/"dags", home/"data", cfg): d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(a.binary, cfg/"dagu"); os.chmod(cfg/"dagu", 0o755)
    (cfg/"server.yaml").write_text(
        f"auth:\n  mode: basic\n  basic:\n"
        f"    username: {user}\n    password: {token}\n")
    os.chmod(cfg/"server.yaml", 0o600)
    (cfg/"credentials").write_text(f"username={user}\npassword={token}\n")
    os.chmod(cfg/"credentials", 0o600)
    shutil.copy2("deploy/dags/testorchestrator-execution.yaml",
                 home/"dags"/"testorchestrator-execution.yaml")
    (cfg/"testorchestrator-dagu.service").write_text(f"""[Unit]
Description=Dagu orchestrator (TestOrchestrator)
After=network.target
[Service]
Type=simple
ExecStart={cfg/'dagu'} start-all --port {a.port} --config {cfg/'server.yaml'} --dags {home/'dags'}
Environment=DAGU_HOME={home}
Restart=on-failure
[Install]
WantedBy=multi-user.target
""")
    print(f"[dagu] health: curl http://127.0.0.1:{a.port}/api/v1/dags")

if __name__ == "__main__":
    main()
