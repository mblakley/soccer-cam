"""Map network share using Windows WNet API (works in any session context)."""
import ctypes
from ctypes import wintypes
import os

mpr = ctypes.WinDLL("mpr")

RESOURCETYPE_DISK = 1

class NETRESOURCE(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD),
        ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR),
        ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR),
        ("lpProvider", wintypes.LPWSTR),
    ]

def map_share(remote: str, user: str, password: str, drive_letter: str = None):
    """Map a UNC share. Works from WMI, services, and PSRemoting."""
    nr = NETRESOURCE()
    nr.dwType = RESOURCETYPE_DISK
    nr.lpRemoteName = remote
    nr.lpLocalName = drive_letter  # None = no drive letter, just UNC access

    rc = mpr.WNetAddConnection2W(
        ctypes.byref(nr),
        password,
        user,
        0,  # no flags
    )
    if rc == 0:
        return True
    elif rc == 1219:  # already connected
        return True
    else:
        print(f"WNetAddConnection2 failed: rc={rc}")
        return False

if __name__ == "__main__":
    ok = map_share(
        r"\\192.168.86.152\video",
        os.environ.get("SHARE_USER", r"DESKTOP-5L867J8\training"),
        os.environ.get("SHARE_PASS", "amy4ever"),
    )
    print(f"Share mapped: {ok}")
    print(f"Exists: {os.path.exists(r'\\\\192.168.86.152\\video\\training_data')}")
