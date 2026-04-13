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
    SetOutPath "$INSTDIR"

    ; Copy files
    File "..\dist\VideoGrouperService.exe"
    File "..\dist\VideoGrouperTray.exe"
    File "..\icon.ico"

    ; Install the Windows service silently (do not start -- onboarding wizard must run first)
    nsExec::ExecToLog '"$INSTDIR\VideoGrouperService.exe" install'

    ; Create startup shortcut for tray agent (no config path arg - wizard handles it)
    CreateShortCut "$SMSTARTUP\VideoGrouperTray.lnk" "$INSTDIR\VideoGrouperTray.exe" "" "$INSTDIR\icon.ico"

    ; Also create a desktop shortcut
    CreateShortCut "$DESKTOP\VideoGrouper.lnk" "$INSTDIR\VideoGrouperTray.exe" "" "$INSTDIR\icon.ico"

    ; Launch tray agent (will show onboarding wizard on first run).
    ; Skip in silent mode -- /S installs need to stay headless, otherwise
    ; the wizard blocks automation (e.g. CI and scripted upgrades).
    ${IfNot} ${Silent}
        Exec '"$INSTDIR\VideoGrouperTray.exe"'
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
    nsExec::ExecToLog '"$INSTDIR\VideoGrouperService.exe" stop'
    nsExec::ExecToLog '"$INSTDIR\VideoGrouperService.exe" remove'

    ; Kill tray if running
    nsExec::ExecToLog 'taskkill /F /IM VideoGrouperTray.exe'

    ; Remove files
    Delete "$INSTDIR\VideoGrouperService.exe"
    Delete "$INSTDIR\VideoGrouperTray.exe"
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
