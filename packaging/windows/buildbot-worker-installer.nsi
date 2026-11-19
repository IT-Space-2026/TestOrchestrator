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

!define MUI_ICON "it.ico"
!define MUI_UNICON "it.ico"
Name "${PRODUCT} ${VERSION} (unsigned)"
OutFile "TestOrchestrator-Buildbot-Worker-Setup-${VERSION}-UNSIGNED.exe"
InstallDir "$PROGRAMFILES64\TestOrchestrator\buildbot-worker"
RequestExecutionLevel admin
ShowInstDetails show
Var /GLOBAL MASTER_ADDR
Var /GLOBAL WORKER_NAME
Var /GLOBAL PASSWD_FILE
Var /GLOBAL DATA_DIR
Var /GLOBAL SVC_ACCOUNT
Var /GLOBAL Dialog
Var /GLOBAL CTL_MASTER
Var /GLOBAL CTL_NAME
Var /GLOBAL CTL_PASS

!include "MUI2.nsh"
!insertmacro MUI_PAGE_DIRECTORY
Page custom ConfigPage ConfigPageLeave
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
!include "nsDialogs.nsh"

Function .onInit
    ReadEnvStr $DATA_DIR "ProgramData"
    StrCpy $DATA_DIR "$DATA_DIR\TestOrchestrator\buildbot-worker"

    ${GetOptions} $CMDLINE "/MASTER=" $MASTER_ADDR
    ${GetOptions} $CMDLINE "/WORKER_NAME=" $WORKER_NAME
    ${GetOptions} $CMDLINE "/PASSWD_FILE=" $PASSWD_FILE
    ${GetOptions} $CMDLINE "/SERVICE_ACCOUNT=" $SVC_ACCOUNT
    ${If} $WORKER_NAME == ""
        ReadEnvStr $WORKER_NAME "COMPUTERNAME"
        StrCpy $WORKER_NAME "bb-$WORKER_NAME"
    ${EndIf}
FunctionEnd

; ---- interactive configuration page ----------------------------------
; Skipped entirely in silent mode — /MASTER, /WORKER_NAME and
; /PASSWD_FILE cover unattended installs. Interactive runs collect the
; endpoint, worker name and the one-time password the portal issued
; (System → Infrastructure → Add execution target) and write it to
; $DATA_DIR\worker-password.txt — never onto a command line.
Function ConfigPage
    ${If} ${Silent}
        Abort
    ${EndIf}
    nsDialogs::Create 1018
    Pop $Dialog
    ${If} $Dialog == error
        Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 24u "Controller Buildbot PB endpoint — the hostname or IP of the machine where TestOrchestrator is installed, port 9989 (e.g. testorchestrator.local:9989). This is NOT the https portal URL:"
    Pop $0
    ${NSD_CreateText} 0 26u 100% 12u "$MASTER_ADDR"
    Pop $CTL_MASTER

    ${NSD_CreateLabel} 0 44u 100% 24u "Worker name — shown when the target was added (System → Infrastructure), or issue a new one with 'Issue password':"
    Pop $0
    ${NSD_CreateText} 0 70u 100% 12u "$WORKER_NAME"
    Pop $CTL_NAME

    ${NSD_CreateLabel} 0 88u 100% 24u "One-time worker password — flashed once in the portal when the target was created; 'Issue password' rotates it:"
    Pop $0
    ${NSD_CreatePassword} 0 114u 100% 12u ""
    Pop $CTL_PASS

    nsDialogs::Show
FunctionEnd

Function ConfigPageLeave
    ${NSD_GetText} $CTL_MASTER $MASTER_ADDR
    ${NSD_GetText} $CTL_NAME $WORKER_NAME
    ${NSD_GetText} $CTL_PASS $0
    ${If} $MASTER_ADDR == ""
    ${OrIf} $WORKER_NAME == ""
        MessageBox MB_ICONEXCLAMATION "Controller address and worker name are required."
        Abort
    ${EndIf}
    ; An explicit /PASSWD_FILE wins — the operator manages that file.
    ; Otherwise persist the entered password to the install-owned file
    ; setup-worker.py reads.
    ${If} $PASSWD_FILE == ""
        ${If} $0 == ""
            MessageBox MB_ICONEXCLAMATION "Enter the one-time worker password issued by the portal (System → Infrastructure → Add execution target)."
            Abort
        ${EndIf}
        CreateDirectory "$DATA_DIR"
        StrCpy $PASSWD_FILE "$DATA_DIR\worker-password.txt"
        FileOpen $1 "$PASSWD_FILE" w
        FileWrite $1 "$0$\r$\n"
        FileClose $1
    ${EndIf}
FunctionEnd

Section "Install Buildbot worker payload" SecMain
    ; stop anything holding file locks on the payload BEFORE
    ; overwriting — a running worker task or tray pythonw keeps
    ; python-embed DLLs open ('_bz2.pyd — error opening it')
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorBuildbotWorker"'
    ${If} ${FileExists} "$INSTDIR\stop-tray.cmd"
        nsExec::ExecToLog '"$INSTDIR\stop-tray.cmd"'
    ${EndIf}
    ; brief beat for handles to release
    Sleep 1500

    SetOutPath "$INSTDIR"
    File /r "${PAYLOAD_DIR}\*.*"
    File "it.ico"

    CreateDirectory "$DATA_DIR"
    CreateDirectory "$DATA_DIR\worker"

    ${If} $MASTER_ADDR == ""
        StrCpy $MASTER_ADDR "<portal-host>:9989"
    ${EndIf}
    ${If} $PASSWD_FILE == ""
        StrCpy $PASSWD_FILE "$DATA_DIR\worker-password.txt"
    ${EndIf}
    ${IfNot} ${FileExists} "$PASSWD_FILE"
        MessageBox MB_ICONEXCLAMATION "Worker password file not found:$\r$\n  $PASSWD_FILE$\r$\n$\r$\nCreate it containing the one-time worker password issued by the portal, or pass /PASSWD_FILE=<path>."
        Abort "worker password file missing"
    ${EndIf}

    ; Boot-time task runs under a dedicated least-privilege account when
    ; the operator supplies /SERVICE_ACCOUNT=DOMAIN\user, else SYSTEM is
    ; a documented fallback — KRE licensing requirements decide.
    ${If} $SVC_ACCOUNT == ""
        StrCpy $SVC_ACCOUNT "SYSTEM"
    ${EndIf}

    ; Offline env + worker creation + task registration — all inside
    ; setup-worker.py (subprocess arg lists; the NSIS->cmd->schtasks
    ; chain mangled embedded /TR quotes). Output tee'd to a persistent
    ; log; buildbot.tac lands under $DATA_DIR\worker.
    nsExec::ExecToLog 'cmd /c ""$INSTDIR\python-embed\python.exe" "$INSTDIR\setup-worker.py" --master "$MASTER_ADDR" --name "$WORKER_NAME" --passwd-file "$PASSWD_FILE" --basedir "$DATA_DIR\worker" --service-account "$SVC_ACCOUNT" > "$DATA_DIR\setup-worker.log" 2>&1"'
    Pop $0
    ${If} $0 != "0"
        MessageBox MB_ICONEXCLAMATION "Worker setup/registration failed (code $0) — see $DATA_DIR\setup-worker.log"
        Abort "setup-worker failed"
    ${EndIf}
    ${IfNot} ${FileExists} "$INSTDIR\python-embed\Scripts\buildbot-worker.exe"
        MessageBox MB_ICONEXCLAMATION "buildbot-worker.exe missing after install — see $DATA_DIR\setup-worker.log"
        Abort "worker exe missing"
    ${EndIf}

    ; tray icon for the installing user — autostart at logon, a Start
    ; Menu shortcut to relaunch it after stop/Quit, and launch it now
    ; so the icon appears without a re-login
    SetShellVarContext current
    CreateShortCut "$SMSTARTUP\TestOrchestrator Buildbot Worker.lnk" '"$INSTDIR\python-embed\pythonw.exe"' '"$INSTDIR\worker-tray.py"' "$INSTDIR\it.ico" 0
    CreateShortCut "$SMPROGRAMS\TestOrchestrator Buildbot Worker.lnk" '"$INSTDIR\python-embed\pythonw.exe"' '"$INSTDIR\worker-tray.py"' "$INSTDIR\it.ico" 0
    Exec '"$INSTDIR\python-embed\pythonw.exe" "$INSTDIR\worker-tray.py"'

    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "DisplayName" "${PRODUCT} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "DisplayIcon" "$INSTDIR\it.ico"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker" "NoRepair" 1
SectionEnd

Section "Uninstall"
    ; Stop + remove the worker task + tray autostart; remove the
    ; program payload. $PROGRAMDATA\TestOrchestrator (basedir, logs,
    ; workspaces) is preserved — deleting it requires the operator's
    ; explicit choice. KRE and unrelated files are never touched.
    SetShellVarContext current
    Delete "$SMSTARTUP\TestOrchestrator Buildbot Worker.lnk"
    Delete "$SMPROGRAMS\TestOrchestrator Buildbot Worker.lnk"
    ; kill only OUR tray process (worker-tray.py) — a blanket
    ; taskkill on pythonw.exe could reap unrelated processes
    ${If} ${FileExists} "$INSTDIR\stop-tray.cmd"
        nsExec::ExecToLog '"$INSTDIR\stop-tray.cmd"'
    ${EndIf}
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorBuildbotWorker"'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorBuildbotWorker" /F'
    RMDir /r "$INSTDIR"
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestratorBuildbotWorker"
    MessageBox MB_YESNO "Also delete configuration and workspaces in $DATA_DIR?" /SD IDNO IDNO keepData
        RMDir /r "$DATA_DIR"
    keepData:
SectionEnd
