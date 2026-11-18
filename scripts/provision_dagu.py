#!/usr/bin/env python3
"""Dagu service provisioning (installer step). Persistent DAGU_HOME,
protected credentials, workflow placement, systemd unit, health check.
Honest scaffold — writes artifacts; service activation needs root."""
import argparse, os, secrets, shutil, sys
from pathlib import Path

def _read_existing_password(cfg: Path) -> str:
    """Recover the credential already provisioned in ``cfg`` so an
    installer rerun preserves the pair the portal's ``coordinator-agent``
    secret was synced to. Checks the installer-owned ``credentials``
    file first, then ``server.yaml``."""
    cred = cfg / "credentials"
    if cred.exists():
        for line in cred.read_text().splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    srv = cfg / "server.yaml"
    if srv.exists():
        for line in srv.read_text().splitlines():
            s = line.strip()
            if s.startswith("password:"):
                return s.split(":", 1)[1].strip()
    return ""

def _resolve_token(cfg: Path) -> tuple:
    """auth.mode: basic (verified vs bundled 2.27.0 — builtin's
    token.secret is a JWT signing secret, not a static bearer
    credential). Source the credential the portal adapter already
    provisioned so the coordinator validates the SAME secret
    Orchestrator will send — never a second random token the portal
    doesn't know. Precedence: explicit env override (rotation) >
    already-provisioned credential (rerun/switch/upgrade) > fresh
    random (first install)."""
    token = os.environ.get("TO_DAGU_PROVISION_PASSWORD")
    if token:
        return token, "env"
    token = _read_existing_password(cfg)
    if token:
        return token, "preserved"
    print("[dagu] WARNING: TO_DAGU_PROVISION_PASSWORD not set — "
          "coordinator gets a credential the portal can't match. "
          "Provision via the app's credential store instead.",
          file=sys.stderr)
    return "to-dagu-" + secrets.token_urlsafe(32), "generated"

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
    token, source = _resolve_token(cfg)
    if source == "preserved":
        print("[dagu] reusing already-provisioned coordinator credential")
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
