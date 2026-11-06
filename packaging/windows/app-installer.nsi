; TestOrchestrator FULL application installer — Windows amd64
; Complete product: app + embedded Python + Dagu + Temurin JRE + Caddy.
; The KRE execution engine is installed/licensed SEPARATELY — not
; redistributed; the installer registers the user's installation.
; UNSIGNED build — labelled clearly.
!define PRODUCT "TestOrchestrator"
!define VERSION "1.0.0-pre.5"
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
    ; user-configured (application-local jre/python/dagu paths).
    FileOpen $0 "$DATA_DIR\testorchestrator.env" w
    FileWrite $0 "TO_INSTANCE_DIR=$DATA_DIR\instance$\r$\n"
    FileWrite $0 "TO_DATABASE_URI=sqlite:///$DATA_DIR\instance\to.db$\r$\n"
    FileWrite $0 "TO_ALLURE_JAVA_HOME=$INSTDIR\jre$\r$\n"
    FileWrite $0 "TO_PUBLIC_ADDR=$PUBLIC_ADDR$\r$\n"
    FileWrite $0 "TO_PORT=$ORCH_PORT$\r$\n"
    FileClose $0

    ; wire vendored site-packages into the embeddable runtime
    FileOpen $1 "$INSTDIR\python\python312._pth" a
    FileSeek $1 0 END
    FileWrite $1 "site-packages$\r$\n"
    FileClose $1
    ; git is not bundled: default backend is Dulwich (no executable);
    ; TO_GIT_BACKEND=cli needs a separately installed Git.
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" "$INSTDIR\testorchestrator\__main__.py" --bootstrap-check'
    Pop $0

    ; scheduled tasks for web + worker + dagu coordinator
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorWeb" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\python\python.exe\" \"$INSTDIR\wsgi.py\""'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorWorker" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\python\python.exe\" -m flask worker"'
    Pop $0
    nsExec::ExecToLog 'schtasks /Create /TN "TestOrchestratorDagu" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "\"$INSTDIR\dagu\dagu.exe\" start-all --host 127.0.0.1"'
    Pop $0

    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "DisplayName" "${PRODUCT} ${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\TestOrchestrator" "UninstallString" "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorWeb"'
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorWorker"'
    nsExec::ExecToLog 'schtasks /End /TN "TestOrchestratorDagu"'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorWeb" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorWorker" /F'
    nsExec::ExecToLog 'schtasks /Delete /TN "TestOrchestratorDagu" /F'
    RMDir /r "$INSTDIR"
    MessageBox MB_YESNO "Also delete configuration, database, and workspaces in $DATA_DIR?" /SD IDNO IDNO keepData
        RMDir /r "$DATA_DIR"
    keepData:
SectionEnd
