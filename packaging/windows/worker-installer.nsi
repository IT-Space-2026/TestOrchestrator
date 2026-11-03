; TestOrchestrator Worker installer — Windows Server 2025 (worker/execution role)
; Build: makensis worker-installer.nsi  (see build_worker_installer.sh)
; Tool choice: NSIS — produces a real .exe (not a renamed archive), is
; scriptable/UAC-aware, runs unattended (/S), and can be built on Linux.
;
; This installer covers the WORKER role only. It does NOT contain the
; central Orchestrator server stack — a Windows central-server role is
; out of scope until scoped and validated separately.
;
; UNSIGNED BUILD — labelled clearly; do not sign until an approved
; signing identity exists, and never ask users to disable Windows
; security controls.

!define PRODUCT "TestOrchestrator Worker"
!define VERSION "1.9.0"
!define PAYLOAD_DIR "worker-payload"   ; staged by build_worker_installer.sh

Name "${PRODUCT} ${VERSION} (unsigned)"
OutFile "testorchestrator-worker-${VERSION}-setup-UNSIGNED.exe"
InstallDir "$PROGRAMFILES64\TestOrchestrator\worker"
RequestExecutionLevel admin          ; service registration needs it
ShowInstDetails show
Var /GLOBAL ORCH_URL
Var /GLOBAL AGENT_TOKEN
Var /GLOBAL DATA_DIR

!include "MUI2.nsh"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---- unattended options ---------------------------------------------
; /S silent · /D=C:\path (install dir) · ORCH_URL/AGENT_TOKEN via
; ${GetOptions} — documented in docs/windows-worker-installer.md
!include "FileFunc.nsh"
!include "LogicLib.nsh"

Section "Install worker payload" SecMain
    SetOutPath "$INSTDIR"
    File /r "${PAYLOAD_DIR}\*.*"
    ; Persistent data OUTSIDE the replaceable payload — preserved by
    ; uninstall by default.
    StrCpy $DATA_DIR "$PROGRAMDATA\TestOrchestrator"
    CreateDirectory "$DATA_DIR"
    CreateDirectory "$DATA_DIR\logs"
    CreateDirectory "$DATA_DIR\work"

    ${GetOptions} $CMDLINE "/ORCH_URL=" $ORCH_URL
    ${GetOptions} $CMDLINE "/AGENT_TOKEN=" $AGENT_TOKEN
    ${If} $ORCH_URL == ""
        StrCpy $ORCH_URL "https://localhost"
    ${EndIf}

    ; worker.conf — connectivity config (token NOT persisted here;
    ; registration is exchanged for a stored credential post-install).
    FileOpen $0 "$DATA_DIR\worker.conf" w
    FileWrite $0 "orchestrator_url=$ORCH_URL$\r$\n"
    FileWrite $0 "data_dir=$DATA_DIR$\r$\n"
    FileWrite $0 "install_dir=$INSTDIR$\r$\n"
    FileClose $0

    ; Register execution mechanism: scheduled task at boot running the
    ; bundled agent (no third-party service wrapper dependency).
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" "$INSTDIR\register_agent.py" --url "$ORCH_URL"'
    Pop $0
    ${If} $0 != "0"
        MessageBox MB_ICONEXCLAMATION "Agent registration task failed — see $DATA_DIR\logs\install.log"
    ${EndIf}
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorWorker" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\python\python.exe\" \"$INSTDIR\agent_service.py\" --config \"$DATA_DIR\worker.conf\""'
    Pop $0
    ${If} $0 != "0"
        MessageBox MB_ICONEXCLAMATION "Scheduled-task registration failed — see $DATA_DIR\logs\install.log"
    ${EndIf}
    nsExec::ExecToLog 'schtasks /Run /TN "TestOrchestratorWorker"'

    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorWorker" "DisplayName" "${PRODUCT} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorWorker" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorWorker" "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorWorker" "NoRepair" 1
SectionEnd

Section "Uninstall"
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorWorker"'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorWorker" /F'
    ; User data under $PROGRAMDATA\TestOrchestrator is PRESERVED by
    ; default; deletion requires /PURGEDATA=1 explicitly.
    ${GetOptions} $CMDLINE "/PURGEDATA=" $0
    ${If} $0 == "1"
        MessageBox MB_YESNO "Delete ALL worker data under $PROGRAMDATA\TestOrchestrator?" IDYES +2
        Goto keep_data
        RMDir /r "$PROGRAMDATA\TestOrchestrator"
    ${EndIf}
    keep_data:
    RMDir /r "$INSTDIR"
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorWorker"
SectionEnd
