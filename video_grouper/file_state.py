from datetime import datetime

class FileState:
    def __init__(self, file_path: str, group_dir: str, total_size: int = 0, downloaded_bytes: int = 0, status: str = "pending", start_time: datetime = None, end_time: datetime = None):
        self.file_path = file_path
        self.group_dir = group_dir
        self.total_size = total_size
        self.downloaded_bytes = downloaded_bytes
        self.status = status
        self.last_updated = datetime.now()
        self.error_message = None
        self.mp4_path = file_path.replace('.dav', '.mp4')
        self.start_time = start_time
        self.end_time = end_time
        self.screenshot_path = None
        self.skip = False

    def to_dict(self):
        return {
            'file_path': self.file_path,
            'group_dir': self.group_dir,
            'total_size': self.total_size,
            'downloaded_bytes': self.downloaded_bytes,
            'status': self.status,
            'last_updated': self.last_updated.isoformat(),
            'error_message': self.error_message,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'screenshot_path': self.screenshot_path,
            'skip': self.skip
        }

    @classmethod
    def from_dict(cls, data):
        state = cls(
            file_path=data['file_path'],
            group_dir=data['group_dir'],
            total_size=data['total_size'],
            downloaded_bytes=data['downloaded_bytes'],
            status=data['status']
        )
        state.last_updated = datetime.fromisoformat(data['last_updated'])
        state.error_message = data['error_message']
        if data['start_time']:
            state.start_time = datetime.fromisoformat(data['start_time'])
        if data['end_time']:
            state.end_time = datetime.fromisoformat(data['end_time'])
        state.screenshot_path = data.get('screenshot_path')
        state.skip = data.get('skip', False)
        return state
