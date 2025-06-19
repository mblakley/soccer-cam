from datetime import datetime

class FileState:
    def __init__(self, file_path: str, group_dir: str, status: str = "pending", start_time: datetime = None, end_time: datetime = None):
        self.file_path = file_path
        self.group_dir = group_dir
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
            status=data.get('status', 'pending')
        )
        if 'last_updated' in data and data['last_updated']:
            state.last_updated = datetime.fromisoformat(data['last_updated'])
        state.error_message = data.get('error_message')
        if data.get('start_time'):
            state.start_time = datetime.fromisoformat(data['start_time'])
        if data.get('end_time'):
            state.end_time = datetime.fromisoformat(data['end_time'])
        state.screenshot_path = data.get('screenshot_path')
        state.skip = data.get('skip', False)
        return state
