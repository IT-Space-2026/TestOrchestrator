"""Network configuration staged apply — Cheroot frontend era.

The Cheroot web tier renders listeners purely from stored settings at
service start — there is no intermediate config file to stage and no
external validator binary. Apply semantics therefore reduce to:

- snapshot ``previous_good_config`` for rollback (same contract as the
  Caddy era: the UI button or ``flask network-rollback`` restores it),
- bump ``network_config_version`` for audit traceability,
- report which changes need a service restart: listener binds
  (ports/host labels/http_mode) take effect on the next start;
  certificate changes are picked up live by the cert watcher.

Settings are validated upstream (``network.check_port_conflicts`` and
the form validators) before this is called — nothing here trusts
request input.
"""

from . import settings as settings_mod


class NetApplyError(RuntimeError):
    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


_SNAPSHOT_KEYS = (
    "https_port", "http_port", "http_mode", "local_host",
    "external_enabled", "external_host", "certificate_mode",
)


def apply_config(app, settings: dict) -> dict:
    """Record the new network configuration as applied.

    Returns a result dict for the UI/audit log. ``restart_required``
    flags that listener settings only bind at service start — the
    Network page prompts a restart; certificates reload live."""
    # previous-good = the config the running service was last started
    # with (the prior applied snapshot). The routes store the candidate
    # BEFORE apply runs, so the candidate itself can't seed it — first
    # apply falls back to current stored settings (the running config
    # until proven otherwise).
    prev = settings_mod.get("applied_network_config") or {
        key: settings_mod.get(key) for key in _SNAPSHOT_KEYS
    }
    prev["network_config_version"] = settings_mod.get(
        "network_config_version", 0)
    settings_mod.set("previous_good_config", prev)
    settings_mod.set(
        "applied_network_config",
        {key: settings[key] for key in _SNAPSHOT_KEYS
         if key in settings})
    settings_mod.set(
        "network_config_version",
        int(settings.get("network_config_version", 0)) + 1,
    )
    settings_mod.db.session.commit()
    return {
        "status": "applied",
        "restart_required": True,
        "version": settings_mod.get("network_config_version"),
    }


def rollback_config(app) -> dict:
    """Restore the last known good snapshot — takes effect on the next
    service start, same as any other network change."""
    prev = settings_mod.get("previous_good_config") or {}
    if not prev:
        raise NetApplyError("no previous good configuration to roll "
                            "back to")
    for key, value in prev.items():
        if key == "network_config_version":
            continue  # version only ever increments forward
        settings_mod.set(key, value)
    settings_mod.set(
        "network_config_version",
        int(settings_mod.get("network_config_version", 0)) + 1,
    )
    settings_mod.db.session.commit()
    return {"status": "rolled-back", "restored": prev}
