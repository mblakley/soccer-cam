import sys
from PyQt5.QtWidgets import QApplication
from video_annotation_tool import VideoAnnotationTool

if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Check for a video path argument
    video_path = sys.argv[1] if len(sys.argv) > 1 else None

    window = VideoAnnotationTool(video_path=video_path)
    window.show()

    sys.exit(app.exec())
