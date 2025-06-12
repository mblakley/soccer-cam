# Build script for VideoGrouper installer

# Configuration
$VERSION = "0.1.0"
$BUILD_NUMBER = "0"
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$ICON_PATH = Join-Path $SCRIPT_DIR "video_grouper\icon.ico"
$SERVICE_SCRIPT = Join-Path $SCRIPT_DIR "video_grouper\service_wrapper.py"
$TRAY_SCRIPT = Join-Path $SCRIPT_DIR "video_grouper\tray_agent.py"
$INSTALLER_SCRIPT = Join-Path $SCRIPT_DIR "video_grouper\installer.nsi"
$DIST_DIR = Join-Path $SCRIPT_DIR "video_grouper\dist"
$BUILD_DIR = Join-Path $SCRIPT_DIR "video_grouper\build"

# Ensure we're in the correct directory
Set-Location $SCRIPT_DIR

# Create dist and build directories if they don't exist
if (-not (Test-Path $DIST_DIR)) {
    New-Item -ItemType Directory -Path $DIST_DIR
}
if (-not (Test-Path $BUILD_DIR)) {
    New-Item -ItemType Directory -Path $BUILD_DIR
}

# Install PyInstaller if not already installed
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Host "Installing PyInstaller..."
    pip install pyinstaller
}

# Check for NSIS installation
$NSIS_PATH = "C:\Program Files (x86)\NSIS\makensis.exe"
if (-not (Test-Path $NSIS_PATH)) {
    Write-Host "NSIS not found at $NSIS_PATH"
    Write-Host "Please install NSIS from https://nsis.sourceforge.io/Download"
    exit 1
}

# Build service executable
Write-Host "Building service executable..."
$iconArg = if (Test-Path $ICON_PATH) { "--icon=$ICON_PATH" } else { "" }
python -m PyInstaller --noconfirm --onefile --windowed $iconArg --name=VideoGrouperService --distpath=$DIST_DIR --workpath=$BUILD_DIR $SERVICE_SCRIPT

# Build tray agent executable
Write-Host "Building tray agent executable..."
python -m PyInstaller --noconfirm --onefile --windowed $iconArg --name=tray_agent --distpath=$DIST_DIR --workpath=$BUILD_DIR $TRAY_SCRIPT

# Build installer
Write-Host "Building installer..."
& $NSIS_PATH "/DVERSION=$VERSION" "/DBUILD_NUMBER=$BUILD_NUMBER" $INSTALLER_SCRIPT

Write-Host "Build complete! Check the video_grouper/dist directory for the installer." 