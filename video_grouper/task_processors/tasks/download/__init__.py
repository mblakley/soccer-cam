"""Download task implementations."""

from .base_download_task import BaseDownloadTask
from .dahua_download_task import DahuaDownloadTask

__all__ = [
    'BaseDownloadTask',
    'DahuaDownloadTask', 
] 