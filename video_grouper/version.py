"""
Version information for VideoGrouper.
This file is automatically updated during the build process.
"""

VERSION = "0.0.0"  # Will be replaced with Git tag version during build
BUILD_NUMBER = (
    "0"  # Will be replaced with 0 for releases, commit count for development builds
)

# Full version string including build number
FULL_VERSION = f"{VERSION}+{BUILD_NUMBER}"

# Version info for Windows executables
VERSION_INFO = {
    "version": VERSION,
    "build_number": BUILD_NUMBER,
    "company_name": "VideoGrouper",
    "file_description": "VideoGrouper Service and Tray Agent",
    "internal_name": "VideoGrouper",
    "legal_copyright": "Copyright (c) 2024",
    "original_filename": "VideoGrouperService.exe",
    "product_name": "VideoGrouper",
}


def get_version():
    """Get the current version string."""
    return VERSION


def get_full_version():
    """Get the full version string including build number."""
    return FULL_VERSION


def get_version_info():
    """Get the version info dictionary for Windows executables."""
    return VERSION_INFO
