"""
MCP47FEB28 Control Console

Desktop application for the Tiny 2040 MicroPython firmware supplied with this
project.  Communication uses newline-delimited JSON over the RP2040 USB serial
port.

Requirements:
    Python 3.10+
    PySide6
    pyserial

Run:
    python dac_control_app.py

Run without hardware for interface exploration:
    python dac_control_app.py --demo
"""

from __future__ import annotations

import argparse
import copy
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import serial
from serial.tools import list_ports

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "MCP47FEB28 Control Console"
APP_VERSION = "1.0.1"
PROTOCOL_VERSION = 1

CHANNEL_NAMES = (
    "out_6",
    "out_7",
    "out_4",
    "out_5",
    "out_2",
    "out_3",
    "out_0",
    "out_1",
)

VREF_NAMES = {
    0: "VDD",
    1: "Internal band gap",
    2: "External VREF (unbuffered)",
    3: "External VREF (buffered)",
}

POWER_NAMES = {
    0: "Normal",
    1: "Power-down: 1 kOhm",
    2: "Power-down: 125 kOhm",
    3: "Power-down: high-Z",
}

LOCK_NAMES = {
    0: "Unlocked",
    1: "EEPROM locked",
    2: "Wiper/config locked",
    3: "Fully locked",
}

REGISTER_NAMES = {
    **{address: "Volatile DAC{}".format(address) for address in range(0x00, 0x08)},
    0x08: "Volatile VREF control",
    0x09: "Volatile power-down control",
    0x0A: "Volatile gain / status",
    0x0B: "WiperLock status (read-only)",
    **{
        address: "EEPROM DAC{}".format(address - 0x10)
        for address in range(0x10, 0x18)
    },
    0x18: "EEPROM VREF control",
    0x19: "EEPROM power-down control",
    0x1A: "EEPROM gain / I2C address",
}

DAC_MAX_CODE = 0x0FFF
DAC_STEPS = 4096


# ---------------------------------------------------------------------------
# Pure DAC calculations
# ---------------------------------------------------------------------------


def clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def output_scale(
    channel: int,
    vref_mode: int,
    gain: int,
    vdd: float,
    vref0: float,
    vref1: float,
    bandgap: float,
) -> float:
    """Return the ideal pre-clipping full-scale multiplier in volts."""
    if vref_mode == 0:
        # Gain=2 is not supported in VDD-reference mode.
        return vdd
    if vref_mode == 1:
        # The band-gap path first produces approximately 2*VBG.
        base = 2.0 * bandgap
    elif vref_mode in (2, 3):
        base = vref0 if channel % 2 == 0 else vref1
    else:
        raise ValueError("invalid VREF mode")
    return base * (2.0 if gain else 1.0)


def code_to_voltage(
    code: int,
    channel: int,
    vref_mode: int,
    gain: int,
    vdd: float,
    vref0: float,
    vref1: float,
    bandgap: float,
) -> float:
    code = min(max(int(code), 0), DAC_MAX_CODE)
    scale = output_scale(channel, vref_mode, gain, vdd, vref0, vref1, bandgap)
    return min(vdd, scale * code / DAC_STEPS)


def voltage_to_code(
    voltage: float,
    channel: int,
    vref_mode: int,
    gain: int,
    vdd: float,
    vref0: float,
    vref1: float,
    bandgap: float,
) -> int:
    scale = output_scale(channel, vref_mode, gain, vdd, vref0, vref1, bandgap)
    if scale <= 0:
        return 0
    voltage = clamp(float(voltage), 0.0, vdd)
    return min(max(int(voltage * DAC_STEPS / scale + 0.5), 0), DAC_MAX_CODE)


def build_raw_words(channels: list[dict[str, Any]], por: bool = False) -> dict[str, int]:
    vref = 0
    power_down = 0
    gain_status = 0x0080 if por else 0
    wiperlock = 0
    for item in channels:
        channel = int(item["channel"])
        vref |= (int(item["vref"]) & 0x03) << (2 * channel)
        power_down |= (int(item["power_down"]) & 0x03) << (2 * channel)
        gain_status |= (int(item["gain"]) & 0x01) << (8 + channel)
        wiperlock |= (int(item.get("wiperlock", 0)) & 0x03) << (2 * channel)
    return {
        "vref": vref,
        "power_down": power_down,
        "gain_status": gain_status,
        "wiperlock": wiperlock,
    }


# ---------------------------------------------------------------------------
# USB serial worker
# ---------------------------------------------------------------------------


class RemoteCommandError(Exception):
    pass


class SerialWorker(QThread):
    connected = Signal(object)
    disconnected = Signal(str)
    response = Signal(str, object)
    request_failed = Signal(str, str)
    log_line = Signal(str, str)

    def __init__(self, port: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.port = port
        self._commands: queue.Queue[tuple[str, str, dict[str, Any], float]] = queue.Queue()
        self._stop_event = threading.Event()
        self._request_id = 0
        self._serial: serial.Serial | None = None

    def submit(
        self,
        tag: str,
        command: str,
        parameters: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> None:
        self._commands.put((tag, command, parameters or {}, timeout))

    def stop(self) -> None:
        self._stop_event.set()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _transact(
        self,
        command: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        if self._serial is None:
            raise serial.SerialException("serial port is not open")

        request_id = self._next_id()
        request = {"id": request_id, "cmd": command}
        request.update(parameters)
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")

        self.log_line.emit("TX", encoded.decode("utf-8").strip())
        self._serial.write(encoded)
        self._serial.flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop_event.is_set():
            raw = self._serial.readline()
            if not raw:
                continue

            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            self.log_line.emit("RX", text)

            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                # MicroPython boot banners and REPL prompts are harmless.
                continue

            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                continue

            if not message.get("ok", False):
                error = message.get("error", {})
                if isinstance(error, dict):
                    detail = error.get("message", "unknown firmware error")
                    kind = error.get("type", "Error")
                    raise RemoteCommandError("{}: {}".format(kind, detail))
                raise RemoteCommandError(str(error))

            result = message.get("result", {})
            return result if isinstance(result, dict) else {"value": result}

        raise TimeoutError("no response to '{}' within {:.1f} s".format(command, timeout))

    def run(self) -> None:
        reason = "Disconnected"
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=115200,
                timeout=0.05,
                write_timeout=1.0,
            )

            # Give Windows and the USB CDC stack a moment after opening.
            time.sleep(0.15)
            info = self._transact("hello", {}, 3.0)

            if int(info.get("protocol_version", -1)) != PROTOCOL_VERSION:
                raise RemoteCommandError(
                    "protocol mismatch: application={}, firmware={}".format(
                        PROTOCOL_VERSION, info.get("protocol_version", "unknown")
                    )
                )

            self.connected.emit(info)

            while not self._stop_event.is_set():
                try:
                    tag, command, parameters, timeout = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue

                try:
                    result = self._transact(command, parameters, timeout)
                    self.response.emit(tag, result)
                except RemoteCommandError as error:
                    self.request_failed.emit(tag, str(error))
                except TimeoutError as error:
                    self.request_failed.emit(tag, str(error))
                finally:
                    self._commands.task_done()

            reason = "Disconnected by user"
        except Exception as error:
            reason = str(error)
        finally:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self.disconnected.emit(reason)


# ---------------------------------------------------------------------------
# Channel editor storage
# ---------------------------------------------------------------------------


@dataclass
class ChannelEditor:
    channel: int
    target_voltage: QDoubleSpinBox
    target_code: QSpinBox
    target_hex: QTableWidgetItem
    device_code: QTableWidgetItem
    calculated_voltage: QTableWidgetItem
    vref: QComboBox
    gain: QComboBox
    power_down: QComboBox
    lock: QTableWidgetItem
    apply_button: QPushButton
    dirty: bool = False
    syncing: bool = False


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.demo = demo
        self.settings = QSettings("OpenAI", "MCP47FEB28ControlConsole")
        self.worker: SerialWorker | None = None
        self.connected_flag = False
        self.dac_present = False
        self.pending_tags: set[str] = set()
        self.tag_counter = 0
        self.last_state: dict[str, Any] | None = None
        self.last_eeprom: dict[str, Any] | None = None
        self.device_info: dict[str, Any] = {}
        self.channel_editors: list[ChannelEditor] = []
        self.info_labels: dict[str, QLabel] = {}

        self.demo_state = self._make_demo_state()
        self.demo_eeprom = self._make_demo_eeprom()

        self.setWindowTitle("{} v{}".format(APP_NAME, APP_VERSION))
        self.resize(1500, 900)
        self.setMinimumSize(1120, 720)

        self._build_ui()
        self._apply_stylesheet()
        self._restore_settings()
        self.refresh_ports()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_tick)
        self._update_poll_timer()

        if self.demo:
            QTimer.singleShot(100, self._connect_demo)

    # ----- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        root.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_channels_tab(), "Channels")
        self.tabs.addTab(self._build_device_tab(), "Device && EEPROM")
        self.tabs.addTab(self._build_log_tab(), "Protocol log")
        root.addWidget(self.tabs, 1)

        # All tab-owned controls now exist, so their initial state can be set
        # in one place.
        self._set_dac_controls_enabled(False)

        self.setCentralWidget(central)

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self.status_message = QLabel("Ready")
        status_bar.addWidget(self.status_message, 1)
        self.poll_status = QLabel("Polling stopped")
        status_bar.addPermanentWidget(self.poll_status)

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("headerFrame")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 13, 14, 13)
        layout.setSpacing(10)

        badge = QLabel("DAC")
        badge.setObjectName("appBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(48, 48)
        layout.addWidget(badge)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("Tiny 2040 USB bridge  |  8 channels  |  12-bit")
        subtitle.setObjectName("mutedText")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box)
        layout.addStretch(1)

        layout.addWidget(QLabel("USB port"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(225)
        layout.addWidget(self.port_combo)

        self.refresh_ports_button = QPushButton("Refresh")
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        layout.addWidget(self.refresh_ports_button)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self.toggle_connection)
        layout.addWidget(self.connect_button)

        self.connection_badge = QLabel("Disconnected")
        self.connection_badge.setObjectName("statusOff")
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_badge.setMinimumWidth(118)
        layout.addWidget(self.connection_badge)
        return frame

    def _build_channels_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 12, 2, 2)
        layout.setSpacing(12)

        settings_row = QHBoxLayout()
        settings_row.setSpacing(12)

        voltage_group = QGroupBox("Voltage calculation")
        voltage_layout = QGridLayout(voltage_group)
        self.vdd_spin = self._voltage_spin(3.300)
        self.vref0_spin = self._voltage_spin(3.300)
        self.vref1_spin = self._voltage_spin(3.300)
        self.bandgap_spin = self._voltage_spin(1.220)
        voltage_layout.addWidget(QLabel("DAC VDD"), 0, 0)
        voltage_layout.addWidget(self.vdd_spin, 0, 1)
        voltage_layout.addWidget(QLabel("External VREF0"), 0, 2)
        voltage_layout.addWidget(self.vref0_spin, 0, 3)
        voltage_layout.addWidget(QLabel("External VREF1"), 1, 0)
        voltage_layout.addWidget(self.vref1_spin, 1, 1)
        voltage_layout.addWidget(QLabel("Band-gap nominal"), 1, 2)
        voltage_layout.addWidget(self.bandgap_spin, 1, 3)
        settings_row.addWidget(voltage_group, 3)

        poll_group = QGroupBox("Live register readback")
        poll_layout = QGridLayout(poll_group)
        self.auto_poll_check = QCheckBox("Poll automatically")
        self.auto_poll_check.setChecked(True)
        self.auto_poll_check.toggled.connect(self._update_poll_timer)
        self.poll_interval_spin = QSpinBox()
        self.poll_interval_spin.setRange(100, 10_000)
        self.poll_interval_spin.setSingleStep(100)
        self.poll_interval_spin.setValue(500)
        self.poll_interval_spin.setSuffix(" ms")
        self.poll_interval_spin.valueChanged.connect(self._update_poll_timer)
        self.read_now_button = QPushButton("Read now")
        self.read_now_button.clicked.connect(self.request_state)
        poll_layout.addWidget(self.auto_poll_check, 0, 0, 1, 2)
        poll_layout.addWidget(QLabel("Interval"), 1, 0)
        poll_layout.addWidget(self.poll_interval_spin, 1, 1)
        poll_layout.addWidget(self.read_now_button, 2, 0, 1, 2)
        settings_row.addWidget(poll_group, 2)

        actions_group = QGroupBox("Output actions")
        actions_layout = QGridLayout(actions_group)
        self.apply_all_button = QPushButton("Apply all channels")
        self.apply_all_button.setObjectName("primaryButton")
        self.apply_all_button.clicked.connect(self.apply_all)
        self.zero_all_button = QPushButton("Set all to zero")
        self.zero_all_button.setObjectName("dangerButton")
        self.zero_all_button.clicked.connect(self.zero_all)
        actions_layout.addWidget(self.apply_all_button, 0, 0)
        actions_layout.addWidget(self.zero_all_button, 1, 0)
        settings_row.addWidget(actions_group, 2)
        layout.addLayout(settings_row)

        note = QLabel(
            "Live values are DAC register readbacks. Calculated VOUT is theoretical; "
            "the RP2040 is not measuring the analog pins."
        )
        note.setObjectName("infoBanner")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.channel_table = QTableWidget(8, 14)
        self.channel_table.setHorizontalHeaderLabels(
            [
                "Net",
                "DAC / pin",
                "Bank",
                "Target V",
                "Target code",
                "Target hex",
                "Device code",
                "Calculated VOUT",
                "Reference source",
                "Gain",
                "Output mode",
                "WiperLock",
                "Apply",
                "State",
            ]
        )
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setAlternatingRowColors(True)
        self.channel_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.channel_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.channel_table.setShowGrid(False)
        self.channel_table.setWordWrap(False)
        self.channel_table.setMinimumHeight(390)

        header = self.channel_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(10, QHeaderView.ResizeMode.Stretch)

        for channel in range(8):
            self._create_channel_row(channel)
            self.channel_table.setRowHeight(channel, 48)

        layout.addWidget(self.channel_table, 1)
        return page

    def _build_device_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 12, 2, 2)
        layout.setSpacing(12)

        upper = QHBoxLayout()
        upper.setSpacing(12)

        info_group = QGroupBox("Connected device")
        form = QFormLayout(info_group)
        info_fields = [
            ("firmware", "Firmware"),
            ("firmware_version", "Firmware version"),
            ("rp2040_uid", "RP2040 unique ID"),
            ("device", "DAC model"),
            ("dac_address", "I2C address"),
            ("i2c_bus", "I2C bus"),
            ("pins", "SDA / SCL"),
            ("i2c_frequency", "I2C frequency"),
            ("uptime", "Firmware uptime"),
            ("por", "POR observed"),
            ("eeprom_busy", "EEPROM busy"),
            ("lat_mode", "LAT mode"),
        ]
        for key, label_text in info_fields:
            value = QLabel("--")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.info_labels[key] = value
            form.addRow(label_text, value)
        upper.addWidget(info_group, 3)

        raw_group = QGroupBox("Raw register inspector")
        raw_layout = QGridLayout(raw_group)
        self.raw_register_combo = QComboBox()
        for address, name in sorted(REGISTER_NAMES.items()):
            self.raw_register_combo.addItem("0x{:02X}  {}".format(address, name), address)
        self.raw_value_edit = QLineEdit("0x0000")
        self.raw_value_edit.setPlaceholderText("0x0000")
        self.raw_read_button = QPushButton("Read register")
        self.raw_read_button.clicked.connect(self.raw_read)
        self.raw_write_button = QPushButton("Write volatile register")
        self.raw_write_button.clicked.connect(self.raw_write)
        raw_note = QLabel(
            "Raw writes are restricted by firmware to volatile addresses 0x00-0x0A. "
            "Use the EEPROM controls below for nonvolatile settings."
        )
        raw_note.setObjectName("mutedText")
        raw_note.setWordWrap(True)
        raw_layout.addWidget(QLabel("Register"), 0, 0)
        raw_layout.addWidget(self.raw_register_combo, 0, 1)
        raw_layout.addWidget(QLabel("Value"), 1, 0)
        raw_layout.addWidget(self.raw_value_edit, 1, 1)
        raw_layout.addWidget(self.raw_read_button, 2, 0)
        raw_layout.addWidget(self.raw_write_button, 2, 1)
        raw_layout.addWidget(raw_note, 3, 0, 1, 2)
        upper.addWidget(raw_group, 4)
        layout.addLayout(upper)

        eeprom_group = QGroupBox("Power-on settings stored in DAC EEPROM")
        eeprom_layout = QVBoxLayout(eeprom_group)
        warning = QLabel(
            "Saving changes the voltages and operating modes loaded at the next power-up. "
            "Unchanged registers are skipped to avoid unnecessary EEPROM wear."
        )
        warning.setObjectName("warningBanner")
        warning.setWordWrap(True)
        eeprom_layout.addWidget(warning)

        button_row = QHBoxLayout()
        self.refresh_eeprom_button = QPushButton("Refresh EEPROM")
        self.refresh_eeprom_button.clicked.connect(self.request_eeprom)
        self.save_eeprom_button = QPushButton("Save current state to EEPROM")
        self.save_eeprom_button.setObjectName("dangerButton")
        self.save_eeprom_button.clicked.connect(self.save_eeprom)
        self.load_eeprom_button = QPushButton("Load EEPROM into outputs")
        self.load_eeprom_button.clicked.connect(self.load_eeprom)
        self.eeprom_address_label = QLabel("Stored address: --")
        self.eeprom_address_label.setObjectName("mutedText")
        button_row.addWidget(self.refresh_eeprom_button)
        button_row.addWidget(self.save_eeprom_button)
        button_row.addWidget(self.load_eeprom_button)
        button_row.addStretch(1)
        button_row.addWidget(self.eeprom_address_label)
        eeprom_layout.addLayout(button_row)

        self.eeprom_table = QTableWidget(8, 8)
        self.eeprom_table.setHorizontalHeaderLabels(
            ["Net", "DAC", "Code", "Hex", "Calculated VOUT", "Reference", "Gain", "Power mode"]
        )
        self.eeprom_table.verticalHeader().setVisible(False)
        self.eeprom_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.eeprom_table.setAlternatingRowColors(True)
        self.eeprom_table.setShowGrid(False)
        self.eeprom_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        eeprom_layout.addWidget(self.eeprom_table)
        layout.addWidget(eeprom_group, 1)
        return page

    def _build_log_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 12, 2, 2)

        action_row = QHBoxLayout()
        description = QLabel("USB protocol traffic and application events")
        description.setObjectName("mutedText")
        clear_button = QPushButton("Clear log")
        clear_button.clicked.connect(lambda: self.log_view.clear())
        action_row.addWidget(description)
        action_row.addStretch(1)
        action_row.addWidget(clear_button)
        layout.addLayout(action_row)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_view, 1)
        return page

    def _voltage_spin(self, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.001, 5.500)
        spin.setDecimals(4)
        spin.setSingleStep(0.001)
        spin.setSuffix(" V")
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self._calibration_changed)
        return spin

    def _create_channel_row(self, channel: int) -> None:
        table = self.channel_table
        net_item = QTableWidgetItem(CHANNEL_NAMES[channel])
        net_item.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        table.setItem(channel, 0, net_item)
        table.setItem(channel, 1, QTableWidgetItem("DAC{} / VOUT{}".format(channel, channel)))
        table.setItem(channel, 2, QTableWidgetItem("LAT{} / VREF{}".format(channel % 2, channel % 2)))

        target_voltage = QDoubleSpinBox()
        target_voltage.setRange(0.0, 5.5)
        target_voltage.setDecimals(6)
        target_voltage.setSingleStep(0.001)
        target_voltage.setSuffix(" V")
        target_voltage.setKeyboardTracking(False)
        table.setCellWidget(channel, 3, target_voltage)

        target_code = QSpinBox()
        target_code.setRange(0, DAC_MAX_CODE)
        target_code.setKeyboardTracking(False)
        table.setCellWidget(channel, 4, target_code)

        target_hex = QTableWidgetItem("0x0000")
        target_hex.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(channel, 5, target_hex)

        device_code = QTableWidgetItem("--")
        device_code.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(channel, 6, device_code)

        calculated = QTableWidgetItem("--")
        calculated.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(channel, 7, calculated)

        vref = QComboBox()
        for value in range(4):
            vref.addItem(VREF_NAMES[value], value)
        vref.setToolTip(
            "VDD=00, internal band gap=01, external unbuffered=10, "
            "external buffered=11. Do not externally drive VREF in band-gap mode."
        )
        table.setCellWidget(channel, 8, vref)

        gain = QComboBox()
        gain.addItem("1x", 0)
        gain.addItem("2x", 1)
        gain.setToolTip("The application enforces 1x when VDD is the reference.")
        table.setCellWidget(channel, 9, gain)

        power = QComboBox()
        for value in range(4):
            power.addItem(POWER_NAMES[value], value)
        power.setToolTip(
            "Normal drive, 1 kOhm pull-down, 125 kOhm pull-down, or high impedance."
        )
        table.setCellWidget(channel, 10, power)

        lock = QTableWidgetItem("--")
        lock.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(channel, 11, lock)

        apply_button = QPushButton("Apply")
        apply_button.setProperty("channel", channel)
        apply_button.clicked.connect(lambda checked=False, ch=channel: self.apply_channel(ch))
        table.setCellWidget(channel, 12, apply_button)

        state_item = QTableWidgetItem("Not read")
        state_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(channel, 13, state_item)

        editor = ChannelEditor(
            channel=channel,
            target_voltage=target_voltage,
            target_code=target_code,
            target_hex=target_hex,
            device_code=device_code,
            calculated_voltage=calculated,
            vref=vref,
            gain=gain,
            power_down=power,
            lock=lock,
            apply_button=apply_button,
        )
        self.channel_editors.append(editor)

        target_voltage.valueChanged.connect(
            lambda value, ch=channel: self._target_voltage_changed(ch, value)
        )
        target_code.valueChanged.connect(
            lambda value, ch=channel: self._target_code_changed(ch, value)
        )
        # With keyboard tracking disabled, valueChanged is emitted only after
        # Enter/focus-out.  Mark the row dirty on the first typed character so
        # the periodic register readback cannot overwrite an edit in progress.
        target_voltage.lineEdit().textEdited.connect(
            lambda _text, ch=channel: self._channel_edit_started(ch)
        )
        target_code.lineEdit().textEdited.connect(
            lambda _text, ch=channel: self._channel_edit_started(ch)
        )
        vref.currentIndexChanged.connect(lambda index, ch=channel: self._config_changed(ch))
        gain.currentIndexChanged.connect(lambda index, ch=channel: self._config_changed(ch))
        power.currentIndexChanged.connect(lambda index, ch=channel: self._config_changed(ch))

    # ----- Presentation ----------------------------------------------------

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #10151d;
                color: #e8edf5;
                font-family: "Segoe UI", Arial, sans-serif;
                font-size: 10pt;
            }
            QFrame#headerFrame, QGroupBox {
                background: #18212d;
                border: 1px solid #2a3849;
                border-radius: 9px;
            }
            QGroupBox {
                margin-top: 10px;
                padding: 13px 10px 9px 10px;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #b9c9dc;
            }
            QLabel#appBadge {
                background: #2f80ed;
                border-radius: 12px;
                color: white;
                font-size: 13pt;
                font-weight: 700;
            }
            QLabel#appTitle {
                font-size: 17pt;
                font-weight: 650;
                color: white;
            }
            QLabel#mutedText { color: #91a1b5; }
            QLabel#infoBanner {
                background: #132c3b;
                color: #b8e3ff;
                border: 1px solid #24506a;
                border-radius: 7px;
                padding: 8px 12px;
            }
            QLabel#warningBanner {
                background: #382d16;
                color: #ffe0a3;
                border: 1px solid #6d5524;
                border-radius: 7px;
                padding: 8px 12px;
            }
            QLabel#statusOff, QLabel#statusOn, QLabel#statusWarn {
                border-radius: 11px;
                padding: 7px 12px;
                font-weight: 600;
            }
            QLabel#statusOff { background: #2a3039; color: #aeb8c5; }
            QLabel#statusOn { background: #173b2b; color: #77e0a8; border: 1px solid #28664a; }
            QLabel#statusWarn { background: #44351d; color: #ffd88b; border: 1px solid #735a2a; }
            QPushButton {
                background: #263244;
                border: 1px solid #3b4b60;
                border-radius: 6px;
                padding: 7px 12px;
                min-height: 18px;
            }
            QPushButton:hover { background: #304058; border-color: #537098; }
            QPushButton:pressed { background: #1d2837; }
            QPushButton:disabled { color: #657285; background: #1c2430; border-color: #283442; }
            QPushButton#primaryButton { background: #2371d1; border-color: #3989eb; color: white; }
            QPushButton#primaryButton:hover { background: #2d82ea; }
            QPushButton#dangerButton { background: #5a2a2e; border-color: #874047; color: #ffd9dc; }
            QPushButton#dangerButton:hover { background: #71343a; }
            QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
                background: #111923;
                border: 1px solid #344458;
                border-radius: 5px;
                padding: 5px 7px;
                min-height: 20px;
                selection-background-color: #2f80ed;
            }
            QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {
                border-color: #438fe9;
            }
            QComboBox::drop-down { border: none; width: 20px; }
            QTableWidget {
                background: #131b25;
                alternate-background-color: #17212d;
                border: 1px solid #2a3849;
                border-radius: 8px;
                selection-background-color: #244f7f;
                gridline-color: transparent;
            }
            QHeaderView::section {
                background: #202c3b;
                color: #b9c8da;
                border: none;
                border-right: 1px solid #2d3a49;
                padding: 8px;
                font-weight: 600;
            }
            QTabWidget::pane { border: 1px solid #293746; border-radius: 8px; top: -1px; }
            QTabBar::tab {
                background: #18212d;
                color: #9eacbd;
                border: 1px solid #293746;
                border-bottom: none;
                padding: 9px 18px;
                margin-right: 3px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }
            QTabBar::tab:selected { background: #233146; color: white; }
            QTextEdit {
                background: #0b1017;
                color: #b7c8da;
                border: 1px solid #2a3849;
                border-radius: 7px;
                padding: 8px;
            }
            QStatusBar { background: #0d131b; color: #8fa0b5; }
            QToolTip { background: #202b39; color: white; border: 1px solid #465970; }
            """
        )

    def append_log(self, direction: str, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        colors = {"TX": "#76b7ff", "RX": "#8ee3ad", "ERR": "#ff8f98", "APP": "#d2b4ff"}
        color = colors.get(direction, "#b7c8da")
        escaped = (
            message.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        self.log_view.append(
            '<span style="color:#718196">{}</span> '
            '<b style="color:{}">{:>3}</b> {}'.format(timestamp, color, direction, escaped)
        )

    # ----- Settings and ports ---------------------------------------------

    def _restore_settings(self) -> None:
        self.vdd_spin.setValue(float(self.settings.value("vdd", 3.300)))
        self.vref0_spin.setValue(float(self.settings.value("vref0", 3.300)))
        self.vref1_spin.setValue(float(self.settings.value("vref1", 3.300)))
        self.bandgap_spin.setValue(float(self.settings.value("bandgap", 1.220)))
        self.poll_interval_spin.setValue(int(self.settings.value("poll_interval", 500)))
        self.auto_poll_check.setChecked(
            str(self.settings.value("auto_poll", "true")).lower() == "true"
        )
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        self.settings.setValue("vdd", self.vdd_spin.value())
        self.settings.setValue("vref0", self.vref0_spin.value())
        self.settings.setValue("vref1", self.vref1_spin.value())
        self.settings.setValue("bandgap", self.bandgap_spin.value())
        self.settings.setValue("poll_interval", self.poll_interval_spin.value())
        self.settings.setValue("auto_poll", self.auto_poll_check.isChecked())
        self.settings.setValue("geometry", self.saveGeometry())
        if self.port_combo.currentData():
            self.settings.setValue("last_port", self.port_combo.currentData())

    def refresh_ports(self) -> None:
        if self.demo:
            self.port_combo.clear()
            self.port_combo.addItem("DEMO  Simulated Tiny 2040", "DEMO")
            return

        previous = self.port_combo.currentData() or self.settings.value("last_port", "")
        ports = list(list_ports.comports())
        self.port_combo.clear()

        best_index = -1
        for index, port in enumerate(ports):
            description = port.description or "Serial device"
            self.port_combo.addItem("{}  {}".format(port.device, description), port.device)
            if port.device == previous:
                best_index = index
            elif best_index < 0 and (
                port.vid == 0x2E8A
                or "Pico" in description
                or "RP2040" in description
                or "MicroPython" in description
            ):
                best_index = index

        if best_index >= 0:
            self.port_combo.setCurrentIndex(best_index)
        self.status_message.setText("Found {} serial port(s)".format(len(ports)))

    # ----- Connection and request routing ---------------------------------

    def toggle_connection(self) -> None:
        if self.connected_flag or self.worker is not None:
            self.disconnect_device()
            return
        if self.demo:
            self._connect_demo()
            return

        port = self.port_combo.currentData()
        if not port:
            QMessageBox.information(self, "No serial port", "Connect the Tiny 2040 and refresh the port list.")
            return

        self.connection_badge.setText("Connecting...")
        self.connection_badge.setObjectName("statusWarn")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.connect_button.setEnabled(False)
        self.status_message.setText("Opening {}...".format(port))

        self.worker = SerialWorker(str(port), self)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.response.connect(self._handle_response)
        self.worker.request_failed.connect(self._handle_request_failure)
        self.worker.log_line.connect(self.append_log)
        self.worker.start()

    def _connect_demo(self) -> None:
        if self.connected_flag:
            return
        info = {
            "firmware": "Tiny2040 MCP47FEB28 USB Controller (demo)",
            "firmware_version": "1.0.0-demo",
            "protocol_version": 1,
            "device": "MCP47FEB28",
            "resolution_bits": 12,
            "channels": 8,
            "dac_address": 0x60,
            "dac_present": True,
            "i2c_devices": [0x60],
            "i2c_bus": 0,
            "i2c_frequency": 100_000,
            "sda_pin": 0,
            "scl_pin": 1,
            "rp2040_uid": "DEMO2040A1B2C3D4",
            "lat_mode": "external; expected LOW for immediate update",
        }
        self._on_connected(info)

    def disconnect_device(self) -> None:
        if self.demo:
            self._on_disconnected("Disconnected by user")
            return
        if self.worker is not None:
            self.status_message.setText("Disconnecting...")
            self.worker.stop()
            self.worker.wait(1500)

    def _on_connected(self, info: object) -> None:
        self.device_info = dict(info) if isinstance(info, dict) else {}
        self.connected_flag = True
        self.dac_present = bool(self.device_info.get("dac_present", False))
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Disconnect")
        self.refresh_ports_button.setEnabled(False)
        self.port_combo.setEnabled(False)

        if self.dac_present:
            self.connection_badge.setText("Connected")
            self.connection_badge.setObjectName("statusOn")
            self.status_message.setText("Tiny 2040 and MCP47FEB28 are online")
        else:
            self.connection_badge.setText("DAC missing")
            self.connection_badge.setObjectName("statusWarn")
            self.status_message.setText("Tiny 2040 connected, but DAC 0x60 was not found")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self._set_dac_controls_enabled(self.dac_present)
        self._update_device_info()
        self.append_log("APP", "Connected; DAC present={}".format(self.dac_present))

        if self.dac_present:
            self.request_state()
            self.request_eeprom()
        self._update_poll_timer()

    def _on_disconnected(self, reason: str) -> None:
        was_connected = self.connected_flag
        self.connected_flag = False
        self.dac_present = False
        self.pending_tags.clear()
        self.worker = None
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Connect")
        self.refresh_ports_button.setEnabled(True)
        self.port_combo.setEnabled(True)
        self.connection_badge.setText("Disconnected")
        self.connection_badge.setObjectName("statusOff")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self._set_dac_controls_enabled(False)
        self._update_poll_timer()
        self.status_message.setText(reason)
        if was_connected or reason != "Disconnected":
            self.append_log("APP", reason)

    def _new_tag(self, prefix: str) -> str:
        self.tag_counter += 1
        return "{}:{}".format(prefix, self.tag_counter)

    def send_command(
        self,
        prefix: str,
        command: str,
        parameters: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> str | None:
        if not self.connected_flag or not self.dac_present:
            self.status_message.setText("Connect to the DAC first")
            return None
        tag = self._new_tag(prefix)
        self.pending_tags.add(tag)

        if self.demo:
            self.append_log("TX", json.dumps({"cmd": command, **(parameters or {})}))
            QTimer.singleShot(
                80,
                lambda: self._run_demo_command(tag, command, parameters or {}),
            )
        elif self.worker is not None:
            self.worker.submit(tag, command, parameters or {}, timeout)
        return tag

    def _handle_response(self, tag: str, result: object) -> None:
        self.pending_tags.discard(tag)
        data = dict(result) if isinstance(result, dict) else {}
        prefix = tag.split(":", 1)[0]

        if prefix == "state":
            self._apply_state(data)
        elif prefix == "eeprom":
            self._apply_eeprom(data)
        elif prefix == "channel":
            channel = int(tag.split(":")[1]) if tag.count(":") > 1 else -1
            if 0 <= channel < 8:
                self._set_row_dirty(channel, False)
            if "state" in data:
                self._apply_state(data["state"])
            self.status_message.setText("Channel {} applied".format(channel))
        elif prefix in ("apply", "zero", "load", "raw_write"):
            if "state" in data:
                if prefix in ("apply", "zero", "load"):
                    for channel in range(8):
                        self._set_row_dirty(channel, False)
                self._apply_state(data["state"])
            if "eeprom" in data:
                self._apply_eeprom(data["eeprom"])
            self.status_message.setText("Command completed")
        elif prefix == "save":
            if "state" in data:
                self._apply_state(data["state"])
            if "eeprom" in data:
                self._apply_eeprom(data["eeprom"])
            self.status_message.setText(
                "EEPROM saved; {} register(s) changed".format(data.get("write_count", 0))
            )
        elif prefix == "raw_read":
            value = int(data.get("value", 0))
            self.raw_value_edit.setText("0x{:04X}".format(value))
            self.status_message.setText(
                "Register 0x{:02X} = 0x{:04X}".format(int(data.get("register", 0)), value)
            )

        self._update_poll_status()

    def _handle_request_failure(self, tag: str, message: str) -> None:
        self.pending_tags.discard(tag)
        self.status_message.setText(message)
        self.append_log("ERR", "{}: {}".format(tag, message))
        self._update_poll_status()

    # ----- Demo backend ----------------------------------------------------

    def _make_demo_state(self) -> dict[str, Any]:
        codes = [0x04D9, 0x0200, 0x0000, 0x0800, 0x0100, 0x0000, 0x0C00, 0x0000]
        channels = []
        for channel in range(8):
            channels.append(
                {
                    "channel": channel,
                    "name": CHANNEL_NAMES[channel],
                    "code": codes[channel],
                    "vref": 0,
                    "gain": 0,
                    "power_down": 0,
                    "wiperlock": 0,
                }
            )
        return {
            "channels": channels,
            "status": {"por_seen": True, "eeprom_busy": False},
            "raw": build_raw_words(channels, por=True),
            "uptime_ms": 12_345,
        }

    def _make_demo_eeprom(self) -> dict[str, Any]:
        channels = copy.deepcopy(self.demo_state["channels"])
        for item in channels:
            item.pop("wiperlock", None)
        raw = build_raw_words(channels)
        return {
            "channels": channels,
            "address": 0x60,
            "address_locked": False,
            "raw": {
                "dac": [item["code"] for item in channels],
                "vref": raw["vref"],
                "power_down": raw["power_down"],
                "gain_address": (raw["gain_status"] & 0xFF00) | 0x60,
            },
        }

    def _refresh_demo_raw(self) -> None:
        por = bool(self.demo_state.get("status", {}).get("por_seen", False))
        self.demo_state["raw"] = build_raw_words(self.demo_state["channels"], por=por)
        self.demo_state["uptime_ms"] = int(self.demo_state.get("uptime_ms", 0)) + 80

    def _run_demo_command(self, tag: str, command: str, parameters: dict[str, Any]) -> None:
        try:
            result: dict[str, Any]
            if command == "get_state":
                self._refresh_demo_raw()
                result = copy.deepcopy(self.demo_state)
            elif command == "get_eeprom":
                result = copy.deepcopy(self.demo_eeprom)
            elif command == "set_channel":
                channel = int(parameters["channel"])
                item = self.demo_state["channels"][channel]
                item["code"] = int(parameters["code"])
                for key in ("vref", "gain", "power_down"):
                    if key in parameters:
                        item[key] = int(parameters[key])
                self._refresh_demo_raw()
                result = {"state": copy.deepcopy(self.demo_state)}
            elif command == "apply_state":
                for channel, item in enumerate(self.demo_state["channels"]):
                    item["code"] = int(parameters["codes"][channel])
                    item["vref"] = int(parameters["vrefs"][channel])
                    item["gain"] = int(parameters["gains"][channel])
                    item["power_down"] = int(parameters["power_downs"][channel])
                self._refresh_demo_raw()
                result = {"state": copy.deepcopy(self.demo_state)}
            elif command == "zero_all":
                for item in self.demo_state["channels"]:
                    item["code"] = 0
                self._refresh_demo_raw()
                result = {"state": copy.deepcopy(self.demo_state)}
            elif command == "save_eeprom":
                self.demo_eeprom = self._make_demo_eeprom()
                result = {
                    "write_count": 0,
                    "changed_registers": [],
                    "state": copy.deepcopy(self.demo_state),
                    "eeprom": copy.deepcopy(self.demo_eeprom),
                }
            elif command == "load_eeprom":
                for channel, source in enumerate(self.demo_eeprom["channels"]):
                    target = self.demo_state["channels"][channel]
                    for key in ("code", "vref", "gain", "power_down"):
                        target[key] = source[key]
                self._refresh_demo_raw()
                result = {
                    "state": copy.deepcopy(self.demo_state),
                    "eeprom": copy.deepcopy(self.demo_eeprom),
                }
            elif command == "raw_read":
                register = int(parameters["register"])
                values = self._demo_register_values()
                result = {"register": register, "value": values.get(register, 0)}
            elif command == "raw_write":
                result = {"state": copy.deepcopy(self.demo_state)}
            else:
                raise ValueError("demo command is not implemented")

            self.append_log("RX", json.dumps({"ok": True, "result": result}))
            self._handle_response(tag, result)
        except Exception as error:
            self._handle_request_failure(tag, str(error))

    def _demo_register_values(self) -> dict[int, int]:
        values = {channel: item["code"] for channel, item in enumerate(self.demo_state["channels"])}
        values[0x08] = self.demo_state["raw"]["vref"]
        values[0x09] = self.demo_state["raw"]["power_down"]
        values[0x0A] = self.demo_state["raw"]["gain_status"]
        values[0x0B] = self.demo_state["raw"]["wiperlock"]
        for channel, item in enumerate(self.demo_eeprom["channels"]):
            values[0x10 + channel] = item["code"]
        values[0x18] = self.demo_eeprom["raw"]["vref"]
        values[0x19] = self.demo_eeprom["raw"]["power_down"]
        values[0x1A] = self.demo_eeprom["raw"]["gain_address"]
        return values

    # ----- Polling ---------------------------------------------------------

    def _update_poll_timer(self) -> None:
        if not hasattr(self, "poll_timer"):
            return
        self.poll_timer.setInterval(self.poll_interval_spin.value())
        if self.auto_poll_check.isChecked() and self.connected_flag and self.dac_present:
            self.poll_timer.start()
        else:
            self.poll_timer.stop()
        self._update_poll_status()

    def _update_poll_status(self) -> None:
        if self.poll_timer.isActive():
            text = "Polling every {} ms".format(self.poll_interval_spin.value())
            if self.pending_tags:
                text += "  |  command pending"
        else:
            text = "Polling stopped"
        self.poll_status.setText(text)

    def _poll_tick(self) -> None:
        if self.pending_tags:
            return
        self.request_state()

    def request_state(self) -> None:
        self.send_command("state", "get_state")

    def request_eeprom(self) -> None:
        self.send_command("eeprom", "get_eeprom")

    # ----- Channel editing -------------------------------------------------

    def _channel_edit_started(self, channel: int) -> None:
        """Protect an in-progress numeric edit from automatic readback."""
        editor = self.channel_editors[channel]
        if not editor.syncing:
            self._set_row_dirty(channel, True)

    @staticmethod
    def _channel_value_editor_has_focus(editor: ChannelEditor) -> bool:
        """Return True while either target-value editor is being typed in."""
        return any(
            widget.hasFocus() or widget.lineEdit().hasFocus()
            for widget in (editor.target_voltage, editor.target_code)
        )

    def _calibration(self) -> tuple[float, float, float, float]:
        return (
            self.vdd_spin.value(),
            self.vref0_spin.value(),
            self.vref1_spin.value(),
            self.bandgap_spin.value(),
        )

    def _combo_value(self, combo: QComboBox) -> int:
        value = combo.currentData()
        return int(value if value is not None else combo.currentIndex())

    def _target_voltage_changed(self, channel: int, voltage: float) -> None:
        editor = self.channel_editors[channel]
        if editor.syncing:
            return
        editor.syncing = True
        try:
            vdd, vref0, vref1, bandgap = self._calibration()
            code = voltage_to_code(
                voltage,
                channel,
                self._combo_value(editor.vref),
                self._combo_value(editor.gain),
                vdd,
                vref0,
                vref1,
                bandgap,
            )
            editor.target_code.setValue(code)
            editor.target_hex.setText("0x{:04X}".format(code))
        finally:
            editor.syncing = False
        self._set_row_dirty(channel, True)

    def _target_code_changed(self, channel: int, code: int) -> None:
        editor = self.channel_editors[channel]
        if editor.syncing:
            return
        editor.syncing = True
        try:
            vdd, vref0, vref1, bandgap = self._calibration()
            voltage = code_to_voltage(
                code,
                channel,
                self._combo_value(editor.vref),
                self._combo_value(editor.gain),
                vdd,
                vref0,
                vref1,
                bandgap,
            )
            editor.target_voltage.setValue(voltage)
            editor.target_hex.setText("0x{:04X}".format(code))
        finally:
            editor.syncing = False
        self._set_row_dirty(channel, True)

    def _config_changed(self, channel: int) -> None:
        editor = self.channel_editors[channel]
        if editor.syncing:
            return

        # VDD reference does not support 2x gain. Correct it immediately.
        if self._combo_value(editor.vref) == 0 and self._combo_value(editor.gain) == 1:
            editor.syncing = True
            editor.gain.setCurrentIndex(editor.gain.findData(0))
            editor.syncing = False

        # Preserve the requested voltage and calculate a code for the new mode.
        self._target_voltage_changed(channel, editor.target_voltage.value())

    def _set_row_dirty(self, channel: int, dirty: bool) -> None:
        editor = self.channel_editors[channel]
        editor.dirty = dirty
        editor.apply_button.setText("Apply *" if dirty else "Apply")
        state_item = self.channel_table.item(channel, 13)
        if dirty:
            state_item.setText("Modified")
            state_item.setForeground(QColor("#ffd079"))
        else:
            state_item.setText("Applied")
            state_item.setForeground(QColor("#82e3aa"))

    def apply_channel(self, channel: int) -> None:
        editor = self.channel_editors[channel]
        parameters = {
            "channel": channel,
            "code": editor.target_code.value(),
            "vref": self._combo_value(editor.vref),
            "gain": self._combo_value(editor.gain),
            "power_down": self._combo_value(editor.power_down),
        }
        tag = self.send_command("channel:{}".format(channel), "set_channel", parameters)
        if tag:
            self.status_message.setText("Applying {}...".format(CHANNEL_NAMES[channel]))

    def apply_all(self) -> None:
        parameters = {
            "codes": [editor.target_code.value() for editor in self.channel_editors],
            "vrefs": [self._combo_value(editor.vref) for editor in self.channel_editors],
            "gains": [self._combo_value(editor.gain) for editor in self.channel_editors],
            "power_downs": [
                self._combo_value(editor.power_down) for editor in self.channel_editors
            ],
        }
        if self.send_command("apply", "apply_state", parameters):
            self.status_message.setText("Applying all eight channels...")

    def zero_all(self) -> None:
        for editor in self.channel_editors:
            editor.target_voltage.setValue(0.0)
            editor.target_code.setValue(0)
            editor.target_hex.setText("0x0000")
        if self.send_command("zero", "zero_all"):
            self.status_message.setText("Setting all outputs to zero...")

    def _calibration_changed(self) -> None:
        if not hasattr(self, "channel_editors"):
            return
        for editor in self.channel_editors:
            if editor.dirty:
                self._target_voltage_changed(editor.channel, editor.target_voltage.value())
        if self.last_state:
            self._apply_state(self.last_state, preserve_dirty=True)
        if self.last_eeprom:
            self._apply_eeprom(self.last_eeprom)

    # ----- State display ---------------------------------------------------

    def _apply_state(self, state: dict[str, Any], preserve_dirty: bool = True) -> None:
        self.last_state = copy.deepcopy(state)
        channels = state.get("channels", [])
        if len(channels) != 8:
            self.append_log("ERR", "Firmware returned an invalid channel count")
            return

        vdd, vref0, vref1, bandgap = self._calibration()
        for item in channels:
            channel = int(item["channel"])
            editor = self.channel_editors[channel]
            code = int(item["code"])
            vref = int(item["vref"])
            gain = int(item["gain"])
            power = int(item["power_down"])
            lock = int(item.get("wiperlock", 0))

            editor.device_code.setText("{} / 0x{:04X}".format(code, code))
            if power == 0:
                volts = code_to_voltage(
                    code, channel, vref, gain, vdd, vref0, vref1, bandgap
                )
                editor.calculated_voltage.setText("{:.6f} V".format(volts))
            else:
                editor.calculated_voltage.setText("Power-down")
            editor.lock.setText(LOCK_NAMES.get(lock, "Unknown"))
            editor.lock.setForeground(
                QColor("#82e3aa") if lock == 0 else QColor("#ff9aa2")
            )

            # Always refresh the live readback columns above, but never replace
            # a target value that the user has modified or is actively typing.
            editor_is_active = self._channel_value_editor_has_focus(editor)
            if not preserve_dirty or (not editor.dirty and not editor_is_active):
                editor.syncing = True
                try:
                    editor.vref.setCurrentIndex(editor.vref.findData(vref))
                    editor.gain.setCurrentIndex(editor.gain.findData(gain))
                    editor.power_down.setCurrentIndex(editor.power_down.findData(power))
                    editor.target_code.setValue(code)
                    target_voltage = code_to_voltage(
                        code, channel, vref, gain, vdd, vref0, vref1, bandgap
                    )
                    editor.target_voltage.setValue(target_voltage)
                    editor.target_hex.setText("0x{:04X}".format(code))
                finally:
                    editor.syncing = False
                self._set_row_dirty(channel, False)

        status = state.get("status", {})
        self.info_labels["por"].setText("Yes" if status.get("por_seen") else "No")
        self.info_labels["eeprom_busy"].setText(
            "Yes" if status.get("eeprom_busy") else "No"
        )
        uptime = int(state.get("uptime_ms", 0))
        self.info_labels["uptime"].setText(self._format_uptime(uptime))
        self.status_message.setText("Register readback updated")

    def _apply_eeprom(self, eeprom: dict[str, Any]) -> None:
        self.last_eeprom = copy.deepcopy(eeprom)
        channels = eeprom.get("channels", [])
        if len(channels) != 8:
            return

        vdd, vref0, vref1, bandgap = self._calibration()
        for row, item in enumerate(channels):
            channel = int(item["channel"])
            code = int(item["code"])
            vref = int(item["vref"])
            gain = int(item["gain"])
            power = int(item["power_down"])
            voltage_text = (
                "{:.6f} V".format(
                    code_to_voltage(
                        code, channel, vref, gain, vdd, vref0, vref1, bandgap
                    )
                )
                if power == 0
                else "Power-down"
            )
            values = [
                CHANNEL_NAMES[channel],
                "DAC{}".format(channel),
                str(code),
                "0x{:04X}".format(code),
                voltage_text,
                VREF_NAMES.get(vref, "Unknown"),
                "{}x".format(2 if gain else 1),
                POWER_NAMES.get(power, "Unknown"),
            ]
            for column, text in enumerate(values):
                item_widget = QTableWidgetItem(text)
                if column in (1, 2, 3, 4, 6):
                    item_widget.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.eeprom_table.setItem(row, column, item_widget)

        address = int(eeprom.get("address", 0))
        locked = bool(eeprom.get("address_locked", False))
        self.eeprom_address_label.setText(
            "Stored address: 0x{:02X}  |  address lock: {}".format(
                address, "enabled" if locked else "disabled"
            )
        )

    def _update_device_info(self) -> None:
        info = self.device_info
        mappings = {
            "firmware": info.get("firmware", "--"),
            "firmware_version": info.get("firmware_version", "--"),
            "rp2040_uid": info.get("rp2040_uid", "--"),
            "device": "{} ({}-bit, {} channels)".format(
                info.get("device", "--"),
                info.get("resolution_bits", "--"),
                info.get("channels", "--"),
            ),
            "dac_address": "0x{:02X}".format(int(info.get("dac_address", 0))),
            "i2c_bus": str(info.get("i2c_bus", "--")),
            "pins": "GP{} / GP{}".format(info.get("sda_pin", "--"), info.get("scl_pin", "--")),
            "i2c_frequency": "{:,} Hz".format(int(info.get("i2c_frequency", 0))),
            "lat_mode": str(info.get("lat_mode", "--")),
        }
        for key, value in mappings.items():
            self.info_labels[key].setText(str(value))

    @staticmethod
    def _format_uptime(milliseconds: int) -> str:
        seconds = max(milliseconds, 0) // 1000
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)

    # ----- EEPROM and raw controls ----------------------------------------

    def save_eeprom(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Save power-on settings",
            "This writes the current eight DAC codes, reference modes, gains and "
            "power-down modes into the MCP47FEB28 EEPROM. They will become the "
            "power-on defaults.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.send_command("save", "save_eeprom", {"confirm": "SAVE"}, timeout=4.0):
            self.status_message.setText("Saving changed registers to EEPROM...")

    def load_eeprom(self) -> None:
        answer = QMessageBox.question(
            self,
            "Load EEPROM settings",
            "This immediately replaces all volatile settings and output codes with "
            "the power-on values stored in EEPROM. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.send_command("load", "load_eeprom", timeout=3.0):
            self.status_message.setText("Loading EEPROM settings into outputs...")

    def raw_read(self) -> None:
        register = int(self.raw_register_combo.currentData())
        self.send_command("raw_read", "raw_read", {"register": register})

    def raw_write(self) -> None:
        register = int(self.raw_register_combo.currentData())
        if register > 0x0A:
            QMessageBox.information(
                self,
                "Read-only here",
                "The raw interface only writes volatile registers 0x00 through 0x0A.",
            )
            return
        try:
            value = int(self.raw_value_edit.text().strip(), 0)
        except ValueError:
            QMessageBox.warning(self, "Invalid value", "Enter a value such as 0x04D9 or 1241.")
            return
        if value < 0 or value > 0xFFFF:
            QMessageBox.warning(self, "Invalid value", "The raw value must fit in 16 bits.")
            return

        answer = QMessageBox.question(
            self,
            "Write volatile register",
            "Write 0x{:04X} to register 0x{:02X}? This can immediately change DAC outputs.".format(
                value, register
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.send_command(
                "raw_write",
                "raw_write",
                {"register": register, "value": value},
            )

    # ----- General control state and shutdown ------------------------------

    def _set_dac_controls_enabled(self, enabled: bool) -> None:
        controls = [
            self.read_now_button,
            self.apply_all_button,
            self.zero_all_button,
            self.refresh_eeprom_button,
            self.save_eeprom_button,
            self.load_eeprom_button,
            self.raw_read_button,
            self.raw_write_button,
        ]
        controls.extend(editor.apply_button for editor in self.channel_editors)
        for control in controls:
            control.setEnabled(enabled)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        self._save_settings()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(1500)
        event.accept()


# ---------------------------------------------------------------------------
# Entry point and basic calculation self-test
# ---------------------------------------------------------------------------


def run_self_test() -> int:
    assert voltage_to_code(1.0, 0, 0, 0, 3.3, 3.3, 3.3, 1.22) == 0x04D9
    assert abs(code_to_voltage(0x04D9, 0, 0, 0, 3.3, 3.3, 3.3, 1.22) - 0.999829) < 1e-6
    assert voltage_to_code(0.0, 7, 0, 0, 3.3, 3.3, 3.3, 1.22) == 0
    assert voltage_to_code(9.0, 0, 0, 0, 3.3, 3.3, 3.3, 1.22) == 0x0FFF
    assert abs(output_scale(0, 1, 0, 3.3, 2.5, 2.5, 1.22) - 2.44) < 1e-9
    print("Self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--demo", action="store_true", help="run with a simulated DAC")
    parser.add_argument("--self-test", action="store_true", help="test calculations and exit")
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("OpenAI")

    window = MainWindow(demo=args.demo or bool(args.screenshot))
    window.show()

    if args.screenshot:
        destination = Path(args.screenshot).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)

        def capture() -> None:
            window.grab().save(str(destination))
            app.quit()

        QTimer.singleShot(900, capture)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
