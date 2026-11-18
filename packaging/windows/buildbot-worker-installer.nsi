; TestOrchestrator Buildbot Worker installer — Windows amd64 (KRE host role)
; Build: build_buildbot_worker_installer() stages bb-worker-payload/
; (python-embed + wheels + setup-worker.py + get-pip) then makensis.
;
; The payload installs an offline Python environment and the pinned
; buildbot-worker on the KRE machine — no preinstalled Python and no
; network access required. The installer runs setup-worker.py which
; creates the worker basedir (buildbot.tac) bound to the controller's
; PB endpoint, then registers a boot-time scheduled task running
;   buildbot-worker.exe start <basedir>
; KRE itself is installed/licensed separately — never redistributed.
;
; UNSIGNED BUILD — labelled clearly; do not sign until an approved
; signing identity exists.

!define PRODUCT "TestOrchestrator Buildbot Worker"
!ifndef VERSION
!define VERSION "4.3.0"   ; default only — builds pass -DVERSION=<bb pin>
!endif
!define PAYLOAD_DIR "bb-worker-payload"

Name "${PRODUCT} ${VERSION} (unsigned)"
OutFile "TestOrchestrator-Buildbot-Worker-Setup-${VERSION}-UNSIGNED.exe"
InstallDir "$PROGRAMFILES64\TestOrchestrator\buildbot-worker"
RequestExecutionLevel admin
ShowInstDetails show
Var /GLOBAL MASTER_ADDR
Var /GLOBAL WORKER_NAME
Var /GLOBAL PASSWD_FILE
Var /GLOBAL DATA_DIR

!include "MUI2.nsh"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---- unattended options ---------------------------------------------
; /S silent · /D=C:\path · /MASTER=<host:9989> · /WORKER_NAME=<name> ·
; /PASSWD_FILE=<path> — the password file is read once at install time
; and may be deleted by the operator afterwards; it is never embedded
; in the installer and never placed on a command line.
!include "FileFunc.nsh"
!include "LogicLib.nsh"

Section "Install Buildbot worker payload" SecMain
    SetOutPath "$INSTDIR"
    File /r "${PAYLOAD_DIR}\*.*"

    ReadEnvStr $DATA_DIR "ProgramData"
    StrCpy $DATA_DIR "$DATA_DIR\TestOrchestrator\buildbot-worker"
    CreateDirectory "$DATA_DIR"
    CreateDirectory "$DATA_DIR\worker"

    ${GetOptions} $CMDLINE "/MASTER=" $MASTER_ADDR
    ${GetOptions} $CMDLINE "/WORKER_NAME=" $WORKER_NAME
    ${GetOptions} $CMDLINE "/PASSWD_FILE=" $PASSWD_FILE
    ${If} $MASTER_ADDR == ""
        StrCpy $MASTER_ADDR "<portal-host>:9989"
    ${EndIf}
    ${If} $WORKER_NAME == ""
        ReadEnvStr $WORKER_NAME "COMPUTERNAME"
        StrCpy $WORKER_NAME "bb-$WORKER_NAME"
    ${EndIf}
    ${If} $PASSWD_FILE == ""
        StrCpy $PASSWD_FILE "$DATA_DIR\worker-password.txt"
    ${EndIf}

    ; Offline env + worker creation. setup-worker.py writes
    ; buildbot.tac under $DATA_DIR\worker and worker.conf.json in
    ; $INSTDIR (connectivity only).
    nsExec::ExecToLog '"$INSTDIR\python-embed\python.exe" "$INSTDIR\setup-worker.py" --master "$MASTER_ADDR" --name "$WORKER_NAME" --passwd-file "$PASSWD_FILE" --basedir "$DATA_DIR\worker"'
    Pop $0
    ${If} $0 != "0"
        MessageBox MB_ICONEXCLAMATION "Worker environment setup failed (code $0) — check $INSTDIR and the details log"
        Abort "setup-worker failed"
    ${EndIf}

    ; Boot-time task under a dedicated least-privilege account when the
    ; operator supplies /SERVICE_ACCOUNT=DOMAIN\user, else SYSTEM is a
    ; documented fallback — KRE licensing requirements decide; evidence
    ; required before assuming SYSTEM is needed.
    Var /GLOBAL SVC_ACCOUNT
    ${GetOptions} $CMDLINE "/SERVICE_ACCOUNT=" $SVC_ACCOUNT
    ${If} $SVC_ACCOUNT == ""
        StrCpy $SVC_ACCOUNT "SYSTEM"
    ${EndIf}
    ${If} $SVC_ACCOUNT == "SYSTEM"
        nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorBuildbotWorker" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\python-embed\Scripts\buildbot-worker.exe\" --nodaemon start \"$DATA_DIR\worker\""'
    ${Else}
        nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorBuildbotWorker" /SC ONSTART /RU "$SVC_ACCOUNT" /RP "" /RL HIGHEST /F /TR "\"$INSTDIR\python-embed\Scripts\buildbot-worker.exe\" --nodaemon start \"$DATA_DIR\worker\""'
    ${EndIf}
    Pop $0
    ${If} $0 != "0"
        MessageBox MB_ICONEXCLAMATION "Scheduled-task registration failed"
    ${EndIf}
    ${If} $MASTER_ADDR != "<portal-host>:9989"
        nsExec::ExecToLog 'schtasks /Run /TN "TestOrchestratorBuildbotWorker"'
    ${EndIf}

    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "DisplayName" "${PRODUCT} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "NoRepair" 1
SectionEnd

Section "Uninstall"
    ; Stop + remove the worker task; remove the program payload.
    ; $PROGRAMDATA\TestOrchestrator (basedir, logs, workspaces) is
    ; preserved — deleting it requires the operator's explicit choice.
    ; KRE and unrelated files are never touched.
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorBuildbotWorker"'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorBuildbotWorker" /F'
    RMDir /r "$INSTDIR"
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker"
    MessageBox MB_YESNO "Also delete configuration and workspaces in $DATA_DIR?" /SD IDNO IDNO keepData
        RMDir /r "$DATA_DIR"
    keepData:
SectionEnd
