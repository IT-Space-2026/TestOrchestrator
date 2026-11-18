"""Production WSGI entry point.

Deployed behind Caddy; Waitress binds loopback only:

    waitress-serve --listen=127.0.0.1:8080 wsgi:app

Never run the Flask development server in production.
"""

from testorchestrator import create_app

app = create_app()

if __name__ == "__main__":
    # Convenience for `python wsgi.py`; identical to waitress-serve above.
    from waitress import serve

    from testorchestrator import frontend, settings as settings_mod

    if frontend.enabled():
        # Opt-in spike tier: Cheroot terminates public HTTPS and the
        # HTTP redirect listener in-process; Waitress keeps its
        # loopback listener for internal probes.
        with app.app_context():
            frontend.serve(app, settings_mod.all_settings())
    serve(app, listen=app.config["WAITRESS_LISTEN"])
