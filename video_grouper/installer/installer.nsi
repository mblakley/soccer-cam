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
InstallDir "$PROGRAMFILES\${APPNAME}"
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

; Custom pages for configuration
Page custom CameraConfigPage CameraConfigPageLeave
Page custom StorageConfigPage StorageConfigPageLeave

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
Var Username
Var Password
Var StoragePath
Var TempPath

; Camera Configuration Page
Function CameraConfigPage
    !insertmacro MUI_HEADER_TEXT "Camera Configuration" "Enter your camera settings"
    
    nsDialogs::Create 1018
    Pop $Dialog
    
    ${NSD_CreateLabel} 0 0 100% 12u "IP Address:"
    ${NSD_CreateText} 0 13u 100% 12u $IPAddress
    Pop $IPAddress
    
    ${NSD_CreateLabel} 0 30u 100% 12u "Username:"
    ${NSD_CreateText} 0 43u 100% 12u $Username
    Pop $Username
    
    ${NSD_CreateLabel} 0 60u 100% 12u "Password:"
    ${NSD_CreatePassword} 0 73u 100% 12u $Password
    Pop $Password
    
    nsDialogs::Show
FunctionEnd

Function CameraConfigPageLeave
    ${NSD_GetText} $IPAddress $IPAddress
    ${NSD_GetText} $Username $Username
    ${NSD_GetText} $Password $Password
FunctionEnd

; Storage Configuration Page
Function StorageConfigPage
    !insertmacro MUI_HEADER_TEXT "Storage Configuration" "Select storage locations"
    
    nsDialogs::Create 1018
    Pop $Dialog
    
    ${NSD_CreateLabel} 0 0 100% 12u "Storage Base Path:"
    ${NSD_CreateText} 0 13u 80% 12u $StoragePath
    Pop $StoragePath
    
    ${NSD_CreateButton} 81% 13u 19% 12u "Browse..."
    Pop $0
    ${NSD_OnClick} $0 StoragePathBrowse
    
    nsDialogs::Show
FunctionEnd

Function StoragePathBrowse
    nsDialogs::SelectFolderDialog "Select Storage Directory" ""
    Pop $0
    ${NSD_SetText} $StoragePath $0
FunctionEnd

Function StorageConfigPageLeave
    ${NSD_GetText} $StoragePath $StoragePath
    StrCpy $TempPath "$StoragePath\temp"
FunctionEnd

; Variables
Var PythonVersion

Section "Install Python" SecPython
    ; Check if Python is installed
    ReadRegStr $PythonVersion HKLM "SOFTWARE\Python\PythonCore\3.9\InstallPath" ""
    StrCmp $PythonVersion "" 0 python_installed
    
    ; Download Python installer
    NSISdl::download "https://www.python.org/ftp/python/3.9.13/python-3.9.13-amd64.exe" "$TEMP\python-3.9.13-amd64.exe"
    Pop $R0
    StrCmp $R0 "success" +3
        MessageBox MB_OK "Failed to download Python installer. Please install Python 3.9 manually."
        Abort
    
    ; Install Python
    ExecWait '"$TEMP\python-3.9.13-amd64.exe" /quiet InstallAllUsers=1 PrependPath=1'
    Delete "$TEMP\python-3.9.13-amd64.exe"
    
    python_installed:
    SetOutPath "$INSTDIR"
SectionEnd

Section "Install Service" SecService
    SetOutPath "$INSTDIR"
    
    ; Create necessary directories
    CreateDirectory "$INSTDIR\logs"
    CreateDirectory "$INSTDIR\config"
    CreateDirectory "$StoragePath"
    CreateDirectory "$TempPath"
    
    ; Create config.ini with user input
    FileOpen $0 "$INSTDIR\config.ini" w
    FileWrite $0 "[CAMERA]$\r$\n"
    FileWrite $0 "ip_address = $IPAddress$\r$\n"
    FileWrite $0 "username = $Username$\r$\n"
    FileWrite $0 "password = $Password$\r$\n"
    FileWrite $0 "$\r$\n"
    FileWrite $0 "[STORAGE]$\r$\n"
    FileWrite $0 "base_path = $StoragePath$\r$\n"
    FileWrite $0 "temp_path = $TempPath$\r$\n"
    FileWrite $0 "$\r$\n"
    FileWrite $0 "[PROCESSING]$\r$\n"
    FileWrite $0 "max_concurrent_downloads = 2$\r$\n"
    FileWrite $0 "max_concurrent_conversions = 1$\r$\n"
    FileWrite $0 "retry_attempts = 3$\r$\n"
    FileWrite $0 "retry_delay = 60$\r$\n"
    FileWrite $0 "$\r$\n"
    FileWrite $0 "[LOGGING]$\r$\n"
    FileWrite $0 "level = INFO$\r$\n"
    FileWrite $0 "log_file = logs\video_grouper.log$\r$\n"
    FileWrite $0 "max_log_size = 10485760$\r$\n"
    FileWrite $0 "backup_count = 5$\r$\n"
    FileClose $0
    
    ; Copy files
    File "..\dist\service\VideoGrouperService.exe"
    File "..\dist\tray\VideoGrouperTray.exe"
    File "..\icon.ico"
    File "..\match_info.ini.dist"
    File "..\requirements.txt"
    
    ; Install Python dependencies
    ExecWait 'pip install -r "$INSTDIR\requirements.txt"'
    
    ; Install and start the service
    ExecWait '"$INSTDIR\VideoGrouperService.exe" install'
    ExecWait '"$INSTDIR\VideoGrouperService.exe" start'
    
    ; Create startup shortcut for tray agent
    CreateShortCut "$SMSTARTUP\VideoGrouperTray.lnk" "$INSTDIR\VideoGrouperTray.exe" "" "$INSTDIR\icon.ico"
    
    ; Create uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"
    
    ; Add uninstall information to Add/Remove Programs
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayIcon" "$INSTDIR\icon.ico"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${FULL_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Version" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "VersionMajor" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "VersionMinor" "0"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "${COMPANYNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "InstallLocation" "$INSTDIR"
SectionEnd

Section "Uninstall"
    ; Stop and remove the service
    ExecWait '"$INSTDIR\VideoGrouperService.exe" stop'
    ExecWait '"$INSTDIR\VideoGrouperService.exe" remove'
    
    ; Remove files and directories
    Delete "$INSTDIR\VideoGrouperService.exe"
    Delete "$INSTDIR\VideoGrouperTray.exe"
    Delete "$INSTDIR\icon.ico"
    Delete "$INSTDIR\match_info.ini.dist"
    Delete "$INSTDIR\requirements.txt"
    Delete "$INSTDIR\config.ini"
    Delete "$INSTDIR\uninstall.exe"
    
    ; Remove startup shortcut
    Delete "$SMSTARTUP\VideoGrouperTray.lnk"
    
    ; Remove directories
    RMDir /r "$INSTDIR\logs"
    RMDir /r "$INSTDIR\config"
    RMDir "$INSTDIR"
    
    ; Remove registry keys
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
    DeleteRegKey HKLM "Software\${APPNAME}"
SectionEnd 