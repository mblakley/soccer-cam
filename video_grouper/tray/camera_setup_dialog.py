"""Camera discovery and setup wizard dialog."""

from __future__ import annotations

import asyncio
import logging
import threading

from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QCheckBox,
    QStackedWidget,
    QWidget,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QMessageBox,
)
from PyQt6.QtCore import QTimer, Qt

from video_grouper.cameras.discovery import (
    DiscoveredCamera,
    discover_onvif_devices,
    probe_reolink,
    configure_always_record,
    change_password,
)

logger = logging.getLogger(__name__)


class CameraSetupDialog(QDialog):
    """Multi-step camera discovery and configuration wizard."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Camera Setup Wizard")
        self.setMinimumSize(550, 400)

        self._result: tuple[str, str, str] | None = None
        self._discovered_cameras: list[DiscoveredCamera] = []
        self._selected_camera: DiscoveredCamera | None = None
        self._verified_ip: str = ""
        self._verified_username: str = ""
        self._verified_password: str = ""

        self._init_ui()

    # ── UI construction ──────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack)

        self._stack.addWidget(self._build_step1_discovery())
        self._stack.addWidget(self._build_step2_credentials())
        self._stack.addWidget(self._build_step3_configuration())

        self._stack.setCurrentIndex(0)

    def _build_step1_discovery(self) -> QWidget:
        """Step 1: Network discovery."""
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("Scan the network for cameras:"))

        # Scan button
        btn_layout = QHBoxLayout()
        self._scan_button = QPushButton("Scan Network")
        self._scan_button.clicked.connect(self._start_scan)
        btn_layout.addWidget(self._scan_button)

        self._scan_status = QLabel("")
        btn_layout.addWidget(self._scan_status)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Results table
        self._camera_table = QTableWidget(0, 4)
        self._camera_table.setHorizontalHeaderLabels(["IP", "Model", "Name", "MAC"])
        self._camera_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._camera_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._camera_table.horizontalHeader().setStretchLastSection(True)
        self._camera_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._camera_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._camera_table)

        # Manual IP entry
        manual_group = QGroupBox("Enter IP Manually")
        manual_layout = QHBoxLayout()
        self._manual_ip = QLineEdit()
        self._manual_ip.setPlaceholderText("192.168.1.100")
        manual_layout.addWidget(self._manual_ip)
        self._add_manual_button = QPushButton("Add")
        self._add_manual_button.clicked.connect(self._add_manual_ip)
        manual_layout.addWidget(self._add_manual_button)
        manual_group.setLayout(manual_layout)
        layout.addWidget(manual_group)

        # Next button
        self._step1_next = QPushButton("Next")
        self._step1_next.clicked.connect(self._step1_to_step2)
        layout.addWidget(self._step1_next, alignment=Qt.AlignmentFlag.AlignRight)

        return page

    def _build_step2_credentials(self) -> QWidget:
        """Step 2: Credentials and connection test."""
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("Enter camera credentials:"))

        form = QFormLayout()
        self._cred_ip_label = QLabel("")
        form.addRow("Camera IP:", self._cred_ip_label)

        self._cred_username = QLineEdit("admin")
        form.addRow("Username:", self._cred_username)

        self._cred_password = QLineEdit()
        self._cred_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self._cred_password)
        layout.addLayout(form)

        # Test button
        test_layout = QHBoxLayout()
        self._test_button = QPushButton("Test Connection")
        self._test_button.clicked.connect(self._test_connection)
        test_layout.addWidget(self._test_button)
        self._test_status = QLabel("")
        test_layout.addWidget(self._test_status)
        test_layout.addStretch()
        layout.addLayout(test_layout)

        # Device info display (read-only)
        self._device_info_group = QGroupBox("Device Info")
        info_layout = QFormLayout()
        self._info_model = QLabel("")
        self._info_firmware = QLabel("")
        self._info_serial = QLabel("")
        self._info_mac = QLabel("")
        info_layout.addRow("Model:", self._info_model)
        info_layout.addRow("Firmware:", self._info_firmware)
        info_layout.addRow("Serial:", self._info_serial)
        info_layout.addRow("MAC:", self._info_mac)
        self._device_info_group.setLayout(info_layout)
        self._device_info_group.setVisible(False)
        layout.addWidget(self._device_info_group)

        layout.addStretch()

        # Nav buttons
        nav_layout = QHBoxLayout()
        self._step2_back = QPushButton("Back")
        self._step2_back.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        nav_layout.addWidget(self._step2_back)
        nav_layout.addStretch()
        self._step2_next = QPushButton("Next")
        self._step2_next.setEnabled(False)
        self._step2_next.clicked.connect(self._step2_to_step3)
        nav_layout.addWidget(self._step2_next)
        layout.addLayout(nav_layout)

        return page

    def _build_step3_configuration(self) -> QWidget:
        """Step 3: Camera configuration options."""
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("Configure camera settings:"))

        # Always-record checkbox
        self._always_record_check = QCheckBox("Enable always-on recording")
        self._always_record_check.setChecked(True)
        layout.addWidget(self._always_record_check)

        # Change password checkbox and fields
        self._change_pass_check = QCheckBox("Change camera password")
        self._change_pass_check.setChecked(False)
        self._change_pass_check.toggled.connect(self._toggle_password_fields)
        layout.addWidget(self._change_pass_check)

        self._new_pass_group = QGroupBox("New Password")
        pass_layout = QFormLayout()
        self._new_password = QLineEdit()
        self._new_password.setEchoMode(QLineEdit.EchoMode.Password)
        pass_layout.addRow("New Password:", self._new_password)
        self._confirm_password = QLineEdit()
        self._confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
        pass_layout.addRow("Confirm Password:", self._confirm_password)
        self._new_pass_group.setLayout(pass_layout)
        self._new_pass_group.setVisible(False)
        layout.addWidget(self._new_pass_group)

        self._config_status = QLabel("")
        layout.addWidget(self._config_status)

        layout.addStretch()

        # Nav buttons
        nav_layout = QHBoxLayout()
        self._step3_back = QPushButton("Back")
        self._step3_back.clicked.connect(lambda: self._stack.setCurrentIndex(1))
        nav_layout.addWidget(self._step3_back)
        nav_layout.addStretch()
        self._finish_button = QPushButton("Finish")
        self._finish_button.clicked.connect(self._finish)
        nav_layout.addWidget(self._finish_button)
        layout.addLayout(nav_layout)

        return page

    # ── Step 1 logic ─────────────────────────────────────────────────

    def _start_scan(self):
        """Run network discovery in a background thread."""
        self._scan_button.setEnabled(False)
        self._scan_status.setText("Scanning network...")
        self._camera_table.setRowCount(0)
        self._discovered_cameras.clear()

        def scan_func():
            ips = discover_onvif_devices(timeout=3.0)
            cameras = []
            for ip in ips:
                cam = asyncio.run(probe_reolink(ip, "admin", ""))
                if cam:
                    cameras.append(cam)
            return cameras

        self._run_in_thread(scan_func, self._on_scan_complete)

    def _on_scan_complete(self, result, error):
        self._scan_button.setEnabled(True)
        if error:
            self._scan_status.setText(f"Scan failed: {error}")
            return

        self._discovered_cameras = result or []
        self._scan_status.setText(f"Found {len(self._discovered_cameras)} camera(s)")
        self._populate_camera_table()

    def _populate_camera_table(self):
        self._camera_table.setRowCount(len(self._discovered_cameras))
        for row, cam in enumerate(self._discovered_cameras):
            self._camera_table.setItem(row, 0, QTableWidgetItem(cam.ip))
            self._camera_table.setItem(row, 1, QTableWidgetItem(cam.model))
            self._camera_table.setItem(row, 2, QTableWidgetItem(cam.name))
            self._camera_table.setItem(row, 3, QTableWidgetItem(cam.mac))

    def _add_manual_ip(self):
        """Add a manually entered IP to the table."""
        ip = self._manual_ip.text().strip()
        if not ip:
            return

        # Add a row with just the IP, other fields unknown
        row = self._camera_table.rowCount()
        self._camera_table.insertRow(row)
        self._camera_table.setItem(row, 0, QTableWidgetItem(ip))
        self._camera_table.setItem(row, 1, QTableWidgetItem(""))
        self._camera_table.setItem(row, 2, QTableWidgetItem(""))
        self._camera_table.setItem(row, 3, QTableWidgetItem(""))
        self._manual_ip.clear()

    def _step1_to_step2(self):
        """Move from step 1 to step 2."""
        selected = self._camera_table.selectedItems()
        if not selected:
            # If there are rows but none selected, select the first
            if self._camera_table.rowCount() > 0:
                self._camera_table.selectRow(0)
                selected = self._camera_table.selectedItems()
            else:
                QMessageBox.warning(
                    self,
                    "No Camera Selected",
                    "Please scan for cameras or enter an IP address.",
                )
                return

        row = self._camera_table.currentRow()
        ip = self._camera_table.item(row, 0).text()

        self._cred_ip_label.setText(ip)
        self._verified_ip = ip
        self._device_info_group.setVisible(False)
        self._step2_next.setEnabled(False)
        self._test_status.setText("")
        self._stack.setCurrentIndex(1)

    # ── Step 2 logic ─────────────────────────────────────────────────

    def _test_connection(self):
        """Test connection with provided credentials."""
        ip = self._cred_ip_label.text()
        username = self._cred_username.text()
        password = self._cred_password.text()

        self._test_button.setEnabled(False)
        self._test_status.setText("Testing connection...")

        def test_func():
            return asyncio.run(probe_reolink(ip, username, password))

        self._run_in_thread(test_func, self._on_test_complete)

    def _on_test_complete(self, result, error):
        self._test_button.setEnabled(True)
        if error:
            self._test_status.setText(f"Error: {error}")
            self._device_info_group.setVisible(False)
            self._step2_next.setEnabled(False)
            return

        if result is None:
            self._test_status.setText("Connection failed. Check credentials.")
            self._device_info_group.setVisible(False)
            self._step2_next.setEnabled(False)
            return

        self._test_status.setText("Connection successful!")
        self._selected_camera = result
        self._verified_username = self._cred_username.text()
        self._verified_password = self._cred_password.text()

        # Show device info
        self._info_model.setText(result.model)
        self._info_firmware.setText(result.firmware)
        self._info_serial.setText(result.serial)
        self._info_mac.setText(result.mac)
        self._device_info_group.setVisible(True)
        self._step2_next.setEnabled(True)

    def _step2_to_step3(self):
        """Move from step 2 to step 3."""
        self._config_status.setText("")
        self._stack.setCurrentIndex(2)

    # ── Step 3 logic ─────────────────────────────────────────────────

    def _toggle_password_fields(self, checked):
        self._new_pass_group.setVisible(checked)

    def _finish(self):
        """Apply configuration and close dialog."""
        # Validate password change fields if checked
        if self._change_pass_check.isChecked():
            new_pass = self._new_password.text()
            confirm = self._confirm_password.text()
            if not new_pass:
                QMessageBox.warning(
                    self, "Validation Error", "New password cannot be empty."
                )
                return
            if new_pass != confirm:
                QMessageBox.warning(self, "Validation Error", "Passwords do not match.")
                return

        do_always_record = self._always_record_check.isChecked()
        do_change_pass = self._change_pass_check.isChecked()

        if not do_always_record and not do_change_pass:
            # Nothing to configure, just accept
            self._result = (
                self._verified_ip,
                self._verified_username,
                self._verified_password,
            )
            self.accept()
            return

        self._finish_button.setEnabled(False)
        self._config_status.setText("Applying configuration...")

        ip = self._verified_ip
        username = self._verified_username
        password = self._verified_password

        def config_func():
            results = {}
            if do_always_record:
                results["always_record"] = asyncio.run(
                    configure_always_record(ip, username, password)
                )
            if do_change_pass:
                new_pass = self._new_password.text()
                results["change_password"] = asyncio.run(
                    change_password(ip, username, password, new_pass)
                )
            return results

        self._run_in_thread(config_func, self._on_config_complete)

    def _on_config_complete(self, result, error):
        self._finish_button.setEnabled(True)

        if error:
            self._config_status.setText(f"Configuration error: {error}")
            return

        messages = []
        all_ok = True

        if "always_record" in result:
            if result["always_record"]:
                messages.append("Always-on recording: enabled")
            else:
                messages.append("Always-on recording: FAILED")
                all_ok = False

        final_password = self._verified_password
        if "change_password" in result:
            if result["change_password"]:
                messages.append("Password changed successfully")
                final_password = self._new_password.text()
            else:
                messages.append("Password change: FAILED")
                all_ok = False

        self._config_status.setText("\n".join(messages))

        if all_ok:
            self._result = (
                self._verified_ip,
                self._verified_username,
                final_password,
            )
            self.accept()
        else:
            QMessageBox.warning(
                self,
                "Configuration Warning",
                "Some configuration steps failed:\n" + "\n".join(messages),
            )

    # ── Helpers ──────────────────────────────────────────────────────

    def _run_in_thread(self, func, on_done):
        """Run func in a background thread, call on_done(result, error) on UI thread."""

        def thread_target():
            try:
                result = func()
                QTimer.singleShot(0, lambda r=result: on_done(r, None))
            except Exception as exc:
                QTimer.singleShot(0, lambda e=exc: on_done(None, e))

        threading.Thread(target=thread_target, daemon=True).start()

    def get_result(self) -> tuple[str, str, str] | None:
        """Return (ip, username, password) if dialog was accepted, else None."""
        return self._result
