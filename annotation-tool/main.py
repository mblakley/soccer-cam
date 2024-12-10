import cv2
import os
import pandas as pd
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QFileDialog
from PyQt5.QtGui import QPixmap, QImage, QPainter, QPen
from PyQt5.QtCore import Qt, QRect, QPoint


class VideoAnnotationTool(QMainWindow):
    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('Soccer Ball Annotation Tool')
        self.showMaximized()  # Open window maximized to the screen

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
        self.video_path = QFileDialog.getOpenFileName(self, "Open Video", "./data", "Video Files (*.mp4 *.avi)")[0]
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

        # Scale the image to fit the QLabel height
        window_height = self.label.height()
        scaled_pixmap = pixmap.scaledToHeight(window_height, Qt.SmoothTransformation)

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

        # Get the scaling factor for mapping full-size coordinates to the zoomed region
        display_h = self.label.height()
        scale_factor = display_h / zoom_height

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
            zoom_factor = 0.9 if delta > 0 else 1.1  # Inverted: scroll up zooms out, scroll down zooms in

            x1, y1, x2, y2 = self.current_zoom_region
            frame_h, frame_w, _ = self.original_frame.shape

            # Calculate the new zoom region by scaling around the center
            center_x = (x1 + x2) / 2
            center_y = (y1 + y2) / 2
            new_width = (x2 - x1) * zoom_factor
            new_height = (y2 - y1) * zoom_factor

            # Enforce minimum and maximum zoom sizes
            min_zoom_height = 20
            aspect_ratio = self.label.width() / self.label.height()
            min_zoom_width = int(min_zoom_height * aspect_ratio)

            new_width = max(min_zoom_width, min(new_width, frame_w))
            new_height = max(min_zoom_height, min(new_height, frame_h))

            # Adjust the zoom region to maintain aspect ratio
            x1 = int(center_x - new_width / 2)
            x2 = int(center_x + new_width / 2)
            y1 = int(center_y - new_height / 2)
            y2 = int(center_y + new_height / 2)

            # Clamp the zoom region to frame boundaries
            if x1 < 0:
                x2 -= x1
                x1 = 0
            if y1 < 0:
                y2 -= y1
                y1 = 0
            if x2 > frame_w:
                x1 -= (x2 - frame_w)
                x2 = frame_w
            if y2 > frame_h:
                y1 -= (y2 - frame_h)
                y2 = frame_h

            # Update and apply the new zoom region
            self.current_zoom_region = (x1, y1, x2, y2)
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

    def mouseReleaseEvent(self, event):
        """Finalize the zoom box or annotation on mouse button release."""
        if event.button() == Qt.RightButton and self.start_point and self.end_point:
            # Finalize zoom
            x1_disp = min(self.start_point.x(), self.end_point.x())
            y1_disp = min(self.start_point.y(), self.end_point.y())
            x2_disp = max(self.start_point.x(), self.end_point.x())
            y2_disp = max(self.start_point.y(), self.end_point.y())

            # Map coordinates to the original frame
            x1, y1, x2, y2 = self.map_to_original(x1_disp, y1_disp, x2_disp, y2_disp)

            # Enforce minimum zoom region size with aspect ratio lock
            frame_h, frame_w, _ = self.original_frame.shape
            aspect_ratio = self.label.width() / self.label.height()
            min_zoom_height = 20  # Minimum height for zoom
            min_zoom_width = int(min_zoom_height * aspect_ratio)

            if (x2 - x1) < min_zoom_width or (y2 - y1) < min_zoom_height:
                print("Zoom region too small; resizing to maintain minimum size with aspect ratio.")
                x2 = x1 + max(min_zoom_width, x2 - x1)
                y2 = y1 + max(min_zoom_height, y2 - y1)

                # Clamp values to frame boundaries
                if x2 > frame_w:
                    x2 = frame_w
                    y2 = y1 + int((x2 - x1) / aspect_ratio)
                if y2 > frame_h:
                    y2 = frame_h
                    x2 = x1 + int((y2 - y1) * aspect_ratio)

            # Update zoom region
            self.current_zoom_region = (x1, y1, x2, y2)
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
        """Map display coordinates to original frame coordinates."""
        zoom_x1, zoom_y1, zoom_x2, zoom_y2 = self.current_zoom_region
        zoom_w = zoom_x2 - zoom_x1
        zoom_h = zoom_y2 - zoom_y1

        # Calculate scaling factor from displayed image to original frame
        display_h = self.label.pixmap().height()
        scale_factor = zoom_h / display_h

        # Map display coordinates to original frame coordinates
        x1 = int(zoom_x1 + x1_disp * scale_factor)
        y1 = int(zoom_y1 + y1_disp * scale_factor)
        x2 = int(zoom_x1 + x2_disp * scale_factor)
        y2 = int(zoom_y1 + y2_disp * scale_factor)

        return x1, y1, x2, y2

    def update_zoom_box(self):
        """Draw the zoom box dynamically on the displayed frame."""
        if self.start_point and self.end_point:
            pixmap = self.displayed_frame.copy()
            painter = QPainter(pixmap)
            painter.setPen(QPen(Qt.red, 2, Qt.SolidLine))

            # Draw rectangle on the image
            rect = QRect(self.start_point, self.end_point)
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
        elif event.key() == Qt.Key_Escape:
            self.reset_annotations()


    def reset_annotations(self):
        """Reset all annotations for the current frame and clear visual annotations."""
        # Remove annotations for the current frame
        self.annotations = self.annotations[self.annotations['frame'] != self.current_frame]
        print(f"All annotations for frame {self.current_frame} have been reset.")

        # Reload the current frame to clear drawn annotations
        self.display_frame()


if __name__ == "__main__":
    app = QApplication([])
    window = VideoAnnotationTool()
    window.show()
    app.exec()
