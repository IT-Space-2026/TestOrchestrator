; TestOrchestrator FULL application installer — Windows amd64
; Complete product: app + embedded Python + Dagu + Node.js + Caddy.
; The KRE execution engine is installed/licensed SEPARATELY — not
; redistributed; the installer registers the user's installation.
; UNSIGNED build — labelled clearly.
!define PRODUCT "TestOrchestrator"
!define VERSION "1.0.0"
!define PAYLOAD_DIR "app-payload"

Name "${PRODUCT} ${VERSION} (unsigned)"
OutFile "testorchestrator-${VERSION}-windows-amd64-setup-UNSIGNED.exe"
InstallDir "$PROGRAMFILES64\TestOrchestrator"
RequestExecutionLevel admin
ShowInstDetails show
Var /GLOBAL DATA_DIR
Var /GLOBAL ORCH_PORT
Var /GLOBAL PUBLIC_ADDR

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
    ReadEnvStr $DATA_DIR "ProgramData"
    StrCpy $DATA_DIR "$DATA_DIR\TestOrchestrator"
    CreateDirectory "$DATA_DIR"
    CreateDirectory "$DATA_DIR\instance"
    CreateDirectory "$DATA_DIR\logs"
    CreateDirectory "$DATA_DIR\home"

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
    ; task-friendly launcher: DAGU_HOME + flags (Task Scheduler sets
    ; no env or working directory)
    FileOpen $0 "$DATA_DIR\run_dagu.cmd" w
    FileWrite $0 "@echo off$\r$\n"
    FileWrite $0 "set DAGU_HOME=$DATA_DIR\dagu$\r$\n"
    FileWrite $0 '"$INSTDIR\dagu\dagu.exe" start-all --port 29877 --config "$DATA_DIR\dagu-cfg\server.yaml" --dags "$DATA_DIR\dagu\dags"$\r$\n'
    FileClose $0

    ; scheduled tasks for web + worker + dagu + caddy (start at boot)
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorWeb" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\python\python.exe\" \"$INSTDIR\wsgi.py\""'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorWorker" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\python\python.exe\" \"$INSTDIR\run_worker.py\""'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorDagu" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$DATA_DIR\run_dagu.cmd\""'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorCaddy" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\caddy\caddy.exe\" run --config \"$DATA_DIR\Caddyfile\" --adapter caddyfile"'
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

    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "DisplayName" "${PRODUCT} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "UninstallString" "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorWeb"'
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorWorker"'
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorDagu"'
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorCaddy"'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorWeb" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorWorker" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorDagu" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorCaddy" /F'
    RMDir /r "$INSTDIR"
    MessageBox MB_YESNO "Also delete configuration, database, and workspaces in $DATA_DIR?" /SD IDNO IDNO keepData
        RMDir /r "$DATA_DIR"
    keepData:
SectionEnd
