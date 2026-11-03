#!/usr/bin/env python3
"""Dagu service provisioning (installer step). Persistent DAGU_HOME,
protected credentials, workflow placement, systemd unit, health check.
Honest scaffold — writes artifacts; service activation needs root."""
import argparse, os, secrets, shutil, subprocess
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="/var/lib/dagu")
    ap.add_argument("--config", default="/etc/testorchestrator/dagu")
    ap.add_argument("--port", type=int, default=29877)
    ap.add_argument("--binary", default="3rdparty/dagu/dagu")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    home, cfg = Path(a.home), Path(a.config)
    token = "to-dagu-" + secrets.token_urlsafe(32)
    steps = [
        ("mkdirs", home/"dags", home/"data"),
        ("install binary", cfg/"dagu"),
        ("server config", cfg/"server.yaml"),
        ("workflow", home/"dags"/"testorchestrator-execution.yaml"),
        ("token file (0600)", cfg/"token"),
        ("systemd unit", cfg/"testorchestrator-dagu.service"),
    ]
    for s in steps:
        print(f"[dagu] {s[0]} -> {s[1]}")
    if a.dry_run: return
    for d in (home/"dags", home/"data", cfg): d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(a.binary, cfg/"dagu"); os.chmod(cfg/"dagu", 0o755)
    (cfg/"server.yaml").write_text(f"auth:\n  mode: builtin\n  token:\n    secret: {token}\n")
    (cfg/"token").write_text(token); os.chmod(cfg/"token", 0o600)
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
