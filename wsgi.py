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

    serve(app, listen=app.config["WAITRESS_LISTEN"])
