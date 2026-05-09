!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; Version information - only define if not passed from command line
!ifndef VERSION
    !define VERSION "0.0.0"
!endif
!ifndef BUILD_NUMBER
    !define BUILD_NUMBER "0"
!endif
!define FULL_VERSION "${VERSION}.${BUILD_NUMBER}"

; Application information
!define APPNAME "VideoGrouper"
!define COMPANYNAME "VideoGrouper"
!define DESCRIPTION "Video Grouper Service and Tray Agent"

; General settings
Name "${APPNAME}"
OutFile "..\dist\VideoGrouperSetup.exe"
InstallDir "$PROGRAMFILES64\${APPNAME}"
InstallDirRegKey HKLM "Software\${APPNAME}" "Install_Dir"
RequestExecutionLevel admin

; Version information for installer properties
VIProductVersion "${VERSION}.${BUILD_NUMBER}"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "CompanyName" "${COMPANYNAME}"
VIAddVersionKey "FileDescription" "${DESCRIPTION}"
VIAddVersionKey "FileVersion" "${FULL_VERSION}"
VIAddVersionKey "ProductVersion" "${FULL_VERSION}"
VIAddVersionKey "LegalCopyright" "Copyright (C) 2026 ${COMPANYNAME}"

; Interface Settings
!define MUI_ABORTWARNING
!define MUI_ICON "..\icon.ico"
!define MUI_UNICON "..\icon.ico"
!define MUI_WELCOMEPAGE_TITLE "Welcome to ${APPNAME} ${FULL_VERSION} Setup"
!define MUI_WELCOMEPAGE_TEXT "This will install ${APPNAME} ${FULL_VERSION} on your computer.$\r$\n$\r$\nAfter installation, a setup wizard will guide you through configuration.$\r$\n$\r$\nClick Next to continue."

; Pages (simplified - configuration is handled by the onboarding wizard)
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Language
!insertmacro MUI_LANGUAGE "English"

Section "Install" SecInstall
    ; Use 64-bit registry view so the 64-bit service can find the keys
    SetRegView 64

    ; Copy the icon to the install root for shortcuts
    SetOutPath "$INSTDIR"
    File "..\icon.ico"

    ; Copy the PyInstaller --onedir output for the service.
    ; Each onedir build is a tree containing the .exe plus an
    ; _internal/ folder with bundled Python + deps. We put service
    ; and tray in separate subdirs so their _internal trees don't
    ; collide.
    SetOutPath "$INSTDIR\service"
    File /r "..\dist\VideoGrouperService\*"

    SetOutPath "$INSTDIR\tray"
    File /r "..\dist\VideoGrouperTray\*"

    SetOutPath "$INSTDIR"

    ; Register the Windows service with delayed-auto-start so it survives
    ; reboots without slowing boot. pywin32's HandleCommandLine accepts
    ; --startup before the verb. Valid values: auto, delayed, manual,
    ; disabled. "delayed" maps to SERVICE_AUTO_START + DelayedAutoStart=1.
    nsExec::ExecToLog '"$INSTDIR\service\VideoGrouperService.exe" --startup delayed install'

    ; Configure service recovery: restart 3 times on failure, then leave
    ; alone. Reset failure count after 24h of clean operation.
    nsExec::ExecToLog 'sc.exe failure VideoGrouperService reset= 86400 actions= restart/5000/restart/30000/restart/60000'

    ; Start the service so the FastAPI wizard at :8765 is reachable
    ; before the tray opens the browser. Skip in silent mode so /S
    ; CI runs stay headless.
    ${IfNot} ${Silent}
        nsExec::ExecToLog 'sc.exe start VideoGrouperService'
    ${EndIf}

    ; Create startup shortcut for tray agent (no config path arg - wizard handles it)
    CreateShortCut "$SMSTARTUP\VideoGrouperTray.lnk" "$INSTDIR\tray\VideoGrouperTray.exe" "" "$INSTDIR\icon.ico"

    ; Also create a desktop shortcut
    CreateShortCut "$DESKTOP\VideoGrouper.lnk" "$INSTDIR\tray\VideoGrouperTray.exe" "" "$INSTDIR\icon.ico"

    ; Launch tray agent (will show onboarding wizard on first run).
    ; Skip in silent mode -- /S installs need to stay headless, otherwise
    ; the wizard blocks automation (e.g. CI and scripted upgrades).
    ${IfNot} ${Silent}
        Exec '"$INSTDIR\tray\VideoGrouperTray.exe"'
    ${EndIf}

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Add uninstall information to Add/Remove Programs
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayIcon" "$INSTDIR\icon.ico"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${FULL_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "${COMPANYNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "InstallLocation" "$INSTDIR"
SectionEnd

Section "Uninstall"
    SetRegView 64
    ; Stop and remove the service silently
    nsExec::ExecToLog '"$INSTDIR\service\VideoGrouperService.exe" stop'
    nsExec::ExecToLog '"$INSTDIR\service\VideoGrouperService.exe" remove'

    ; Kill tray if running
    nsExec::ExecToLog 'taskkill /F /IM VideoGrouperTray.exe'

    ; Remove the tray lock file from the storage path so a leftover
    ; lock from a forced-kill doesn't block the next install's tray.
    ; StoragePath is set by the installer in HKLM\Software\VideoGrouper.
    ReadRegStr $0 HKLM "Software\${APPNAME}" "StoragePath"
    ${If} $0 != ""
        Delete "$0\tray_agent.lock"
    ${EndIf}

    ; Remove the bundled directory trees recursively
    RMDir /r "$INSTDIR\service"
    RMDir /r "$INSTDIR\tray"

    ; Remove root-level files
    Delete "$INSTDIR\icon.ico"
    Delete "$INSTDIR\uninstall.exe"

    ; Remove shortcuts
    Delete "$SMSTARTUP\VideoGrouperTray.lnk"
    Delete "$DESKTOP\VideoGrouper.lnk"

    ; Remove install directory
    RMDir "$INSTDIR"

    ; Remove registry keys
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
    DeleteRegKey HKLM "Software\${APPNAME}"
SectionEnd
