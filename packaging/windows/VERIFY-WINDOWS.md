# Windows runtime verification script (FOR A WINDOWS HOST — not run yet)

Run on a clean Windows 10/11 amd64 VM/host after installing
testorchestrator-1.0.0-pre.1-windows-amd64-setup-UNSIGNED.exe:

1. Embedded python + deps:
   & "$env:ProgramFiles\TestOrchestrator\python\python.exe" -c "import flask, sqlalchemy, waitress, argon2; print('imports ok')"
2. Startup: confirm scheduled tasks TestOrchestratorWeb/Worker/Dagu
   exist: schtasks /query /tn TestOrchestratorWeb
   After tasks run: GET http://localhost:8080/ -> login page.
3. Java/Allure: app-local JRE at $INSTDIR\jre\bin\java.exe -version;
   Allure report generation from an execution's results dir.
4. Worker downloads: System -> KRE installations -> Download Dagu
   worker; verify SHA-256 against the published checksum.
5. Restart controls: the portal's System -> Health -> Restart must
   map to `schtasks /End` + `/Run` on the task names — NOT systemctl.
   KNOWN GAP: the current restart path is systemd-only; a schtasks
   backend for the same configured service set is pending — the UI
   must report "unmanaged" on Windows until it exists.
6. Upgrade: install over an existing install; verify
   %ProgramData%\TestOrchestrator (db, config) is preserved.
7. Uninstall: program files removed; %ProgramData% kept unless the
   operator opts in; scheduled tasks deleted.
