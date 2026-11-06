# Dagu worker disconnect — controlled loopback test plan (awaiting approval)

Graceful SIGTERM shutdown is demonstrated (run daf252dd). NOT yet
exercised: hard kill and network partition. This plan uses ONLY the
loopback fixture (coordinator 127.0.0.1:29987, gRPC 50055) — no external
worker connection. Approval required before restarting workers.

## Fixture reset
    tar xzf 3rdparty/dagu/dagu_2.25.8_linux_amd64.tar.gz -C /tmp/to-fx-coord/bin dagu
    DAGU_HOME=/tmp/to-fx-coord/home /tmp/to-fx-coord/bin/dagu start-all \
      --port 29987 --host 127.0.0.1 --config /tmp/to-fx-coord/server.yaml \
      --coordinator.advertise=127.0.0.1 --dags /tmp/to-fx-coord/home/dags &
    /tmp/to-fx-coord/bin/dagu worker --worker.coordinators=127.0.0.1:50055 \
      --worker.labels kre=fixture-1 --worker.id fixture-worker-1 \
      --worker.health-port 0 &
Isolated portal: TO_INSTANCE_DIR=/tmp/to-fx-portal
TO_DATABASE_URI=sqlite:////tmp/to-fx-portal/fx.db (existing fx.db has the
demo executions + evidence; keep it or start a fresh db file alongside).

## Case A — hard kill (SIGKILL) mid-run
1. Submit a 120s-sleep fixture execution (executable_path=/tmp/to-fx-kre/katalonc-sleep).
   Record execution id + dagu run id + worker child PIDs (pgrep -f katalonc-sleep).
2. `kill -9 <worker-pid>` — the coordinator loses the worker channel.
3. Assert over t+5s..t+120s: dagu run statusLabel; whether katalonc-sleep
   survives orphaned (it likely does — SIGKILL cannot run the worker's
   shutdown handler); when coordinator marks run aborted/failed (heartbeat
   timeout, NOT worker_wait_timeout which bounds queue-wait only).
4. Portal reconcile: execution must reach FAILED/infrastructure or UNKNOWN
   — never auto-redispatch; record orphan cleanup requirement (kill -9 the
   sleep by hand; document it as operator action).
5. Restart worker; reconnect; confirm run does NOT resume/re-execute and no
   second invocation occurs (runningTasks empty for that dagRunId, single
   attempt-id in coordinator logs).

## Case B — network partition (worker cannot reach coordinator)
1. Same running fixture.
2. Partition loopback gRPC only: e.g. `sudo iptables -A OUTPUT -p tcp --dport 50055 -j DROP`
   scoped to the worker UID, OR run worker in `unshare -n` netns — needs
   approval + cleanup step documented before use.
3. Assert: worker-side consecutive-failure backoff (seen in logs:
   "CoordinatorCli connection recovered ... previous-consecutive-failures"),
   remote process keeps running during partition (check pid alive), dagu
   run status during/after heal.
4. Heal partition; worker reconnects — does it finish the run and report
   artifacts late? Portal must collect the late evidence and end COMPLETED
   or honestly-marked — never duplicate.

## What counts as proof (not "one portal row")
- coordinator log: single attempt-id; no second "Task dispatched" for the
  same dagRunId after reconnect.
- /api/v1/workers runningTasks listing before/after.
- worker log: step lifecycle for the single attempt.
- portal: single execution row + artifacts match one run's evidence.

## Cleanup
kill only fixture PIDs; drop iptables rule; keep logs under /tmp/to-fx-*.
