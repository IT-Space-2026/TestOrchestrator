# -*- mode: python ; coding: utf-8 -*-
"""Focused frozen build — web app + worker + flask CLI + migrations.
Run: pyinstaller --clean packaging/pyinstaller/app.spec
Output: dist/frozen/testorchestrator/ (onedir, no external python)."""
from PyInstaller.utils.hooks import collect_all, collect_data_files
import os
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

datas, binaries, hiddenimports = [], [], []
for pkg in ("testorchestrator", "flask", "flask_wtf", "wtforms",
            "flask_sqlalchemy", "flask_migrate", "flask_login",
            "sqlalchemy", "alembic", "waitress", "jinja2", "mako",
            "werkzeug", "click", "git", "gitdb", "smmap", "requests",
            "urllib3", "certifi", "charset_normalizer", "idna",
            "cryptography", "cffi", "argon2", "mako.template",
            "itsdangerous", "blinker", "markupsafe", "pycparser",
            "typing_extensions"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
# Source-tree package: hook collection skips it — ship data explicitly.
for src, dest in (("testorchestrator/templates","testorchestrator/templates"),
                  ("testorchestrator/static","testorchestrator/static"),
                  ("migrations","migrations"), ("release","release")):
    p = os.path.join(ROOT, src)
    for f in sorted(__import__("pathlib").Path(p).rglob("*")):
        if f.is_file():
            datas.append((str(f), dest if f.parent == __import__("pathlib").Path(p)
                          else str(f.parent.relative_to(ROOT))))

a = Analysis([os.path.join(ROOT, "wsgi.py")], pathex=[ROOT], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports +
             ["testorchestrator.main.routes","testorchestrator.portal.routes",
              "testorchestrator.system.routes","testorchestrator.agent_api",
              "testorchestrator.adapters.allure","testorchestrator.adapters.kre",
              "testorchestrator.adapters.dagu","testorchestrator.adapters.reportportal"],
             excludes=["tkinter","matplotlib","numpy","pandas","PyQt5","PyQt6","PySide2","PySide6","IPython","notebook","pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="toctl",
          console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="testorchestrator")
