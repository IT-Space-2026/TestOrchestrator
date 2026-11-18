"""Production WSGI entry point.

Deployed behind Caddy; Waitress binds loopback only:

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
                settings = settings_mod.all_settings()
            except Exception:
                app.logger.warning(
                    "frontend: settings unreadable (database not "
                    "migrated yet?) — using defaults")
                settings = dict(settings_mod.DEFAULTS)
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
