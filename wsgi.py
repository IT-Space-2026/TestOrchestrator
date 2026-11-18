"""Production WSGI entry point.

Production WSGI entry point — Waitress binds loopback for internal
probes; the in-process Cheroot tier owns the public listeners:

    waitress-serve --listen=127.0.0.1:8080 wsgi:app

Never run the Flask development server in production.
"""

import os

from testorchestrator import create_app

app = create_app()

if __name__ == "__main__":
    # Convenience for `python wsgi.py`; identical to waitress-serve above.
    from waitress import serve

    from testorchestrator import frontend, settings as settings_mod

    fh = None
    if frontend.enabled():
        # Cheroot opt-in tier: terminates public HTTPS and the
        # HTTP redirect listener in-process; Waitress keeps its
        # loopback listener for internal probes. A not-yet-migrated
        # database (first boot) must not block the listeners — fall
        # back to schema defaults, which match the packaged defaults.
        with app.app_context():
            try:
                # Env→DB bridge must run BEFORE the listeners bind:
                # before_request is too late — a migrated-but-unseeded
                # install would otherwise bind the packaged defaults
                # instead of the installer's ports.
                from testorchestrator.bootstrap import (
                    ensure_orchestrator_settings)
                ensure_orchestrator_settings()
                settings = settings_mod.all_settings()
            except Exception:
                app.logger.warning(
                    "frontend: settings unreadable (database not "
                    "migrated yet?) — using defaults")
                settings = dict(settings_mod.DEFAULTS)
                # First boot before the DB exists: the installer's
                # env still wins for the listener ports so a custom
                # --https-port install binds correctly on first start.
                for env_key, skey in (("TO_HTTPS_PORT", "https_port"),
                                      ("TO_HTTP_PORT", "http_port")):
                    if os.environ.get(env_key):
                        settings[skey] = int(os.environ[env_key])
            fh = frontend.serve(app, settings)
    try:
        serve(app, listen=app.config["WAITRESS_LISTEN"])
    except Exception:
        # Cheroot's worker threads are non-daemon: without a hard exit
        # the process would linger — public listeners still up while
        # systemd (Restart=on-failure) waits forever on a process that
        # never dies. Stop the frontend and die fast so the service
        # manager performs the restart.
        if fh is not None:
            app.logger.exception("frontend: backend serve failed — "
                                 "stopping frontend and exiting")
            fh.stop()
            os._exit(1)
        raise
