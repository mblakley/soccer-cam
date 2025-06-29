import os
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QDialog)
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtCore import Qt, QSize
from video_grouper.utils.time_utils import parse_dt_from_string_with_tz, convert_utc_to_local

class ImagePreviewDialog(QDialog):
    """A simple dialog to show a larger version of an image."""
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image Preview")
        layout = QVBoxLayout()
        self.image_label = QLabel()
        self.image_label.setPixmap(pixmap)
        layout.addWidget(self.image_label)
        self.setLayout(layout)

class QueueItemWidget(QWidget):
    """
    A custom widget for displaying an item in a queue, with a skip button and optional thumbnail.
    """
    def __init__(self, item_text, file_path, skip_callback, show_thumbnail=True, group_name=None, timezone_str="UTC", parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.skip_callback = skip_callback

        layout = QHBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)

        # Conditionally show thumbnail
        if show_thumbnail:
            self.thumbnail_label = QLabel()
            self.thumbnail_label.setFixedSize(128, 72)
            self.thumbnail_label.setStyleSheet("background-color: #333; border: 1px solid #555;")
            self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.thumbnail_label.setText("No Preview")
            self.preview_pixmap = None
            self.set_thumbnail()
            layout.addWidget(self.thumbnail_label)

        # Main content
        main_content_layout = QVBoxLayout()
        
        # Display group name and converted local time
        if group_name:
            try:
                # The source timezone is hardcoded as America/New_York
                source_dt = parse_dt_from_string_with_tz(group_name, 'America/New_York')
                local_time = convert_utc_to_local(source_dt, timezone_str)
                local_time_str = local_time.strftime('%Y-%m-%d %I:%M:%S %p %Z')
                main_content_layout.addWidget(QLabel(f"<b>Group:</b> {group_name}"))
                main_content_layout.addWidget(QLabel(f"<i>{local_time_str}</i>"))
            except ValueError:
                main_content_layout.addWidget(QLabel(f"<b>Group:</b> {group_name}"))

        self.name_label = QLabel(item_text)
        main_content_layout.addWidget(self.name_label)
        
        # Skip button
        self.skip_button = QPushButton("Skip")
        self.skip_button.setFixedWidth(80)
        if skip_callback:
            self.skip_button.clicked.connect(self._on_skip_clicked)
        else:
            self.skip_button.setEnabled(False)
            self.skip_button.setToolTip("Action not available for this item.")
        main_content_layout.addWidget(self.skip_button)

        layout.addLayout(main_content_layout)
        self.setLayout(layout)

    def set_thumbnail(self):
        # Infer thumbnail path from video path (e.g., video.mp4 -> video.jpg)
        base, _ = os.path.splitext(self.file_path)
        thumbnail_path = f"{base}.jpg"
        
        if os.path.exists(thumbnail_path):
            pixmap = QPixmap(thumbnail_path)
            self.preview_pixmap = pixmap # Store full-size pixmap
            # Scale for display
            scaled_pixmap = pixmap.scaled(
                self.thumbnail_label.size(), 
                Qt.AspectRatioMode.KeepAspectRatio, 
                Qt.TransformationMode.SmoothTransformation
            )
            self.thumbnail_label.setPixmap(scaled_pixmap)
            self.thumbnail_label.mousePressEvent = self.show_image_preview
        
    def show_image_preview(self, event):
        if self.preview_pixmap:
            dialog = ImagePreviewDialog(self.preview_pixmap, self)
            dialog.exec()

    def _on_skip_clicked(self):
        if self.skip_callback:
            self.skip_callback(self.file_path) 