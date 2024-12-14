import cv2
import os
import pandas as pd
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QFileDialog
from PyQt5.QtGui import QPixmap, QImage, QPainter, QPen
from PyQt5.QtCore import Qt, QRect, QPoint


class VideoAnnotationTool(QMainWindow):
    def __init__(self, video_path=None):
        super().__init__()
        self.video_path = video_path  # Store the video path
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('Soccer Ball Annotation Tool')
        self.showFullScreen()

        # QLabel to display the video frame
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)
        self.setCentralWidget(self.label)

        # Variables for video, annotations, and zoom
        self.current_frame = 0
        self.annotations = pd.DataFrame(columns=['frame', 'x_center', 'y_center', 'width', 'height'])
        self.original_frame = None
        self.zoom_frame = None
        self.current_zoom_region = None  # Store the currently displayed region in absolute coordinates

        # Load video
        if not self.video_path:  # If no path is provided, prompt the user
            self.video_path = QFileDialog.getOpenFileName(
                self, "Open Video", "./data", "Video Files (*.mp4 *.avi)"
            )[0]

        if self.video_path:
            self.cap = cv2.VideoCapture(self.video_path)
            self.load_existing_annotations()
            self.load_frame()
        else:
            print("No video file selected.")
            self.close()

    def load_existing_annotations(self):
        """Check for annotations.csv and update the starting frame."""
        video_dir = os.path.dirname(self.video_path)
        csv_path = os.path.join(video_dir, "annotations.csv")

        if os.path.exists(csv_path):
            print(f"Found annotations file: {csv_path}")
            try:
                # Load existing annotations
                self.annotations = pd.read_csv(csv_path)

                # Ensure all required columns are present
                required_columns = {'frame', 'x_center', 'y_center', 'width', 'height'}
                if not required_columns.issubset(self.annotations.columns):
                    print("Annotations file is missing required columns.")
                    self.annotations = pd.DataFrame(columns=required_columns)

                # Determine the last annotated frame
                if not self.annotations.empty:
                    last_frame = self.annotations['frame'].max()
                    self.current_frame = last_frame + 25
                    print(f"Starting at frame {self.current_frame} (last annotated frame + 25).")
            except Exception as e:
                print(f"Error reading annotations file: {e}")
                self.annotations = pd.DataFrame(columns=['frame', 'x_center', 'y_center', 'width', 'height'])
        else:
            print("No annotations file found. Starting at frame 0.")


    def load_frame(self):
        """Load the current frame from the video."""
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame)
        ret, frame = self.cap.read()
        if not ret:
            print("End of video")
            return

        self.original_frame = frame  # Store the original frame

        # Reset zoom region if none exists
        if self.current_zoom_region is None:
            print(f"Setting zoom region to: 0, 0, {frame.shape[1]}, {frame.shape[0]}")
            self.current_zoom_region = (0, 0, frame.shape[1], frame.shape[0])

        self.apply_zoom()
        self.display_frame()

        # Save annotations after frame initialization
        self.save_annotations_to_csv()

    def save_annotations_to_csv(self):
        """Save all annotations to a CSV file in YOLO-compatible format."""
        if not self.video_path or self.original_frame is None:
            print("Cannot save annotations: video not loaded or no frame available.")
            return

        # Determine the directory of the loaded video file
        video_dir = os.path.dirname(self.video_path)
        csv_path = os.path.join(video_dir, "annotations.csv")  # Save as annotations.csv in the same directory

        # Ensure valid annotations
        if self.annotations.empty:
            print("No annotations to save.")
            return

        # Save the annotations DataFrame to CSV
        self.annotations.to_csv(csv_path, index=False)
        print(f"Annotations saved to {csv_path}")

    def apply_zoom(self):
        """Apply the currently selected zoom region."""
        x1, y1, x2, y2 = self.current_zoom_region
        self.zoom_frame = self.original_frame[y1:y2, x1:x2]

    def display_frame(self):
        """Display the current frame or zoomed region with annotations."""
        frame = self.zoom_frame

        # Convert frame to QImage for PyQt
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame.shape
        bytes_per_line = ch * w
        q_img = QImage(frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(q_img)

        # Scale the image to fit the QLabel width
        window_width = self.label.width()
        scaled_pixmap = pixmap.scaledToWidth(window_width, Qt.SmoothTransformation)

        # Draw annotations on the pixmap
        annotated_pixmap = self.draw_annotations(scaled_pixmap)

        self.label.setPixmap(annotated_pixmap)
        self.displayed_frame = annotated_pixmap  # Store the displayed frame for further use

    def draw_annotations(self, pixmap):
        """Draw annotations for the current frame on the given pixmap."""
        if self.annotations.empty or self.original_frame is None:
            return pixmap  # Nothing to draw

        painter = QPainter(pixmap)
        painter.setPen(QPen(Qt.green, 2, Qt.SolidLine))

        # Get the current zoom region and its dimensions
        zoom_x1, zoom_y1, zoom_x2, zoom_y2 = self.current_zoom_region
        zoom_width = zoom_x2 - zoom_x1
        zoom_height = zoom_y2 - zoom_y1

        # Calculate the scaling factor based on the displayed width
        display_w = self.label.width()
        scale_factor = display_w / zoom_width

        # Get annotations for the current frame
        frame_annotations = self.annotations[self.annotations['frame'] == self.current_frame]

        for _, annotation in frame_annotations.iterrows():
            # Map annotation coordinates from full image to zoomed display
            x_center = (annotation['x_center'] - zoom_x1) * scale_factor
            y_center = (annotation['y_center'] - zoom_y1) * scale_factor
            width = annotation['width'] * scale_factor
            height = annotation['height'] * scale_factor

            # Calculate rectangle corners
            x1 = x_center - width / 2
            y1 = y_center - height / 2
            x2 = x_center + width / 2
            y2 = y_center + height / 2

            # Draw the annotation rectangle
            painter.drawRect(int(x1), int(y1), int(x2 - x1), int(y2 - y1))

        painter.end()
        return pixmap


    def wheelEvent(self, event):
        """Handle zoom in/out with the scroll wheel while holding Ctrl."""
        if QApplication.keyboardModifiers() == Qt.ControlModifier:
            delta = event.angleDelta().y() / 120  # Positive for scroll up, negative for scroll down
            zoom_factor = 0.9 if delta > 0 else 1.1  # Scroll up to zoom in, scroll down to zoom out

            x1, y1, x2, y2 = self.current_zoom_region

            # Calculate the center of the current zoom region
            center_x = (x1 + x2) / 2
            center_y = (y1 + y2) / 2
            new_width = (x2 - x1) * zoom_factor
            new_height = (y2 - y1) * zoom_factor

            # Calculate the new zoom region
            self.current_zoom_region = self.calculate_zoom_region(center_x, center_y, new_width, new_height)

            # Apply and display the new zoom
            self.apply_zoom()
            self.display_frame()


    def mousePressEvent(self, event):
        """Start drawing the zoom box or annotation on mouse button press."""
        if event.button() == Qt.RightButton:
            # Start zoom box drawing
            self.start_point = event.pos()
            self.end_point = None  # Reset end point
        elif event.button() == Qt.LeftButton:
            # Start annotation
            self.start_point = event.pos()
            self.end_point = None  # Reset end point

    def mouseMoveEvent(self, event):
        """Update the zoom box or annotation while dragging."""
        if self.start_point and event.buttons() == Qt.RightButton:
            # Update zoom box
            aspect_ratio = self.label.width() / self.label.height()

            # Calculate the new box dimensions
            delta_x = event.pos().x() - self.start_point.x()
            delta_y = event.pos().y() - self.start_point.y()

            if abs(delta_x) / aspect_ratio > abs(delta_y):
                delta_y = delta_x / aspect_ratio
            else:
                delta_x = delta_y * aspect_ratio

            self.end_point = QPoint(self.start_point.x() + int(delta_x), self.start_point.y() + int(delta_y))
            self.update_zoom_box()
        elif self.start_point and event.buttons() == Qt.LeftButton:
            # Update annotation box
            delta = abs(event.pos().y() - self.start_point.y())  # Square size based on vertical drag
            self.end_point = QPoint(self.start_point.x() + delta, self.start_point.y() + delta)
            self.update_annotation()

    def update_annotation(self):
        """Draw the annotation box dynamically."""
        if self.start_point and self.end_point:
            # Create a copy of the displayed pixmap to overlay the annotation
            pixmap = self.displayed_frame.copy()
            painter = QPainter(pixmap)
            painter.setPen(QPen(Qt.green, 2, Qt.SolidLine))

            # Calculate the box size based on the drag distance
            center_x, center_y = self.start_point.x(), self.start_point.y()
            drag_y = self.end_point.y()
            box_size = abs(drag_y - center_y)

            # Draw the square annotation box and a vertical line indicating the drag
            painter.drawLine(center_x, center_y, center_x, drag_y)  # Vertical line
            painter.drawRect(center_x - box_size, center_y - box_size, box_size * 2, box_size * 2)  # Square box
            painter.end()

            # Update the QLabel to display the dynamically drawn annotation
            self.label.setPixmap(pixmap)

    def calculate_zoom_region(self, center_x, center_y, zoom_width, zoom_height):
        """Calculate the zoom region while clamping to image boundaries and maintaining aspect ratio."""
        frame_h, frame_w, _ = self.original_frame.shape

        # Enforce minimum zoom width and height
        aspect_ratio = frame_w / frame_h
        min_zoom_height = 20
        min_zoom_width = int(min_zoom_height * aspect_ratio)

        zoom_width = max(min_zoom_width, min(zoom_width, frame_w))
        zoom_height = max(min_zoom_height, min(zoom_height, frame_h))

        # Calculate new zoom box
        x1 = int(center_x - zoom_width / 2)
        x2 = int(center_x + zoom_width / 2)
        y1 = int(center_y - zoom_height / 2)
        y2 = int(center_y + zoom_height / 2)

        # Log initial values
        print(f"Initial: x1={x1}, y1={y1}, x2={x2}, y2={y2}, zoom_width={zoom_width}, zoom_height={zoom_height}")

        # Clamp to the image boundaries
        if x1 < 0:
            x2 -= x1  # Shift x2 to maintain the zoom width
            x1 = 0
            print(f"Clamped x1: x1={x1}, x2={x2}")
        if x2 > frame_w:
            x1 -= (x2 - frame_w)  # Shift x1 to maintain the zoom width
            x2 = frame_w
            print(f"Clamped x2: x1={x1}, x2={x2}")

        if y1 < 0:
            y2 -= y1  # Shift y2 to maintain the zoom height
            y1 = 0
            print(f"Clamped y1: y1={y1}, y2={y2}")
        if y2 > frame_h:
            y1 -= (y2 - frame_h)  # Shift y1 to maintain the zoom height
            y2 = frame_h
            print(f"Clamped y2: y1={y1}, y2={y2}")

        # Final logging
        print(f"Final: x1={x1}, y1={y1}, x2={x2}, y2={y2}")
        return x1, y1, x2, y2


    def mouseReleaseEvent(self, event):
        """Finalize the zoom box or annotation on mouse button release."""
        if event.button() == Qt.RightButton and self.start_point and self.end_point:
            # Calculate display coordinates
            x1_disp = min(self.start_point.x(), self.end_point.x())
            y1_disp = min(self.start_point.y(), self.end_point.y())
            x2_disp = max(self.start_point.x(), self.end_point.x())
            y2_disp = max(self.start_point.y(), self.end_point.y())

            # Map display coordinates to original frame
            x1, y1, x2, y2 = self.map_to_original(x1_disp, y1_disp, x2_disp, y2_disp)

            # Calculate the new zoom region
            center_x = (x1 + x2) / 2
            center_y = (y1 + y2) / 2
            zoom_width = x2 - x1
            zoom_height = y2 - y1
            self.current_zoom_region = self.calculate_zoom_region(center_x, center_y, zoom_width, zoom_height)

            # Apply and display the new zoom
            self.apply_zoom()
            self.display_frame()

            # Reset drawing points
            self.start_point = None
            self.end_point = None
        elif event.button() == Qt.LeftButton and self.start_point and self.end_point:
            # Finalize annotation
            self.finalize_annotation()


    def finalize_annotation(self):
        """Save the annotation in pixels relative to the original frame and reset points."""
        if self.start_point and self.end_point:
            drag_y = self.end_point.y()
            box_size = abs(drag_y - self.start_point.y())

            # Map display coordinates to original frame coordinates
            zoom_x1, zoom_y1, zoom_x2, zoom_y2 = self.current_zoom_region
            scale_factor = (zoom_y2 - zoom_y1) / self.label.height()

            center_x_disp, center_y_disp = self.start_point.x(), self.start_point.y()
            center_x = int(zoom_x1 + center_x_disp * scale_factor)
            center_y = int(zoom_y1 + center_y_disp * scale_factor)
            box_size = int(box_size * scale_factor)

            width = height = box_size * 2

            # Save annotation in pixels relative to the full-size image
            new_annotation = pd.DataFrame([{
                'frame': self.current_frame,
                'x_center': center_x,
                'y_center': center_y,
                'width': width,
                'height': height
            }])

            # Append to the annotations DataFrame
            self.annotations = pd.concat([self.annotations, new_annotation], ignore_index=True)

            print(f"Annotation saved for frame {self.current_frame}: "
                  f"x_center={center_x}, y_center={center_y}, width={width}, height={height}")

            # Reset points
            self.start_point = None
            self.end_point = None


    def map_to_original(self, x1_disp, y1_disp, x2_disp, y2_disp):
        zoom_x1, zoom_y1, zoom_x2, zoom_y2 = self.current_zoom_region
        zoom_w = zoom_x2 - zoom_x1
        zoom_h = zoom_y2 - zoom_y1

        # Calculate the scaling factor based on width
        display_w = self.label.width()
        scale_factor = zoom_w / display_w

        # Calculate vertical offset if the image is centered vertically in the label
        display_h = int(zoom_h / scale_factor)
        vertical_offset = (self.label.height() - display_h) // 2

        # Adjust display coordinates to exclude the offset
        y1_disp_adjusted = y1_disp - vertical_offset
        y2_disp_adjusted = y2_disp - vertical_offset

        # Clamp display coordinates to the displayed image bounds
        y1_disp_adjusted = max(0, min(display_h, y1_disp_adjusted))
        y2_disp_adjusted = max(0, min(display_h, y2_disp_adjusted))

        # Map display coordinates to original frame coordinates
        x1 = int(zoom_x1 + x1_disp * scale_factor)
        y1 = int(zoom_y1 + y1_disp_adjusted * scale_factor)
        x2 = int(zoom_x1 + x2_disp * scale_factor)
        y2 = int(zoom_y1 + y2_disp_adjusted * scale_factor)

        # Clamp to the zoom region
        x1 = max(zoom_x1, x1)
        y1 = max(zoom_y1, y1)
        x2 = min(zoom_x2, x2)
        y2 = min(zoom_y2, y2)

        return x1, y1, x2, y2


    def update_zoom_box(self):
        """Draw the zoom box dynamically on the displayed frame."""
        if self.start_point and self.end_point:
            pixmap = self.displayed_frame.copy()
            painter = QPainter(pixmap)
            painter.setPen(QPen(Qt.red, 2, Qt.SolidLine))

            # Calculate vertical offset for the displayed image
            display_w = self.label.width()
            display_h = int(self.original_frame.shape[0] * (display_w / self.original_frame.shape[1]))
            vertical_offset = (self.label.height() - display_h) // 2

            # Adjust the start and end points to exclude the vertical offset
            start_point_adjusted = QPoint(self.start_point.x(), self.start_point.y() - vertical_offset)
            end_point_adjusted = QPoint(self.end_point.x(), self.end_point.y() - vertical_offset)

            # Draw rectangle on the image
            rect = QRect(start_point_adjusted, end_point_adjusted)
            painter.drawRect(rect)
            painter.end()

            # Update the label with the new pixmap
            self.label.setPixmap(pixmap)


    def keyPressEvent(self, event):
        """Handle keyboard input for navigation, zoom reset, and annotation reset."""
        single_frame = QApplication.keyboardModifiers() == Qt.ShiftModifier

        # Get the total number of frames in the video
        total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if event.key() == Qt.Key_Right:
            # Increment frame with boundary check
            self.current_frame += 1 if single_frame else 25
            if self.current_frame >= total_frames:
                self.current_frame = total_frames - 1  # Clamp to the last frame
                print(f"Reached the last frame: {self.current_frame}")
            self.load_frame()
        elif event.key() == Qt.Key_Left:
            # Decrement frame with boundary check
            self.current_frame -= 1 if single_frame else 25
            if self.current_frame < 0:
                self.current_frame = 0  # Clamp to the first frame
                print("Reached the first frame.")
            self.load_frame()
        elif event.key() == Qt.Key_R:
            self.current_zoom_region = None  # Reset zoom
            self.load_frame()
        elif event.key() == Qt.Key_A:
            self.reset_annotations()
        elif event.key() == Qt.Key_Escape:
            self.close()


    def reset_annotations(self):
        """Reset all annotations for the current frame and clear visual annotations."""
        # Remove annotations for the current frame
        self.annotations = self.annotations[self.annotations['frame'] != self.current_frame]
        print(f"All annotations for frame {self.current_frame} have been reset.")

        # Reload the current frame to clear drawn annotations
        self.display_frame()