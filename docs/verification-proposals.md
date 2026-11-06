# Exact verification proposals (awaiting approval before execution)

## A. Isolated managed-install + restart verification
Goal: prove "Restart services" survives portal termination, restarts
managed services, writes result, restores health — WITHOUT touching
the development instance or host services.

Uses a **user-scope systemd** run (real systemd, non-root, no host
changes — cleanup removes only units we create):
    # 1. stage isolated install
    mkdir -p /tmp/mgd/{home,bin}
    tar xzf dist/testorchestrator-1.9.1-pre2-linux-x86_64.tar.gz -C /tmp/mgd
    # 2. fake managed units (user systemd, same names as package units)
    for u in testorchestrator-web testorchestrator-worker \
             testorchestrator-dagu testorchestrator-caddy; do
      printf '[Service]\nExecStart=/bin/sleep infinity\n' \
        > ~/.config/systemd/user/$u.service
    done
    systemctl --user daemon-reload
    systemctl --user start testorchestrator-web testorchestrator-worker \
      testorchestrator-dagu testorchestrator-caddy
    systemctl --user is-active testorchestrator-web   # -> active
    # 3. portal on loopback with the isolated instance dir
    TO_INSTANCE_DIR=/tmp/mgd/home TO_DATABASE_URI=sqlite:////tmp/mgd/home/m.db \
      waitress-serve --listen=127.0.0.1:39991 wsgi:app &
    # 4. POST /system/services/restart as admin; helper must run
    #    `systemctl --user restart` — REQUIRES the helper to detect
    #    user-vs-system scope (currently calls bare systemctl).
    # 5. assert: portal dies/restarts, marker result=done, units active.
Cleanup: systemctl --user stop <units>; rm ~/.config/systemd/user/testorchestrator-*;
    rm -rf /tmp/mgd; fuser -k 39991/tcp
CAVEAT: validates the restart PATH + writeback on user units; the
packaged install uses system units — same mechanism, different scope.
A full system-unit test needs a disposable VM/container.

## B. Loopback worker run returning a viewable report
Rebuild the approved loopback fixture (coordinator 29987/grpc 50055 +
worker kre=fixture-1), seed a fixture KRE that writes BOTH junit.xml
and an index.html report entry into the run's artifact dir, run a real
execution end-to-end, then GET /executions/<id>/report (200) and
/artifacts.zip + checksum-verify downloaded artifacts. Commands =
HANDOFF.md reproduction block; fixture dirs /tmp/to-fx-* (preserved).

## C. Hard-crash / network partition
Per docs/dagu-disconnect-test-plan.md:
    kill -9 <worker-pid>          # SIGKILL mid-run (orphan fate,
                                  # heartbeat-loss abort, no re-invoke)
    # partition (needs sudo + cleanup): block ONLY gRPC to worker
    sudo iptables -A OUTPUT -p tcp --dport 50055 -j DROP   # heal: -D
Both cases assert single attempt-id, late artifacts, honest state.
