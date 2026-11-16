; TestOrchestrator FULL application installer — Windows amd64
; Complete product: app + embedded Python + Dagu + Node.js + Caddy.
; The KRE execution engine is installed/licensed SEPARATELY — not
; redistributed; the installer registers the user's installation.
; UNSIGNED build — labelled clearly.
!define PRODUCT "TestOrchestrator"
!define VERSION "2.0.0"
!define PAYLOAD_DIR "app-payload"

Name "${PRODUCT} ${VERSION} (unsigned)"
OutFile "testorchestrator-${VERSION}-windows-amd64-setup-UNSIGNED.exe"
InstallDir "$PROGRAMFILES64\TestOrchestrator"
RequestExecutionLevel admin
ShowInstDetails show
Var /GLOBAL DATA_DIR
Var /GLOBAL ORCH_PORT
Var /GLOBAL PUBLIC_ADDR
Var /GLOBAL PURGE_DATA

!include "MUI2.nsh"
!define MUI_ICON "it.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "wizard.bmp"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

Section "Install TestOrchestrator" SecMain
    SetOutPath "$INSTDIR"
    File /r "${PAYLOAD_DIR}\*.*"

    ; Upgrade cleanup: 2.0 dropped bundled Allure/Node. Remove ONLY
    ; application-owned runtime dirs from a previous install —
    ; system-wide Node.js and other software are never touched.
    RMDir /r "$INSTDIR\nodejs"
    RMDir /r "$INSTDIR\allure3"
    ReadEnvStr $DATA_DIR "ProgramData"
    StrCpy $DATA_DIR "$DATA_DIR\TestOrchestrator"
    CreateDirectory "$DATA_DIR"
    CreateDirectory "$DATA_DIR\instance"
    CreateDirectory "$DATA_DIR\logs"
    CreateDirectory "$DATA_DIR\home"
    CreateDirectory "$DATA_DIR\caddy-home"
    CreateDirectory "$DATA_DIR\dagu"
    CreateDirectory "$DATA_DIR\dagu-cfg"
    ; private CA key, database/licence state, and dagu credentials are
    ; not world-readable: strip inherited Users access on those dirs
    nsExec::ExecToLog 'icacls "$DATA_DIR\caddy-home" /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"'
    nsExec::ExecToLog 'icacls "$DATA_DIR\instance" /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"'
    nsExec::ExecToLog 'icacls "$DATA_DIR\dagu" /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"'
    nsExec::ExecToLog 'icacls "$DATA_DIR\dagu-cfg" /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"'

    ${GetOptions} $CMDLINE "/PORT=" $ORCH_PORT
    ${GetOptions} $CMDLINE "/ADDR=" $PUBLIC_ADDR
    ${If} $ORCH_PORT == ""
        StrCpy $ORCH_PORT "8080"
    ${EndIf}
    ${If} $PUBLIC_ADDR == ""
        StrCpy $PUBLIC_ADDR "localhost"
    ${EndIf}

    ; application env — internal component wiring is generated, not
    ; user-configured (application-local nodejs/python/dagu paths).
    FileOpen $0 "$DATA_DIR\testorchestrator.env" w
    FileWrite $0 "TO_INSTANCE_DIR=$DATA_DIR\instance$\r$\n"
    FileWrite $0 "TO_DATABASE_URI=sqlite:///$DATA_DIR\instance\to.db$\r$\n"
    FileWrite $0 "TO_PUBLIC_ADDR=$PUBLIC_ADDR$\r$\n"
    FileWrite $0 "TO_PORT=$ORCH_PORT$\r$\n"
    FileClose $0

    ; wire vendored site-packages into the embeddable runtime —
    ; ._pth entries resolve relative to the python dir, and wheels are
    ; extracted to $INSTDIR\site-packages
    FileOpen $1 "$INSTDIR\python\python312._pth" a
    FileSeek $1 0 END
    FileWrite $1 "..\site-packages$\r$\n"
    FileWrite $1 "..$\r$\n"
    FileClose $1
    ; git is not bundled: default backend is Dulwich (no executable);
    ; TO_GIT_BACKEND=cli needs a separately installed Git.
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m testorchestrator db-upgrade'
    Pop $0

    ; Caddy reverse proxy config (HTTPS :443, redirect-only HTTP :8080 —
    ; env placeholders carry localhost + tls-internal defaults)
    CopyFiles "$INSTDIR\deploy\Caddyfile" "$DATA_DIR\Caddyfile"

    ; Dagu coordinator provisioning (dirs, basic-auth server.yaml,
    ; workflow) — mirrors scripts/install.py on Linux
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" "$INSTDIR\provision_dagu.py" --home "$DATA_DIR\dagu" --config "$DATA_DIR\dagu-cfg" --binary "$INSTDIR\dagu\dagu.exe"'
    Pop $0
    ; task-friendly launchers: Task Scheduler sets no env, working
    ; directory, or stdout capture — wrap each service in a .cmd that
    ; appends output to $DATA_DIR\logs (the journald equivalent)
    FileOpen $0 "$DATA_DIR\run_web.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 '"$INSTDIR\python\python.exe" -u "$INSTDIR\wsgi.py" >> "$DATA_DIR\logs\web.log" 2>&1$\r$\n'
    FileClose $0
    FileOpen $0 "$DATA_DIR\run_worker.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 '"$INSTDIR\python\python.exe" -u "$INSTDIR\run_worker.py" >> "$DATA_DIR\logs\worker.log" 2>&1$\r$\n'
    FileClose $0
    FileOpen $0 "$DATA_DIR\run_dagu.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 "set DAGU_HOME=$DATA_DIR\dagu$\r$\n"
    FileWrite $0 '"$INSTDIR\dagu\dagu.exe" start-all --port 29877 --config "$DATA_DIR\dagu-cfg\server.yaml" --dags "$DATA_DIR\dagu\dags" >> "$DATA_DIR\logs\dagu.log" 2>&1$\r$\n'
    FileClose $0
    FileOpen $0 "$DATA_DIR\run_caddy.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    ; pin Caddy storage under ProgramData — the per-installation local CA
    ; then lives at a deterministic path for the SYSTEM-run instance
    FileWrite $0 'set "APPDATA=$DATA_DIR\caddy-home"$\r$\n'
    FileWrite $0 '"$INSTDIR\caddy\caddy.exe" run --config "$DATA_DIR\Caddyfile" --adapter caddyfile >> "$DATA_DIR\logs\caddy.log" 2>&1$\r$\n'
    FileClose $0
    ; supported stop: end the tasks AND reap orphaned children —
    ; schtasks /End alone kills only the .cmd host and leaves the
    ; python/caddy/dagu children bound to their ports
    FileOpen $0 "$DATA_DIR\stop_to.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 'schtasks /End /TN "TestOrchestratorWeb"$\r$\n'
    FileWrite $0 'schtasks /End /TN "TestOrchestratorWorker"$\r$\n'
    FileWrite $0 'schtasks /End /TN "TestOrchestratorDagu"$\r$\n'
    FileWrite $0 'schtasks /End /TN "TestOrchestratorCaddy"$\r$\n'
    FileWrite $0 "powershell -NoProfile -Command $\"$$ps = Get-CimInstance Win32_Process | Where-Object { $$_.CommandLine -and $$_.CommandLine.IndexOf('$INSTDIR',[StringComparison]::OrdinalIgnoreCase) -ge 0 }; $$ps | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }; $$ps | ForEach-Object { Wait-Process -Id $$_.ProcessId -Timeout 20 -ErrorAction SilentlyContinue }$\"$\r$\n"
    FileClose $0
    ; uninstall helper: untrust this installation's root CA by thumbprint
    ; (the CA is unique per install — never a shared/distributed key)
    ; supported headless uninstall — self-contained: the NSIS silent
    ; uninstaller (temp-copy or _?=) silently no-ops in headless/Session-0
    ; contexts, so the wrapper performs the same steps deterministically.
    ; It first self-relocates to %TEMP%: PURGE must delete $DATA_DIR,
    ; which cannot be removed while the running .cmd lives inside it —
    ; the temp copy then purges synchronously (detached 'start' is not
    ; reliable in headless/Session-0 contexts).
    ; Interactive uninstall via uninstall.exe / Add-Remove remains the
    ; desktop path.
    FileOpen $0 "$DATA_DIR\uninstall_to.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 'if "%~1"=="/IN-TEMP" goto :run$\r$\n'
    FileWrite $0 'copy /y "%~f0" "%TEMP%\to-uninstall.cmd" >nul$\r$\n'
    FileWrite $0 '"%TEMP%\to-uninstall.cmd" /IN-TEMP %*$\r$\n'
    FileWrite $0 "exit /b$\r$\n"
    FileWrite $0 ":run$\r$\n"
    FileWrite $0 'call "$DATA_DIR\untrust_caddy.cmd"$\r$\n'
    FileWrite $0 'call "$DATA_DIR\stop_to.cmd"$\r$\n'
    FileWrite $0 'schtasks /Delete /TN "TestOrchestratorWeb" /F$\r$\n'
    FileWrite $0 'schtasks /Delete /TN "TestOrchestratorWorker" /F$\r$\n'
    FileWrite $0 'schtasks /Delete /TN "TestOrchestratorDagu" /F$\r$\n'
    FileWrite $0 'schtasks /Delete /TN "TestOrchestratorCaddy" /F$\r$\n'
    FileWrite $0 'reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" /f >nul 2>&1$\r$\n'
    FileWrite $0 'rmdir /s /q "$INSTDIR"$\r$\n'
    FileWrite $0 'echo %* | findstr /i /c:"/PURGE" >nul && rmdir /s /q "$DATA_DIR"$\r$\n'
    FileWrite $0 "exit /b 0$\r$\n"
    FileClose $0
    FileOpen $0 "$DATA_DIR\untrust_caddy.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 "powershell -NoProfile -Command $\"if(Test-Path '$DATA_DIR\caddy-home\Caddy\pki\authorities\local\root.crt'){$$t=(New-Object System.Security.Cryptography.X509Certificates.X509Certificate2('$DATA_DIR\caddy-home\Caddy\pki\authorities\local\root.crt')).Thumbprint; Remove-Item -Path ('HKLM:\SOFTWARE\Microsoft\SystemCertificates\Root\Certificates\'+$$t) -Recurse -Force -ErrorAction SilentlyContinue}$\"$\r$\n"
    FileClose $0

    ; scheduled tasks for web + worker + dagu + caddy (start at boot)
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorWeb" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$DATA_DIR\run_web.cmd\""'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorWorker" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$DATA_DIR\run_worker.cmd\""'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorDagu" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$DATA_DIR\run_dagu.cmd\""'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorCaddy" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$DATA_DIR\run_caddy.cmd\""'
    Pop $0

    ; start now — the app is usable immediately after install,
    ; no reboot required
    nsExec::ExecToLog 'schtasks /Run /TN "TestOrchestratorWeb"'
    Pop $0
    nsExec::ExecToLog 'schtasks /Run /TN "TestOrchestratorWorker"'
    Pop $0
    nsExec::ExecToLog 'schtasks /Run /TN "TestOrchestratorDagu"'
    Pop $0
    nsExec::ExecToLog 'schtasks /Run /TN "TestOrchestratorCaddy"'
    Pop $0

    ; Trust this installation's generated Caddy root CA in the
    ; LocalMachine store so client browsers accept https://localhost
    ; without certificate-error bypasses. The CA is generated fresh per
    ; installation by Caddy itself — no private CA key is distributed
    ; in or shared across packages.
    StrCpy $2 0
    ${Do}
        ${If} ${FileExists} "$DATA_DIR\caddy-home\Caddy\pki\authorities\local\root.crt"
            ${ExitDo}
        ${EndIf}
        IntOp $2 $2 + 1
        ${If} $2 >= 30
            ${ExitDo}
        ${EndIf}
        Sleep 1000
    ${Loop}
    StrCpy $3 ""
    ${IfNot} ${FileExists} "$DATA_DIR\caddy-home\Caddy\pki\authorities\local\root.crt"
        StrCpy $3 "Caddy did not generate its local CA within 30s"
    ${Else}
        nsExec::ExecToLog 'certutil -addstore -f Root "$DATA_DIR\caddy-home\Caddy\pki\authorities\local\root.crt"'
        Pop $0
        ${If} $0 != 0
            StrCpy $3 "certutil -addstore Root exited $0"
        ${EndIf}
    ${EndIf}
    ${If} $3 != ""
        DetailPrint "Certificate trust NOT configured: $3"
        FileOpen $0 "$DATA_DIR\CERT-TRUST-NOT-INSTALLED.txt" w
        FileWrite $0 "Automatic HTTPS trust failed: $3$\r$\n$\r$\nBrowsers will warn on https://localhost until the generated$\r$\nroot CA is trusted manually:$\r$\n  certutil -addstore -f Root $\"$DATA_DIR\caddy-home\Caddy\pki\authorities\local\root.crt$\"$\r$\n"
        FileClose $0
        ${IfNot} ${Silent}
            MessageBox MB_ICONEXCLAMATION|MB_OK "HTTPS certificate trust was not configured ($3).$\r$\n$\r$\nSee $DATA_DIR\CERT-TRUST-NOT-INSTALLED.txt for the manual step."
        ${EndIf}
    ${EndIf}

    WriteUninstaller "$INSTDIR\uninstall.exe"
    ; NSIS is 32-bit: without SetRegView, HKLM\SOFTWARE writes land in
    ; WOW6432Node — register ARP in the native 64-bit view
    SetRegView 64
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "DisplayName" "${PRODUCT} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "DisplayVersion" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "Publisher" "IT-Space"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "DisplayIcon" "$INSTDIR\uninstall.exe"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "NoRepair" 1
    ; interactive desktop flow — pending real-host validation (quoted:
    ; $INSTDIR may contain spaces)
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "UninstallString" '"$INSTDIR\uninstall.exe"'
    ; silent/scripted flow — deterministic wrapper (data retained; /PURGE
    ; is an explicit admin choice, not a quiet default)
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "QuietUninstallString" 'cmd /c "$DATA_DIR\uninstall_to.cmd"'
SectionEnd

Function un.onInit
    ; $DATA_DIR is a runtime variable — it is NOT carried over from the
    ; install process; re-derive it so untrust/RMDir act on real paths.
    SetRegView 64
    ReadEnvStr $DATA_DIR "ProgramData"
    StrCpy $DATA_DIR "$DATA_DIR\TestOrchestrator"
    StrCpy $PURGE_DATA "0"
    ${un.GetOptions} $CMDLINE "/PURGEDATA=" $PURGE_DATA
FunctionEnd

Section "Uninstall"
    ; remove this installation's root CA from the machine trust store —
    ; without this a deleted private CA would leave a stale trust entry
    nsExec::ExecToLog '"$DATA_DIR\untrust_caddy.cmd"'
    Pop $0
    nsExec::ExecToLog '"$DATA_DIR\stop_to.cmd"'
    Pop $0
    ; give killed children a beat to release file locks before RMDir
    Sleep 1500
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorWeb" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorWorker" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorDagu" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorCaddy" /F'
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator"
    RMDir /r "$INSTDIR"
    ${If} $PURGE_DATA == "1"
        RMDir /r "$DATA_DIR"
    ${Else}
        MessageBox MB_YESNO "Also delete configuration, database, and workspaces in $DATA_DIR?" /SD IDNO IDNO keepData
            RMDir /r "$DATA_DIR"
        keepData:
    ${EndIf}
SectionEnd
