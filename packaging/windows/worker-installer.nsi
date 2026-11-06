; TestOrchestrator Dagu Worker installer — Windows amd64 (KRE host role)
; Build: build_worker_installer.sh stages worker-payload/ then makensis.
;
; The worker payload is the pinned, checksum-verified Dagu 2.25.8
; Windows binary — self-contained, no Python or other runtime required
; on the KRE machine. This installer lays it down, writes a worker
; config, and registers a boot-time scheduled task running
;   dagu.exe worker --worker.coordinators=<portal>:50055
;                 --worker.labels kre=<label> --worker.id <name>
; KRE itself is installed/licensed separately — never redistributed.
;
; UNSIGNED BUILD — labelled clearly; do not sign until an approved
; signing identity exists.

!define PRODUCT "TestOrchestrator Dagu Worker"
!define VERSION "2.25.8"
!define PAYLOAD_DIR "worker-payload"

Name "${PRODUCT} ${VERSION} (unsigned)"
OutFile "testorchestrator-dagu-worker-${VERSION}-setup-UNSIGNED.exe"
InstallDir "$PROGRAMFILES64\TestOrchestrator\dagu-worker"
RequestExecutionLevel admin
ShowInstDetails show
Var /GLOBAL COORD_ADDR
Var /GLOBAL WORKER_LABEL
Var /GLOBAL WORKER_ID
Var /GLOBAL DATA_DIR

!include "MUI2.nsh"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---- unattended options ---------------------------------------------
; /S silent · /D=C:\path · /COORD=<host:port> · /LABEL=kre-<tag> ·
; /WORKER_ID=<name>.  No credentials on the command line — worker
; peer-TLS/auth material is provisioned separately per deployment.
!include "FileFunc.nsh"
!include "LogicLib.nsh"

Section "Install Dagu worker payload" SecMain
    SetOutPath "$INSTDIR"
    File /r "${PAYLOAD_DIR}\*.*"

    ReadEnvStr $DATA_DIR "ProgramData"
    StrCpy $DATA_DIR "$DATA_DIR\TestOrchestrator\dagu-worker"
    CreateDirectory "$DATA_DIR"
    CreateDirectory "$DATA_DIR\home"

    ${GetOptions} $CMDLINE "/COORD=" $COORD_ADDR
    ${GetOptions} $CMDLINE "/LABEL=" $WORKER_LABEL
    ${GetOptions} $CMDLINE "/WORKER_ID=" $WORKER_ID
    ${If} $COORD_ADDR == ""
        StrCpy $COORD_ADDR "<portal-host>:50055"
    ${EndIf}
    ${If} $WORKER_LABEL == ""
        StrCpy $WORKER_LABEL "unset"
    ${EndIf}
    ${If} $WORKER_ID == ""
        ReadEnvStr $WORKER_ID "COMPUTERNAME"
        StrCpy $WORKER_ID "worker-$WORKER_ID"
    ${EndIf}

    ; worker.conf — connectivity config; credentials are NOT stored
    ; here (peer TLS / auth provisioning is a separate, documented step).
    FileOpen $0 "$DATA_DIR\worker.conf" w
    FileWrite $0 "coordinators=$COORD_ADDR$\r$\n"
    FileWrite $0 "labels=kre=$WORKER_LABEL$\r$\n"
    FileWrite $0 "worker_id=$WORKER_ID$\r$\n"
    FileWrite $0 "dagu_home=$DATA_DIR\home$\r$\n"
    FileClose $0

    ; Boot-time task: least-privilege is the design target — SYSTEM is
    ; used only if KRE licensing/execution proves to require it, and
    ; that requirement must be evidenced, not assumed.
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorDaguWorker" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\dagu.exe\" worker --worker.coordinators=$COORD_ADDR --worker.labels kre=$WORKER_LABEL --worker.id $WORKER_ID"'
    Pop $0
    ${If} $0 != "0"
        MessageBox MB_ICONEXCLAMATION "Scheduled-task registration failed"
    ${EndIf}
    ${If} $WORKER_LABEL != "unset"
        nsExec::ExecToLog 'schtasks /Run /TN "TestOrchestratorDaguWorker"'
    ${EndIf}

    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorDaguWorker" "DisplayName" "${PRODUCT} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorDaguWorker" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorDaguWorker" "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorDaguWorker" "NoRepair" 1
SectionEnd

Section "Uninstall"
    ; Stop + remove the worker task; remove the program payload.
    ; $PROGRAMDATA\TestOrchestrator (config, logs, workspaces) is
    ; preserved — deleting it requires the operator's explicit choice.
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorDaguWorker"'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorDaguWorker" /F'
    RMDir /r "$INSTDIR"
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorDaguWorker"
    MessageBox MB_YESNO "Also delete configuration and workspaces in $DATA_DIR?" /SD IDNO IDNO keepData
        RMDir /r "$DATA_DIR"
    keepData:
SectionEnd
