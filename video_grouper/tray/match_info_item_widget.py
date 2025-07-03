import os
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFormLayout,
    QGridLayout,
    QScrollArea,
)
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt
from video_grouper.models import DirectoryState
from video_grouper.utils.time_utils import parse_utc_from_string, convert_utc_to_local
from .queue_item_widget import ImagePreviewDialog


class MatchInfoItemWidget(QWidget):
    """
    A widget for a single group directory needing match info, showing thumbnails and input fields.
    """

    def __init__(
        self, group_dir_path, refresh_callback, timezone_str="UTC", parent=None
    ):
        super().__init__(parent)
        self.group_dir_path = group_dir_path
        self.refresh_callback = refresh_callback
        self.dir_state = DirectoryState(group_dir_path)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)

        # Group name label and local time
        group_name = os.path.basename(group_dir_path)
        main_layout.addWidget(QLabel(f"<h3>Group: {group_name}</h3>"))
        try:
            utc_time = parse_utc_from_string(group_name)
            local_time = convert_utc_to_local(utc_time, timezone_str)
            local_time_str = local_time.strftime("%Y-%m-%d %I:%M:%S %p %Z")
            main_layout.addWidget(QLabel(f"<i>{local_time_str}</i>"))
        except ValueError:
            pass  # If parsing fails, just don't show the local time

        # Scrollable thumbnail grid
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setFixedHeight(240)

        self.thumbnail_container = QWidget()
        self.thumbnail_grid = QGridLayout(self.thumbnail_container)
        self.populate_thumbnails()
        self.scroll_area.setWidget(self.thumbnail_container)
        main_layout.addWidget(self.scroll_area)

        # Form for match info
        form_layout = QFormLayout()
        self.start_time_offset = QLineEdit("00:00")
        self.my_team_name = QLineEdit()
        self.opponent_team_name = QLineEdit()
        self.location = QLineEdit()

        form_layout.addRow("Start Time Offset (MM:SS):", self.start_time_offset)
        form_layout.addRow("My Team Name:", self.my_team_name)
        form_layout.addRow("Opponent Team Name:", self.opponent_team_name)
        form_layout.addRow("Location:", self.location)
        main_layout.addLayout(form_layout)

        # Save button
        save_button = QPushButton("Save Match Info")
        save_button.clicked.connect(self.on_save_clicked)
        main_layout.addWidget(save_button)

        self.setLayout(main_layout)

    def populate_thumbnails(self):
        col, row = 0, 0
        thumbnail_files = [
            f for f in os.listdir(self.group_dir_path) if f.lower().endswith(".jpg")
        ]
        for filename in sorted(thumbnail_files):
            thumbnail_path = os.path.join(self.group_dir_path, filename)
            pixmap = QPixmap(thumbnail_path)

            thumb_label = QLabel()
            thumb_label.setFixedSize(128, 72)
            thumb_label.setPixmap(
                pixmap.scaled(
                    thumb_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            thumb_label.mousePressEvent = (
                lambda event, p=pixmap: self.show_image_preview(p)
            )

            self.thumbnail_grid.addWidget(thumb_label, row, col)
            col += 1
            if col > 3:
                col = 0
                row += 1

    def show_image_preview(self, pixmap):
        dialog = ImagePreviewDialog(pixmap, self)
        dialog.exec()

    def on_save_clicked(self):
        """Callback when the save button is clicked."""
        if self.refresh_callback:
            # Format the start time offset to HH:MM:SS format
            start_time = self.start_time_offset.text().strip()
            if ":" not in start_time:
                # Assume MM:SS format and convert to HH:MM:SS
                try:
                    minutes, seconds = start_time.split(":", 1)
                    start_time = f"00:{minutes.zfill(2)}:{seconds.zfill(2)}"
                except ValueError:
                    # If no colon, assume seconds only
                    try:
                        seconds = int(start_time)
                        minutes = seconds // 60
                        seconds = seconds % 60
                        start_time = f"00:{minutes:02d}:{seconds:02d}"
                    except ValueError:
                        start_time = "00:00:00"
            elif len(start_time.split(":")) == 2:
                # MM:SS format, add hours
                start_time = f"00:{start_time}"

            info_dict = {
                "start_time_offset": start_time,
                "my_team_name": self.my_team_name.text().strip(),
                "opponent_team_name": self.opponent_team_name.text().strip(),
                "location": self.location.text().strip(),
            }
            self.refresh_callback(self.group_dir_path, info_dict)
