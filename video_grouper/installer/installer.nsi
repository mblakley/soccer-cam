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
!define TRAY_TASK_NAME "VideoGrouperTrayLaunch"
; Per-machine state path. Service main.py falls back here when
; HKLM\Software\VideoGrouper\StoragePath is unset, and the
; auto-upgrade phase marker lives here so the post-upgrade service
; can read what NSIS reached on its way through.
!define STORAGE_PATH "$APPDATA\${APPNAME}"
!define NSIS_PHASE_FILE "${STORAGE_PATH}\update\nsis-phase.txt"

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

; Detect a prior install and let the user choose upgrade-in-place vs
; clean reinstall. The auto-upgrade flow runs us silently and always
; takes the upgrade path. Interactive runs over an existing install
; (e.g. legacy v0.3.5 user double-clicking VideoGrouperSetup.exe to
; manually upgrade) get a prompt:
;   Yes -> upgrade in place: config + storage preserved
;   No  -> uninstall first, wipe ProgramData, then install fresh
;   Cancel -> abort
;
; Either way the install proceeds against the *new* binaries; the only
; difference is whether prior config/storage survives.
Function .onInit
    SetRegView 64
    SetShellVarContext all
    ReadRegStr $0 HKLM "Software\${APPNAME}" "Install_Dir"
    ${If} $0 == ""
        ; No prior install detected.
        Return
    ${EndIf}
    ${If} ${Silent}
        ; Auto-upgrade (service spawned us with /S). Always upgrade in
        ; place; never prompt, never wipe user data.
        Return
    ${EndIf}
    ; NSIS MessageBox supports at most 2 return-check pairs; the third
    ; choice (Cancel) is handled by falling through to Abort.
    MessageBox MB_YESNOCANCEL|MB_ICONQUESTION \
        "An existing VideoGrouper install was found at $0.$\r$\n$\r$\nYes: Upgrade in place (preserves config + storage)$\r$\nNo:  Clean reinstall (wipes ${STORAGE_PATH})$\r$\nCancel: Abort" \
        /SD IDYES \
        IDYES _proceed IDNO _cleanFirst
    ; Fallthrough = Cancel.
    Abort
    _cleanFirst:
    ; Run the existing uninstaller silently so the service deregisters
    ; itself and Add/Remove Programs entries clear.
    ${If} ${FileExists} "$0\uninstall.exe"
        ExecWait '"$0\uninstall.exe" /S _?=$0'
    ${EndIf}
    ; Drop per-machine state too -- config.ini, queue state, journal,
    ; the lot. The user explicitly chose "clean reinstall".
    RMDir /r "${STORAGE_PATH}"
    _proceed:
FunctionEnd

; Write the current install phase to disk so a failed/interrupted
; install leaves a breadcrumb. The post-upgrade service reads this
; file on startup and appends it to update_history.jsonl as the
; previous attempt's terminal state. See
; ``video_grouper.update.nsis_marker``.
!macro WritePhase Phase
    Push $0
    CreateDirectory "${STORAGE_PATH}\update"
    FileOpen $0 "${NSIS_PHASE_FILE}" w
    FileWrite $0 "${Phase}"
    FileClose $0
    Pop $0
!macroend

Section "Install" SecInstall
    ; Use 64-bit registry view so the 64-bit service can find the keys
    SetRegView 64

    ; Resolve $SMSTARTUP / $DESKTOP / $SMPROGRAMS to the all-users folders
    ; (C:\ProgramData\... and C:\Users\Public\Desktop), not the running
    ; user's profile. Same SHCTX choice makes $APPDATA = C:\ProgramData,
    ; which is the per-machine state path the service falls back to and
    ; where we drop the NSIS phase marker.
    SetShellVarContext all

    !insertmacro WritePhase "started"

    ; Tear down any prior install BEFORE replacing files. The
    ; auto-upgrade flow exits the service cleanly before spawning us,
    ; but the tray is launched via a separate scheduled task and stays
    ; running across the upgrade window. Without an explicit kill,
    ; the new tray.exe lands on disk but the still-running old tray
    ; process holds the singleton lock file -- schtasks /Run below
    ; would fire, the new tray would fail to acquire the lock, and
    ; the user would be left running yesterday's binary against the
    ; new service. Confirmed empirically during Phase 6 v0.3.7 ->
    ; v0.3.8 upgrade testing (LastTaskResult 0x80071420 on the
    ; post-upgrade /Run because of the lock conflict).
    ;
    ; sc.exe stop is best-effort: a clean upgrade has the service
    ; already gone, and a fresh install has nothing to stop. The
    ; 0 exit code means stopped; non-zero is benign here.
    nsExec::ExecToLog 'sc.exe stop VideoGrouperService'
    nsExec::ExecToLog 'taskkill /F /IM VideoGrouperTray.exe'
    ; Brief delay so Windows actually releases handles on the
    ; killed binaries before File /r tries to overwrite them.
    Sleep 1500

    ; Copy the merged onedir output (service + tray + shared _internal/).
    ; The multi-target spec at video_grouper/installer/VideoGrouper.spec
    ; builds both exes against one dependency tree, so service and tray
    ; live side-by-side under $INSTDIR with a single _internal/ folder.
    SetOutPath "$INSTDIR"
    File "..\icon.ico"
    File /r "..\dist\VideoGrouper\*"

    !insertmacro WritePhase "files-copied"

    ; Register the Windows service with delayed-auto-start so it
    ; survives reboots without slowing boot. pywin32's
    ; HandleCommandLine "install" verb FAILS when the service already
    ; exists -- so on upgrade-in-place we skip it (the existing
    ; registration already points binPath at this very exe, which
    ; File /r above just replaced). `sc query` returns 0 when the
    ; service is registered, non-zero (typically 1060) when it isn't.
    nsExec::ExecToLog 'sc.exe query VideoGrouperService'
    Pop $0
    ${If} $0 != "0"
        ; Fresh install: register the service.
        nsExec::ExecToLog '"$INSTDIR\VideoGrouperService.exe" --startup delayed install'
    ${Else}
        ; Upgrade in place: leave registration alone, but force the
        ; start type to delayed-auto-start in case the prior version
        ; was registered with a different setting.
        nsExec::ExecToLog 'sc.exe config VideoGrouperService start= delayed-auto'
    ${EndIf}

    ; Always (re-)apply recovery actions. Idempotent and quick, and
    ; it lets us tighten the retry policy in later releases without
    ; needing a clean reinstall.
    nsExec::ExecToLog 'sc.exe failure VideoGrouperService reset= 86400 actions= restart/5000/restart/30000/restart/60000'

    !insertmacro WritePhase "service-installed"

    ; Register the tray-launch scheduled task. /RU INTERACTIVE makes
    ; the task run as whoever is currently logged on -- the only
    ; reliable way for a LocalSystem service (or NSIS spawned by it
    ; during auto-upgrade) to land the tray in the active desktop
    ; session. Plain subprocess.Popen("VideoGrouperTray.exe") from a
    ; LocalSystem context drops the tray into session 0 where the
    ; user can't see it. See
    ; ``~/.claude/plans/investigate-the-auto-upgrade-process-jiggly-gem.md``
    ; for the precedent (Tailscale, WireGuard, NordVPN, 1Password all
    ; ship this exact pattern).
    ;
    ; /F makes the create idempotent on upgrade; /SC ONLOGON gives
    ; us a logon trigger so the tray comes back automatically after
    ; reboot; /RL LIMITED runs unelevated since the tray doesn't
    ; need admin. We can additionally drive an on-demand launch via
    ; ``schtasks /Run /TN VideoGrouperTrayLaunch`` from anywhere
    ; (installer below, service-side helper post-upgrade).
    ; schtasks /Create /TR "<path with spaces>" silently mangles the
    ; action -- the OS task store ends up with Execute="C:\Program"
    ; and Arguments="Files\...\VideoGrouperTray.exe", so the logon
    ; trigger fires but Windows fails the spawn with 0x80070002
    ; (file not found). Confirmed empirically on Win11 in Phase 6
    ; verification. PowerShell's Register-ScheduledTask has a real
    ; Execute/Arguments split, so we go through it instead.
    FileOpen $0 "$TEMP\vg-register-tray-task.ps1" w
    FileWrite $0 "$$action = New-ScheduledTaskAction -Execute '$INSTDIR\VideoGrouperTray.exe'$\r$\n"
    FileWrite $0 "$$trigger = New-ScheduledTaskTrigger -AtLogOn$\r$\n"
    FileWrite $0 "$$principal = New-ScheduledTaskPrincipal -GroupId 'INTERACTIVE' -RunLevel Limited$\r$\n"
    FileWrite $0 "Register-ScheduledTask -TaskName '${TRAY_TASK_NAME}' -Action $$action -Trigger $$trigger -Principal $$principal -Force | Out-Null$\r$\n"
    FileClose $0
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$TEMP\vg-register-tray-task.ps1"'
    Delete "$TEMP\vg-register-tray-task.ps1"

    ; Desktop shortcut so the user can manually launch the tray /
    ; open the dashboard. We DON'T create $SMSTARTUP -- the
    ; scheduled task's logon trigger replaces it (avoiding a
    ; double-launch race).
    CreateShortCut "$DESKTOP\VideoGrouper.lnk" "$INSTDIR\VideoGrouperTray.exe" "" "$INSTDIR\icon.ico"

    !insertmacro WritePhase "scheduled-task-registered"

    ; Start the service so the FastAPI wizard at :8765 is reachable
    ; before the tray opens the browser. Skip in silent mode so /S
    ; CI runs stay headless. (Silent installs also happen during
    ; auto-upgrade: the service spawns us with /S and exits; we
    ; come up cleanly when the service's StartService below fires.)
    ${IfNot} ${Silent}
        nsExec::ExecToLog 'sc.exe start VideoGrouperService'
    ${Else}
        ; Auto-upgrade re-runs us silently. The previous service
        ; will have exited cleanly before we got here; start the
        ; freshly-installed binary so the dashboard comes back up
        ; without waiting for the recovery actions.
        nsExec::ExecToLog 'sc.exe start VideoGrouperService'
    ${EndIf}

    !insertmacro WritePhase "service-started"

    ; Launch tray in the interactive session. Going through the
    ; scheduled task (rather than NSIS's Exec) is what makes this
    ; work both at first-install (NSIS run by a user) AND at
    ; auto-upgrade (NSIS run by the LocalSystem service, where a
    ; direct Exec would land in session 0).
    ${IfNot} ${Silent}
        nsExec::ExecToLog 'schtasks /Run /TN "${TRAY_TASK_NAME}"'
    ${Else}
        ; Auto-upgrade silent path: bring the tray back too.
        nsExec::ExecToLog 'schtasks /Run /TN "${TRAY_TASK_NAME}"'
    ${EndIf}

    !insertmacro WritePhase "tray-launched"

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Add uninstall information to Add/Remove Programs
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayIcon" "$INSTDIR\icon.ico"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${FULL_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "${COMPANYNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "InstallLocation" "$INSTDIR"

    ; Pin the per-machine StoragePath the service looks up at boot
    ; (video_grouper/service/main.py reads this before falling back
    ; to %ProgramData%\VideoGrouper). The wizard can rewrite this
    ; later if the user picks a different storage path.
    WriteRegStr HKLM "Software\${APPNAME}" "StoragePath" "${STORAGE_PATH}"

    !insertmacro WritePhase "complete"
SectionEnd

Section "Uninstall"
    SetRegView 64
    ; Match the install side: target the all-users shortcut folders,
    ; not the per-user profile of whoever is running the uninstaller.
    SetShellVarContext all
    ; Stop and remove the service silently
    nsExec::ExecToLog '"$INSTDIR\VideoGrouperService.exe" stop'
    nsExec::ExecToLog '"$INSTDIR\VideoGrouperService.exe" remove'

    ; Kill tray if running
    nsExec::ExecToLog 'taskkill /F /IM VideoGrouperTray.exe'

    ; Drop the scheduled task. /F suppresses the "are you sure"
    ; prompt; missing-task error is harmless on a partial install
    ; that never got this far.
    nsExec::ExecToLog 'schtasks /Delete /F /TN "${TRAY_TASK_NAME}"'

    ; Remove the tray lock file from the storage path so a leftover
    ; lock from a forced-kill doesn't block the next install's tray.
    ; StoragePath is set by the installer in HKLM\Software\VideoGrouper.
    ReadRegStr $0 HKLM "Software\${APPNAME}" "StoragePath"
    ${If} $0 != ""
        Delete "$0\tray_agent.lock"
    ${EndIf}

    ; Drop the NSIS phase marker (best-effort, may not exist).
    Delete "${NSIS_PHASE_FILE}"
    RMDir "${STORAGE_PATH}\update"

    ; Recursively remove everything we shipped: the merged spec drops
    ; both exes + a shared _internal/ directly under $INSTDIR.
    RMDir /r "$INSTDIR\_internal"
    Delete "$INSTDIR\VideoGrouperService.exe"
    Delete "$INSTDIR\VideoGrouperTray.exe"
    Delete "$INSTDIR\icon.ico"
    Delete "$INSTDIR\uninstall.exe"

    ; Remove shortcuts (legacy $SMSTARTUP entry from pre-Phase-4
    ; installs, plus the desktop one we still create).
    Delete "$SMSTARTUP\VideoGrouperTray.lnk"
    Delete "$DESKTOP\VideoGrouper.lnk"

    ; Remove install directory
    RMDir "$INSTDIR"

    ; Remove registry keys
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
    DeleteRegKey HKLM "Software\${APPNAME}"
SectionEnd
