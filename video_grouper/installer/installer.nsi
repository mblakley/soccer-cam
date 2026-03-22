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
VIAddVersionKey "LegalCopyright" "Copyright (C) 2024 ${COMPANYNAME}"

; Interface Settings
!define MUI_ABORTWARNING
!define MUI_ICON "..\icon.ico"
!define MUI_UNICON "..\icon.ico"
!define MUI_WELCOMEPAGE_TITLE "Welcome to ${APPNAME} ${FULL_VERSION} Setup"
!define MUI_WELCOMEPAGE_TEXT "This will install ${APPNAME} ${FULL_VERSION} on your computer.$\r$\n$\r$\nClick Next to continue."

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"

; Storage page first so we can read existing config
Page custom StorageConfigPage StorageConfigPageLeave
Page custom CameraConfigPage CameraConfigPageLeave

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Language
!insertmacro MUI_LANGUAGE "English"

; Variables for configuration
Var Dialog
Var IPAddress
Var IPAddressCtl
Var Username
Var UsernameCtl
Var Password
Var PasswordCtl
Var StoragePath
Var StoragePathCtl
Var ConfigExists

; Storage Configuration Page (shown first)
Function StorageConfigPage
    !insertmacro MUI_HEADER_TEXT "Storage Configuration" "Select the shared_data directory"

    nsDialogs::Create 1018
    Pop $Dialog

    ${NSD_CreateLabel} 0 0 100% 24u "Select the shared_data directory for config, logs, and video data.$\r$\nIf a config.ini exists there, camera settings will be pre-filled."
    ${NSD_CreateText} 0 30u 80% 12u $StoragePath
    Pop $StoragePathCtl

    ${NSD_CreateButton} 81% 30u 19% 12u "Browse..."
    Pop $0
    ${NSD_OnClick} $0 StoragePathBrowse

    nsDialogs::Show
FunctionEnd

Function StoragePathBrowse
    nsDialogs::SelectFolderDialog "Select shared_data Directory" ""
    Pop $0
    ${NSD_SetText} $StoragePathCtl $0
FunctionEnd

Function StorageConfigPageLeave
    ${NSD_GetText} $StoragePathCtl $StoragePath

    ; Try to read existing config.ini
    StrCpy $ConfigExists "0"
    IfFileExists "$StoragePath\config.ini" 0 no_existing_config
        StrCpy $ConfigExists "1"
        ; Read camera settings from existing config
        ; Try [CAMERA.reolink] first, then [CAMERA.dahua], then [CAMERA]
        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA.reolink" "device_ip"
        StrCmp $0 "" 0 got_ip
        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA.dahua" "device_ip"
        StrCmp $0 "" 0 got_ip
        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA" "device_ip"
        got_ip:
        StrCmp $0 "" +2
            StrCpy $IPAddress $0

        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA.reolink" "username"
        StrCmp $0 "" 0 got_user
        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA.dahua" "username"
        StrCmp $0 "" 0 got_user
        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA" "username"
        got_user:
        StrCmp $0 "" +2
            StrCpy $Username $0

        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA.reolink" "password"
        StrCmp $0 "" 0 got_pass
        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA.dahua" "password"
        StrCmp $0 "" 0 got_pass
        ReadINIStr $0 "$StoragePath\config.ini" "CAMERA" "password"
        got_pass:
        StrCmp $0 "" +2
            StrCpy $Password $0
    no_existing_config:
FunctionEnd

; Camera Configuration Page (shown second, pre-populated if config exists)
Function CameraConfigPage
    !insertmacro MUI_HEADER_TEXT "Camera Configuration" "Enter your camera settings"

    nsDialogs::Create 1018
    Pop $Dialog

    StrCmp $ConfigExists "1" 0 +3
        ${NSD_CreateLabel} 0 0 100% 12u "Settings loaded from existing config.ini. Modify if needed:"
        Goto label_done
        ${NSD_CreateLabel} 0 0 100% 12u "Enter your camera connection settings:"
    label_done:

    ${NSD_CreateLabel} 0 16u 100% 12u "IP Address:"
    ${NSD_CreateText} 0 29u 100% 12u $IPAddress
    Pop $IPAddressCtl

    ${NSD_CreateLabel} 0 46u 100% 12u "Username:"
    ${NSD_CreateText} 0 59u 100% 12u $Username
    Pop $UsernameCtl

    ${NSD_CreateLabel} 0 76u 100% 12u "Password:"
    ${NSD_CreateText} 0 89u 100% 12u $Password
    Pop $PasswordCtl

    nsDialogs::Show
FunctionEnd

Function CameraConfigPageLeave
    ${NSD_GetText} $IPAddressCtl $IPAddress
    ${NSD_GetText} $UsernameCtl $Username
    ${NSD_GetText} $PasswordCtl $Password
FunctionEnd

Section "Install" SecInstall
    ; Use 64-bit registry view so the 64-bit service can find the keys
    SetRegView 64
    SetOutPath "$INSTDIR"

    ; Copy files
    File "..\dist\VideoGrouperService.exe"
    File "..\dist\VideoGrouperTray.exe"
    File "..\icon.ico"

    ; Create config.ini in storage path if it doesn't exist
    IfFileExists "$StoragePath\config.ini" skip_config
        CreateDirectory "$StoragePath"
        CreateDirectory "$StoragePath\logs"
        FileOpen $0 "$StoragePath\config.ini" w
        FileWrite $0 "[CAMERA]$\r$\n"
        FileWrite $0 "type = dahua$\r$\n"
        FileWrite $0 "device_ip = $IPAddress$\r$\n"
        FileWrite $0 "username = $Username$\r$\n"
        FileWrite $0 "password = $Password$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[STORAGE]$\r$\n"
        FileWrite $0 "path = $StoragePath$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[RECORDING]$\r$\n"
        FileWrite $0 "min_duration = 60$\r$\n"
        FileWrite $0 "max_duration = 3600$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[PROCESSING]$\r$\n"
        FileWrite $0 "max_concurrent_downloads = 1$\r$\n"
        FileWrite $0 "max_concurrent_conversions = 1$\r$\n"
        FileWrite $0 "retry_attempts = 3$\r$\n"
        FileWrite $0 "retry_delay = 60$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[LOGGING]$\r$\n"
        FileWrite $0 "level = INFO$\r$\n"
        FileWrite $0 "log_file = $StoragePath\logs\video_grouper.log$\r$\n"
        FileWrite $0 "max_log_size = 10485760$\r$\n"
        FileWrite $0 "backup_count = 5$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[APP]$\r$\n"
        FileWrite $0 "check_interval_seconds = 60$\r$\n"
        FileWrite $0 "timezone = America/New_York$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[NTFY]$\r$\n"
        FileWrite $0 "enabled = false$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[YOUTUBE]$\r$\n"
        FileWrite $0 "enabled = false$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[TEAMSNAP]$\r$\n"
        FileWrite $0 "enabled = false$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[PLAYMETRICS]$\r$\n"
        FileWrite $0 "enabled = false$\r$\n"
        FileWrite $0 "$\r$\n"
        FileWrite $0 "[AUTOCAM]$\r$\n"
        FileWrite $0 "enabled = false$\r$\n"
        FileClose $0
    skip_config:

    ; Save storage path to registry (service reads this to find config)
    WriteRegStr HKLM "Software\${APPNAME}" "StoragePath" "$StoragePath"

    ; Install and start the Windows service
    ExecWait '"$INSTDIR\VideoGrouperService.exe" install'
    ExecWait '"$INSTDIR\VideoGrouperService.exe" start'

    ; Create startup shortcut for tray agent with config path argument
    CreateShortCut "$SMSTARTUP\VideoGrouperTray.lnk" "$INSTDIR\VideoGrouperTray.exe" '"$StoragePath\config.ini"' "$INSTDIR\icon.ico"

    ; Also create a desktop shortcut
    CreateShortCut "$DESKTOP\VideoGrouper.lnk" "$INSTDIR\VideoGrouperTray.exe" '"$StoragePath\config.ini"' "$INSTDIR\icon.ico"

    ; Launch tray agent now
    Exec '"$INSTDIR\VideoGrouperTray.exe" "$StoragePath\config.ini"'

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
    ; Stop and remove the service
    ExecWait '"$INSTDIR\VideoGrouperService.exe" stop'
    ExecWait '"$INSTDIR\VideoGrouperService.exe" remove'

    ; Kill tray if running
    ExecWait 'taskkill /F /IM VideoGrouperTray.exe'

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
