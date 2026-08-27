"""
SiPM Bias Control

Operator-focused desktop interface for the Tiny 2040 MCP47FEB28 firmware.
The application converts desired high-voltage bias setpoints into DAC codes
using the external LTC6090 feedback-divider gain, reads DAC registers back, and
plots their corresponding estimated bias voltage over time.

Important: register readback is not an analog measurement of the SiPM bias pin.

Run:
    python sipm_bias_control_app.py

Run without hardware:
    python sipm_bias_control_app.py --demo
"""

from __future__ import annotations

import argparse
import bisect
import csv
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import sys
import time
from typing import Any

from serial.tools import list_ports

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QMargins, QPointF, QSettings, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)


DESKTOP_DIRECTORY = Path(__file__).resolve().parent
if str(DESKTOP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIRECTORY))

from dac_control_app import (  # noqa: E402
    CHANNEL_NAMES,
    DAC_MAX_CODE,
    DAC_STEPS,
    SerialWorker,
)


APP_NAME = "SiPM Bias Control"
APP_VERSION = "1.1.0"

DEFAULT_MAIN_SOURCE_V = 20.0
DEFAULT_DAC_VDD_V = 3.3
DEFAULT_R_FEEDBACK_KOHM = 82.0
DEFAULT_R_GROUND_KOHM = 3.9
DEFAULT_OUTPUT_HEADROOM_V = 0.10
DEFAULT_POLL_INTERVAL_MS = 500
MAX_HISTORY_SAMPLES = 20_000
SLIDER_UNITS_PER_VOLT = 1000

CHANNEL_COLORS = (
    "#4da3ff",
    "#ff8a65",
    "#7bd88f",
    "#c792ea",
    "#ffd166",
    "#5eead4",
    "#f472b6",
    "#a3e635",
)


# ---------------------------------------------------------------------------
# Pure circuit calculations
# ---------------------------------------------------------------------------


def amplifier_gain(r_feedback_kohm: float, r_ground_kohm: float) -> float:
    """Return the LTC6090 non-inverting closed-loop gain."""
    if r_ground_kohm <= 0:
        raise ValueError("R to ground must be greater than zero")
    return 1.0 + float(r_feedback_kohm) / float(r_ground_kohm)


def dac_code_to_voltage(code: int, dac_vdd: float) -> float:
    """Convert a 12-bit MCP47FEB28 code to its ideal VDD-referenced output."""
    code = min(max(int(code), 0), DAC_MAX_CODE)
    return float(dac_vdd) * code / DAC_STEPS


def usable_bias_maximum(
    main_source_v: float,
    dac_vdd: float,
    gain: float,
    output_headroom_v: float,
) -> float:
    """Return the highest setpoint allowed by both source and DAC range."""
    source_limit = max(0.0, float(main_source_v) - max(0.0, output_headroom_v))
    dac_limit = dac_code_to_voltage(DAC_MAX_CODE, dac_vdd) * gain
    return max(0.0, min(source_limit, dac_limit))


def bias_voltage_to_code(
    requested_bias_v: float,
    dac_vdd: float,
    gain: float,
    maximum_bias_v: float,
) -> int:
    """Convert a desired external bias into the nearest safe DAC code."""
    if dac_vdd <= 0 or gain <= 0:
        return 0
    bias = min(max(float(requested_bias_v), 0.0), max(0.0, maximum_bias_v))
    dac_voltage = bias / gain
    code = int(dac_voltage * DAC_STEPS / dac_vdd + 0.5)
    return min(max(code, 0), DAC_MAX_CODE)


def code_to_estimated_bias(
    code: int,
    dac_vdd: float,
    gain: float,
    maximum_bias_v: float,
) -> float:
    """Estimate external bias from readback, including configured clipping."""
    ideal = dac_code_to_voltage(code, dac_vdd) * gain
    return min(max(ideal, 0.0), max(0.0, float(maximum_bias_v)))


# ---------------------------------------------------------------------------
# Channel UI storage
# ---------------------------------------------------------------------------


@dataclass
class ChannelControls:
    channel: int
    frame: QFrame
    plot_check: QCheckBox
    target_spin: QDoubleSpinBox
    slider: QSlider
    command_value: QLabel
    command_code: QLabel
    readback_value: QLabel
    readback_code: QLabel
    estimated_bias: QLabel
    delta_label: QLabel
    apply_button: QPushButton
    state_label: QLabel
    dirty: bool = False
    syncing: bool = False
    last_code: int | None = None
    last_estimated_bias: float | None = None


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------


class BiasControlWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.demo = demo
        self.settings = QSettings("OpenAI", "SiPMBiasControl")
        self.worker: SerialWorker | None = None
        self.connected_flag = False
        self.dac_present = False
        self.device_info: dict[str, Any] = {}
        self.pending_tags: set[str] = set()
        self.tag_counter = 0
        self.last_state: dict[str, Any] | None = None
        self.channel_controls: list[ChannelControls] = []

        self.history_epoch = time.monotonic()
        self.history_elapsed: deque[float] = deque(maxlen=MAX_HISTORY_SAMPLES)
        self.history_wall_time: deque[str] = deque(maxlen=MAX_HISTORY_SAMPLES)
        self.history_bias = [deque(maxlen=MAX_HISTORY_SAMPLES) for _ in range(8)]
        self.history_dac = [deque(maxlen=MAX_HISTORY_SAMPLES) for _ in range(8)]
        self.history_codes = [deque(maxlen=MAX_HISTORY_SAMPLES) for _ in range(8)]
        self.plot_curves: list[QLineSeries] = []

        self.demo_started = time.monotonic()
        self.demo_state = self._make_demo_state()

        self.setWindowTitle("{} v{}".format(APP_NAME, APP_VERSION))
        self.resize(1540, 930)
        self.setMinimumSize(1240, 720)

        self._build_interface()
        self._apply_stylesheet()
        self._restore_settings()
        self.refresh_ports()
        self._calibration_changed()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_tick)

        self.status_message.setText(
            "Demo mode: simulated firmware" if demo else "Select the Tiny 2040 USB port"
        )

        if self.demo:
            QTimer.singleShot(80, self.toggle_connection)

    # ----- Interface construction ---------------------------------------

    def _build_interface(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 10)
        root.setSpacing(10)

        root.addWidget(self._build_header())
        root.addWidget(self._build_source_panel())

        notice = QLabel(
            "READBACK is the value stored in the DAC register. The estimated bias is "
            "calculated from that code and the circuit model; it is not an ADC measurement "
            "of the physical SiPM voltage."
        )
        notice.setObjectName("infoBanner")
        notice.setWordWrap(True)
        root.addWidget(notice)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_channel_panel())
        splitter.addWidget(self._build_history_panel())
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 6)
        splitter.setSizes([800, 690])
        root.addWidget(splitter, 1)

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self.status_message = QLabel()
        self.poll_status = QLabel("Polling stopped")
        status_bar.addWidget(self.status_message, 1)
        status_bar.addPermanentWidget(self.poll_status)

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("headerFrame")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        icon = QLabel("HV")
        icon.setObjectName("appIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        title_box = QVBoxLayout()
        title = QLabel("SiPM Bias Control")
        title.setObjectName("appTitle")
        subtitle = QLabel("Tiny 2040  •  MCP47FEB28  •  8-channel bias trim")
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box)
        layout.addStretch(1)

        port_label = QLabel("USB port")
        port_label.setObjectName("fieldCaption")
        layout.addWidget(port_label)

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(210)
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
        self.connection_badge.setMinimumWidth(104)
        layout.addWidget(self.connection_badge)

        self.header_zero_button = QPushButton("ZERO ALL")
        self.header_zero_button.setObjectName("dangerButton")
        self.header_zero_button.setEnabled(False)
        self.header_zero_button.setToolTip("Immediately write zero to all eight volatile DAC registers")
        self.header_zero_button.clicked.connect(self.zero_all)
        layout.addWidget(self.header_zero_button)
        return frame

    def _build_source_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sourcePanel")
        layout = QGridLayout(panel)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(4)

        source_caption = QLabel("MAIN HV SOURCE (MEASURED)")
        source_caption.setObjectName("fieldCaption")
        self.main_source_spin = QDoubleSpinBox()
        self.main_source_spin.setObjectName("heroSpin")
        self.main_source_spin.setRange(1.0, 140.0)
        self.main_source_spin.setDecimals(3)
        self.main_source_spin.setSingleStep(1.0)
        self.main_source_spin.setSuffix(" V")
        self.main_source_spin.setKeyboardTracking(False)
        self.main_source_spin.valueChanged.connect(self._calibration_changed)
        layout.addWidget(source_caption, 0, 0)
        layout.addWidget(self.main_source_spin, 1, 0)

        dac_caption = QLabel("DAC VDD / REFERENCE")
        dac_caption.setObjectName("fieldCaption")
        self.dac_vdd_spin = QDoubleSpinBox()
        self.dac_vdd_spin.setRange(1.8, 5.5)
        self.dac_vdd_spin.setDecimals(4)
        self.dac_vdd_spin.setSingleStep(0.001)
        self.dac_vdd_spin.setSuffix(" V")
        self.dac_vdd_spin.setKeyboardTracking(False)
        self.dac_vdd_spin.valueChanged.connect(self._calibration_changed)
        layout.addWidget(dac_caption, 0, 1)
        layout.addWidget(self.dac_vdd_spin, 1, 1)

        feedback_caption = QLabel("FEEDBACK / GROUND")
        feedback_caption.setObjectName("fieldCaption")
        resistor_row = QHBoxLayout()
        self.feedback_resistor_spin = QDoubleSpinBox()
        self.feedback_resistor_spin.setRange(0.1, 1000.0)
        self.feedback_resistor_spin.setDecimals(3)
        self.feedback_resistor_spin.setSuffix(" kΩ")
        self.feedback_resistor_spin.setKeyboardTracking(False)
        self.feedback_resistor_spin.valueChanged.connect(self._calibration_changed)
        self.ground_resistor_spin = QDoubleSpinBox()
        self.ground_resistor_spin.setRange(0.1, 1000.0)
        self.ground_resistor_spin.setDecimals(3)
        self.ground_resistor_spin.setSuffix(" kΩ")
        self.ground_resistor_spin.setKeyboardTracking(False)
        self.ground_resistor_spin.valueChanged.connect(self._calibration_changed)
        resistor_row.addWidget(self.feedback_resistor_spin)
        separator = QLabel("/")
        separator.setObjectName("mutedText")
        resistor_row.addWidget(separator)
        resistor_row.addWidget(self.ground_resistor_spin)
        layout.addWidget(feedback_caption, 0, 2)
        layout.addLayout(resistor_row, 1, 2)

        headroom_caption = QLabel("POSITIVE-RAIL HEADROOM")
        headroom_caption.setObjectName("fieldCaption")
        self.headroom_spin = QDoubleSpinBox()
        self.headroom_spin.setRange(0.0, 10.0)
        self.headroom_spin.setDecimals(3)
        self.headroom_spin.setSingleStep(0.05)
        self.headroom_spin.setSuffix(" V")
        self.headroom_spin.setKeyboardTracking(False)
        self.headroom_spin.valueChanged.connect(self._calibration_changed)
        layout.addWidget(headroom_caption, 0, 3)
        layout.addWidget(self.headroom_spin, 1, 3)

        gain_caption = QLabel("AMPLIFIER GAIN")
        gain_caption.setObjectName("fieldCaption")
        self.gain_value_label = QLabel("×--")
        self.gain_value_label.setObjectName("heroValue")
        layout.addWidget(gain_caption, 0, 4)
        layout.addWidget(self.gain_value_label, 1, 4)

        range_caption = QLabel("AVAILABLE SETPOINT RANGE")
        range_caption.setObjectName("fieldCaption")
        self.range_value_label = QLabel("0 – -- V")
        self.range_value_label.setObjectName("heroValue")
        layout.addWidget(range_caption, 0, 5)
        layout.addWidget(self.range_value_label, 1, 5)

        layout.setColumnStretch(0, 2)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 2)
        layout.setColumnStretch(3, 1)
        layout.setColumnStretch(4, 1)
        layout.setColumnStretch(5, 2)
        return panel

    def _build_channel_panel(self) -> QWidget:
        outer = QFrame()
        outer.setObjectName("mainPanel")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("Channel setpoints")
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)

        live_label = QLabel("Live slider writes")
        live_label.setObjectName("mutedText")
        self.live_slider_check = QCheckBox()
        self.live_slider_check.setToolTip(
            "When enabled, releasing a slider immediately writes that channel. "
            "It is off by default for HV safety."
        )
        title_row.addWidget(live_label)
        title_row.addWidget(self.live_slider_check)
        layout.addLayout(title_row)

        global_bar = QFrame()
        global_bar.setObjectName("globalBar")
        global_layout = QHBoxLayout(global_bar)
        global_layout.setContentsMargins(10, 8, 10, 8)

        global_layout.addWidget(QLabel("Set every target to"))
        self.global_target_spin = QDoubleSpinBox()
        self.global_target_spin.setDecimals(3)
        self.global_target_spin.setSingleStep(0.1)
        self.global_target_spin.setSuffix(" V")
        self.global_target_spin.setKeyboardTracking(False)
        global_layout.addWidget(self.global_target_spin)

        set_targets = QPushButton("Stage all")
        set_targets.clicked.connect(self.stage_all_targets)
        global_layout.addWidget(set_targets)

        self.apply_all_button = QPushButton("Apply all channels")
        self.apply_all_button.setObjectName("primaryButton")
        self.apply_all_button.setEnabled(False)
        self.apply_all_button.clicked.connect(self.apply_all)
        global_layout.addWidget(self.apply_all_button)
        global_layout.addStretch(1)

        self.poll_interval_spin = QSpinBox()
        self.poll_interval_spin.setRange(100, 10_000)
        self.poll_interval_spin.setSingleStep(100)
        self.poll_interval_spin.setSuffix(" ms readback")
        self.poll_interval_spin.valueChanged.connect(self._update_poll_timer)
        global_layout.addWidget(self.poll_interval_spin)
        layout.addWidget(global_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        self.channels_layout = QVBoxLayout(scroll_content)
        self.channels_layout.setContentsMargins(0, 0, 0, 0)
        self.channels_layout.setSpacing(7)
        for channel in range(8):
            self._create_channel_card(channel)
        self.channels_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)
        return outer

    def _create_channel_card(self, channel: int) -> None:
        frame = QFrame()
        frame.setObjectName("channelCard")
        frame.setMinimumHeight(86)
        grid = QGridLayout(frame)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(8)

        plot_check = QCheckBox()
        plot_check.setChecked(True)
        plot_check.setToolTip("Show or hide this channel in the history graph")
        grid.addWidget(plot_check, 0, 0, Qt.AlignmentFlag.AlignVCenter)

        swatch = QFrame()
        swatch.setFixedSize(5, 50)
        swatch.setStyleSheet(
            "background:{}; border-radius:2px; border:none;".format(CHANNEL_COLORS[channel])
        )
        grid.addWidget(swatch, 0, 1, Qt.AlignmentFlag.AlignVCenter)

        identity = QVBoxLayout()
        identity.setSpacing(2)
        net = QLabel(CHANNEL_NAMES[channel])
        net.setObjectName("channelName")
        dac = QLabel("DAC{} / VOUT{}".format(channel, channel))
        dac.setObjectName("mutedText")
        identity.addWidget(net)
        identity.addWidget(dac)
        identity.addStretch(1)
        grid.addLayout(identity, 0, 2)

        set_caption = QLabel("SET BIAS")
        set_caption.setObjectName("fieldCaption")
        target_spin = QDoubleSpinBox()
        target_spin.setDecimals(3)
        target_spin.setSingleStep(0.010)
        target_spin.setSuffix(" V")
        target_spin.setKeyboardTracking(False)
        target_spin.setMinimumWidth(100)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setSingleStep(10)
        slider.setPageStep(100)
        slider.setMinimumWidth(115)
        setpoint_box = QVBoxLayout()
        setpoint_box.setSpacing(3)
        setpoint_box.addWidget(set_caption)
        setpoint_box.addWidget(target_spin)
        setpoint_box.addWidget(slider)
        grid.addLayout(setpoint_box, 0, 3)

        command_caption = QLabel("DAC COMMAND")
        command_caption.setObjectName("fieldCaption")
        command_value = QLabel("0.0000 V")
        command_value.setObjectName("readoutValue")
        command_code = QLabel("0 / 0x000")
        command_code.setObjectName("mutedText")
        command_box = QVBoxLayout()
        command_box.setSpacing(3)
        command_box.addWidget(command_caption)
        command_box.addWidget(command_value)
        command_box.addWidget(command_code)
        grid.addLayout(command_box, 0, 4)

        read_caption = QLabel("DAC READBACK")
        read_caption.setObjectName("fieldCaption")
        readback_value = QLabel("-- V")
        readback_value.setObjectName("readoutValue")
        readback_code = QLabel("--")
        readback_code.setObjectName("mutedText")
        readback_box = QVBoxLayout()
        readback_box.setSpacing(3)
        readback_box.addWidget(read_caption)
        readback_box.addWidget(readback_value)
        readback_box.addWidget(readback_code)
        grid.addLayout(readback_box, 0, 5)

        estimate_caption = QLabel("ESTIMATED BIAS")
        estimate_caption.setObjectName("fieldCaption")
        estimated_bias = QLabel("-- V")
        estimated_bias.setObjectName("biasReadout")
        delta_label = QLabel("Δ --")
        delta_label.setObjectName("mutedText")
        estimate_box = QVBoxLayout()
        estimate_box.setSpacing(3)
        estimate_box.addWidget(estimate_caption)
        estimate_box.addWidget(estimated_bias)
        estimate_box.addWidget(delta_label)
        grid.addLayout(estimate_box, 0, 6)

        apply_button = QPushButton("Apply")
        apply_button.setEnabled(False)
        state_label = QLabel("Not read")
        state_label.setObjectName("stateLabel")
        state_label.setProperty("state", "idle")
        state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        action_box = QVBoxLayout()
        action_box.setSpacing(5)
        action_box.addWidget(apply_button)
        action_box.addWidget(state_label)
        grid.addLayout(action_box, 0, 7)

        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 3)
        grid.setColumnStretch(4, 1)
        grid.setColumnStretch(5, 1)
        grid.setColumnStretch(6, 1)

        controls = ChannelControls(
            channel=channel,
            frame=frame,
            plot_check=plot_check,
            target_spin=target_spin,
            slider=slider,
            command_value=command_value,
            command_code=command_code,
            readback_value=readback_value,
            readback_code=readback_code,
            estimated_bias=estimated_bias,
            delta_label=delta_label,
            apply_button=apply_button,
            state_label=state_label,
        )
        self.channel_controls.append(controls)
        self.channels_layout.addWidget(frame)

        target_spin.valueChanged.connect(
            lambda value, ch=channel: self._target_spin_changed(ch, value)
        )
        target_spin.lineEdit().textEdited.connect(
            lambda _text, ch=channel: self._mark_channel_dirty(ch)
        )
        target_spin.editingFinished.connect(
            lambda ch=channel: self._apply_if_live(ch)
        )
        slider.valueChanged.connect(
            lambda value, ch=channel: self._slider_changed(ch, value)
        )
        slider.sliderReleased.connect(lambda ch=channel: self._apply_if_live(ch))
        plot_check.toggled.connect(lambda _checked: self._update_plot())
        apply_button.clicked.connect(lambda _checked=False, ch=channel: self.apply_channel(ch))

    def _build_history_panel(self) -> QWidget:
        outer = QFrame()
        outer.setObjectName("mainPanel")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("Bias history")
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)

        self.graph_cursor_label = QLabel("Hover a trace for its exact value")
        self.graph_cursor_label.setObjectName("mutedText")
        title_row.addWidget(self.graph_cursor_label)

        self.sample_count_label = QLabel("0 samples")
        self.sample_count_label.setObjectName("mutedText")
        title_row.addWidget(self.sample_count_label)
        layout.addLayout(title_row)

        tools = QFrame()
        tools.setObjectName("globalBar")
        tool_layout = QHBoxLayout(tools)
        tool_layout.setContentsMargins(9, 7, 9, 7)
        tool_layout.addWidget(QLabel("Window"))
        self.history_window_combo = QComboBox()
        self.history_window_combo.addItem("30 seconds", 30.0)
        self.history_window_combo.addItem("2 minutes", 120.0)
        self.history_window_combo.addItem("10 minutes", 600.0)
        self.history_window_combo.addItem("All", 0.0)
        self.history_window_combo.currentIndexChanged.connect(self._update_plot)
        tool_layout.addWidget(self.history_window_combo)

        self.scale_to_source_check = QCheckBox("Scale to source")
        self.scale_to_source_check.setChecked(True)
        self.scale_to_source_check.toggled.connect(self._update_plot)
        tool_layout.addWidget(self.scale_to_source_check)

        self.pause_plot_check = QCheckBox("Pause display")
        self.pause_plot_check.toggled.connect(self._update_plot)
        tool_layout.addWidget(self.pause_plot_check)
        tool_layout.addStretch(1)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.clear_history)
        tool_layout.addWidget(clear_button)
        export_button = QPushButton("Export CSV")
        export_button.clicked.connect(self.export_history_csv)
        tool_layout.addWidget(export_button)
        layout.addWidget(tools)

        self.chart = QChart()
        self.chart.setTheme(QChart.ChartTheme.ChartThemeLight)
        self.chart.setTitle("")
        self.chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        self.chart.setMargins(QMargins(4, 2, 4, 2))
        self.chart.setBackgroundRoundness(0)
        self.chart.setBackgroundBrush(QColor("#ffffff"))
        self.chart.setPlotAreaBackgroundBrush(QColor("#fafbfc"))
        self.chart.setPlotAreaBackgroundVisible(True)
        self.chart.legend().setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chart.legend().setLabelColor(QColor("#25313b"))

        self.time_axis = QValueAxis()
        self.time_axis.setTitleText("Elapsed time (s)")
        self.time_axis.setLabelFormat("%.1f")
        self.time_axis.setTickCount(6)
        self.time_axis.setMinorTickCount(3)
        self.time_axis.setRange(0.0, 30.0)

        self.bias_axis = QValueAxis()
        self.bias_axis.setTitleText("Estimated bias from DAC register (V)")
        self.bias_axis.setLabelFormat("%.1f")
        self.bias_axis.setTickCount(6)
        self.bias_axis.setMinorTickCount(3)
        self.bias_axis.setRange(0.0, DEFAULT_MAIN_SOURCE_V)

        axis_pen = QPen(QColor("#5f6f7c"), 1)
        grid_pen = QPen(QColor("#d4dbe1"), 1)
        minor_grid_pen = QPen(QColor("#edf0f2"), 1)
        for axis in (self.time_axis, self.bias_axis):
            axis.setLinePen(axis_pen)
            axis.setLabelsColor(QColor("#43525e"))
            axis.setTitleBrush(QColor("#43525e"))
            axis.setGridLinePen(grid_pen)
            axis.setMinorGridLinePen(minor_grid_pen)

        self.chart.addAxis(self.time_axis, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.bias_axis, Qt.AlignmentFlag.AlignLeft)

        self.plot_widget = QChartView(self.chart)
        self.plot_widget.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.plot_widget.setRubberBand(QChartView.RubberBand.RectangleRubberBand)
        self.plot_widget.setMinimumHeight(300)
        for channel in range(8):
            series = QLineSeries()
            series.setName(CHANNEL_NAMES[channel])
            series.setPen(QPen(QColor(CHANNEL_COLORS[channel]), 2))
            series.hovered.connect(
                lambda point, entered, ch=channel: self._series_hovered(ch, point, entered)
            )
            self.chart.addSeries(series)
            series.attachAxis(self.time_axis)
            series.attachAxis(self.bias_axis)
            self.plot_curves.append(series)
        layout.addWidget(self.plot_widget, 1)

        graph_note = QLabel(
            "The chart logs estimated bias derived from volatile DAC register readback. "
            "Add an ADC/multiplexer if the physical HV output must be measured."
        )
        graph_note.setObjectName("mutedText")
        graph_note.setWordWrap(True)
        layout.addWidget(graph_note)
        return outer

    # ----- Styling -------------------------------------------------------

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #edf0f2;
                color: #202a33;
                font-family: "Segoe UI", Arial, sans-serif;
                font-size: 10pt;
            }
            QLabel, QCheckBox { background: transparent; }
            QFrame#headerFrame, QFrame#sourcePanel, QFrame#mainPanel {
                background: #ffffff;
                border: 1px solid #b9c2ca;
                border-radius: 5px;
            }
            QFrame#channelCard {
                background: #ffffff;
                border: 1px solid #c7cfd6;
                border-radius: 4px;
            }
            QFrame#globalBar {
                background: #e8ecef;
                border: 1px solid #c1c9d0;
                border-radius: 4px;
            }
            QLabel#appIcon {
                background: #236a97;
                color: white;
                border-radius: 5px;
                font-size: 14pt;
                font-weight: 700;
                min-width: 46px;
                min-height: 46px;
            }
            QLabel#appTitle { font-size: 17pt; font-weight: 650; color: #1f2a33; }
            QLabel#appSubtitle, QLabel#mutedText { color: #647483; }
            QLabel#sectionTitle { font-size: 12pt; font-weight: 650; color: #24313b; }
            QLabel#channelName { font-size: 11pt; font-weight: 650; color: #24313b; }
            QLabel#fieldCaption {
                color: #516678;
                font-size: 8pt;
                font-weight: 650;
            }
            QLabel#heroValue { color: #1d628f; font-size: 14pt; font-weight: 650; }
            QLabel#readoutValue { color: #26343f; font-weight: 650; }
            QLabel#biasReadout { color: #17663b; font-size: 13pt; font-weight: 700; }
            QLabel#infoBanner {
                background: #e4f0f6;
                color: #234c63;
                border: 1px solid #86aebe;
                border-radius: 4px;
                padding: 8px 12px;
            }
            QLabel#statusOff, QLabel#statusWarn, QLabel#statusOn {
                border-radius: 4px;
                padding: 8px 10px;
                font-weight: 650;
            }
            QLabel#statusOff { background: #e2e6e9; color: #53616c; border: 1px solid #c2c9cf; }
            QLabel#statusWarn { background: #fff1c9; color: #725300; border: 1px solid #d5b85c; }
            QLabel#statusOn { background: #dcefe2; color: #145d34; border: 1px solid #8bb79a; }
            QLabel#stateLabel {
                border-radius: 3px;
                padding: 2px 5px;
                font-size: 8pt;
            }
            QLabel#stateLabel[state="idle"] { background: #e4e8eb; color: #5a6873; border: 1px solid #c6cdd3; }
            QLabel#stateLabel[state="pending"] { background: #fff1c9; color: #725300; border: 1px solid #d5b85c; }
            QLabel#stateLabel[state="applied"] { background: #dcefe2; color: #145d34; border: 1px solid #8bb79a; }
            QLabel#stateLabel[state="error"] { background: #f8dfe1; color: #8b252e; border: 1px solid #cf8e94; }
            QPushButton {
                background: #e5eaee;
                border: 1px solid #aab5bf;
                border-radius: 4px;
                padding: 7px 12px;
                color: #23303a;
            }
            QPushButton:hover { background: #d8e0e6; border-color: #8394a2; }
            QPushButton:pressed { background: #c9d3db; }
            QPushButton:disabled { color: #909ba4; background: #eef0f2; border-color: #d1d6da; }
            QPushButton#primaryButton { background: #236fa3; border-color: #15567f; color: white; }
            QPushButton#primaryButton:hover { background: #195f8e; }
            QPushButton#dangerButton { background: #b43c43; border-color: #8d292f; color: white; }
            QPushButton#dangerButton:hover { background: #9e3037; }
            QComboBox, QSpinBox, QDoubleSpinBox {
                background: #ffffff;
                color: #202a33;
                border: 1px solid #98a6b2;
                border-radius: 3px;
                padding: 5px 7px;
                selection-background-color: #317ead;
                selection-color: white;
            }
            QDoubleSpinBox#heroSpin {
                font-size: 15pt;
                font-weight: 700;
                color: #17232d;
                padding: 6px 9px;
            }
            QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #2479aa;
            }
            QSlider::groove:horizontal {
                height: 5px;
                background: #c8d0d7;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #2777a9;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: #ffffff;
                border: 2px solid #2777a9;
            }
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical, QScrollBar:horizontal { background: #e5e9ec; border: none; }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #9ba8b2; border-radius: 3px; min-height: 24px; min-width: 24px; }
            QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
            QSplitter::handle { background: #edf0f2; width: 7px; }
            QStatusBar { background: #dfe4e8; color: #43525e; border-top: 1px solid #bdc6cd; }
            QToolTip { background: #27343e; color: white; border: 1px solid #596b79; }
            """
        )

    # ----- Settings, calibration and model ------------------------------

    def _restore_settings(self) -> None:
        self.main_source_spin.setValue(
            float(self.settings.value("main_source_v", DEFAULT_MAIN_SOURCE_V))
        )
        self.dac_vdd_spin.setValue(
            float(self.settings.value("dac_vdd_v", DEFAULT_DAC_VDD_V))
        )
        self.feedback_resistor_spin.setValue(
            float(self.settings.value("r_feedback_kohm", DEFAULT_R_FEEDBACK_KOHM))
        )
        self.ground_resistor_spin.setValue(
            float(self.settings.value("r_ground_kohm", DEFAULT_R_GROUND_KOHM))
        )
        self.headroom_spin.setValue(
            float(self.settings.value("headroom_v", DEFAULT_OUTPUT_HEADROOM_V))
        )
        self.poll_interval_spin.setValue(
            int(self.settings.value("poll_interval_ms", DEFAULT_POLL_INTERVAL_MS))
        )
        window_index = int(self.settings.value("history_window_index", 1))
        self.history_window_combo.setCurrentIndex(
            min(max(window_index, 0), self.history_window_combo.count() - 1)
        )
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        self.settings.setValue("main_source_v", self.main_source_spin.value())
        self.settings.setValue("dac_vdd_v", self.dac_vdd_spin.value())
        self.settings.setValue("r_feedback_kohm", self.feedback_resistor_spin.value())
        self.settings.setValue("r_ground_kohm", self.ground_resistor_spin.value())
        self.settings.setValue("headroom_v", self.headroom_spin.value())
        self.settings.setValue("poll_interval_ms", self.poll_interval_spin.value())
        self.settings.setValue("history_window_index", self.history_window_combo.currentIndex())
        self.settings.setValue("geometry", self.saveGeometry())
        if self.port_combo.currentData():
            self.settings.setValue("last_port", self.port_combo.currentData())

    def _gain(self) -> float:
        return amplifier_gain(
            self.feedback_resistor_spin.value(), self.ground_resistor_spin.value()
        )

    def _maximum_bias(self) -> float:
        return usable_bias_maximum(
            self.main_source_spin.value(),
            self.dac_vdd_spin.value(),
            self._gain(),
            self.headroom_spin.value(),
        )

    def _calibration_changed(self) -> None:
        if not hasattr(self, "channel_controls"):
            return

        gain = self._gain()
        maximum = self._maximum_bias()
        self.gain_value_label.setText("×{:.4f}".format(gain))
        self.range_value_label.setText("0 – {:.3f} V".format(maximum))
        self.global_target_spin.setRange(0.0, maximum)

        slider_maximum = max(0, int(round(maximum * SLIDER_UNITS_PER_VOLT)))
        for controls in self.channel_controls:
            old_value = controls.target_spin.value()
            controls.syncing = True
            try:
                controls.target_spin.setRange(0.0, maximum)
                controls.slider.setRange(0, slider_maximum)
                controls.target_spin.setValue(min(old_value, maximum))
                controls.slider.setValue(
                    int(round(controls.target_spin.value() * SLIDER_UNITS_PER_VOLT))
                )
            finally:
                controls.syncing = False
            if old_value > maximum + 0.0005:
                self._set_channel_dirty(controls.channel, True)
            self._update_command_display(controls.channel)

        if self.last_state is not None:
            self._apply_state(self.last_state, record_sample=False, preserve_dirty=True)
        self._update_plot()

    # ----- Channel editing ----------------------------------------------

    def _target_spin_changed(self, channel: int, value: float) -> None:
        controls = self.channel_controls[channel]
        if controls.syncing:
            return
        controls.syncing = True
        try:
            controls.slider.setValue(int(round(value * SLIDER_UNITS_PER_VOLT)))
        finally:
            controls.syncing = False
        self._set_channel_dirty(channel, True)
        self._update_command_display(channel)

    def _slider_changed(self, channel: int, slider_value: int) -> None:
        controls = self.channel_controls[channel]
        if controls.syncing:
            return
        controls.syncing = True
        try:
            controls.target_spin.setValue(slider_value / SLIDER_UNITS_PER_VOLT)
        finally:
            controls.syncing = False
        self._set_channel_dirty(channel, True)
        self._update_command_display(channel)

    def _mark_channel_dirty(self, channel: int) -> None:
        controls = self.channel_controls[channel]
        if not controls.syncing:
            self._set_channel_dirty(channel, True)

    def _set_channel_dirty(self, channel: int, dirty: bool) -> None:
        controls = self.channel_controls[channel]
        controls.dirty = dirty
        controls.apply_button.setText("Apply *" if dirty else "Apply")
        if dirty:
            self._set_state_label(controls, "Pending", "pending")
        elif controls.last_code is not None:
            self._set_state_label(controls, "Applied", "applied")

    @staticmethod
    def _set_state_label(controls: ChannelControls, text: str, state: str) -> None:
        controls.state_label.setText(text)
        controls.state_label.setProperty("state", state)
        controls.state_label.style().unpolish(controls.state_label)
        controls.state_label.style().polish(controls.state_label)

    @staticmethod
    def _editor_is_active(controls: ChannelControls) -> bool:
        return controls.target_spin.hasFocus() or controls.target_spin.lineEdit().hasFocus()

    def _command_code(self, channel: int) -> int:
        controls = self.channel_controls[channel]
        return bias_voltage_to_code(
            controls.target_spin.value(),
            self.dac_vdd_spin.value(),
            self._gain(),
            self._maximum_bias(),
        )

    def _update_command_display(self, channel: int) -> None:
        controls = self.channel_controls[channel]
        code = self._command_code(channel)
        dac_voltage = dac_code_to_voltage(code, self.dac_vdd_spin.value())
        realized = code_to_estimated_bias(
            code,
            self.dac_vdd_spin.value(),
            self._gain(),
            self._maximum_bias(),
        )
        controls.command_value.setText("{:.4f} V".format(dac_voltage))
        controls.command_code.setText("{} / 0x{:03X}".format(code, code))

        if controls.last_estimated_bias is None:
            controls.delta_label.setText("quantized {:.3f} V".format(realized))
        else:
            difference_mv = (controls.last_estimated_bias - controls.target_spin.value()) * 1000
            controls.delta_label.setText("Δ {:+.1f} mV".format(difference_mv))

    def _apply_if_live(self, channel: int) -> None:
        if self.live_slider_check.isChecked() and self.channel_controls[channel].dirty:
            self.apply_channel(channel)

    def stage_all_targets(self) -> None:
        value = self.global_target_spin.value()
        for controls in self.channel_controls:
            controls.target_spin.setValue(value)
            self._set_channel_dirty(controls.channel, True)
            self._update_command_display(controls.channel)
        self.status_message.setText("Staged {:.3f} V for all channels; press Apply all".format(value))

    # ----- Command application ------------------------------------------

    def apply_channel(self, channel: int) -> None:
        code = self._command_code(channel)
        parameters = {
            "channel": channel,
            "code": code,
            "vref": 0,
            "gain": 0,
            "power_down": 0,
        }
        if self.send_command("channel:{}".format(channel), "set_channel", parameters):
            self._set_state_label(self.channel_controls[channel], "Writing…", "pending")
            self.status_message.setText(
                "Applying {:.3f} V to {}...".format(
                    self.channel_controls[channel].target_spin.value(), CHANNEL_NAMES[channel]
                )
            )

    def apply_all(self) -> None:
        codes = [self._command_code(channel) for channel in range(8)]
        parameters = {
            "codes": codes,
            "vrefs": [0] * 8,
            "gains": [0] * 8,
            "power_downs": [0] * 8,
        }
        if self.send_command("apply", "apply_state", parameters):
            for controls in self.channel_controls:
                self._set_state_label(controls, "Writing…", "pending")
            self.status_message.setText("Applying all eight SiPM bias setpoints...")

    def zero_all(self) -> None:
        for controls in self.channel_controls:
            controls.target_spin.setValue(0.0)
            self._set_channel_dirty(controls.channel, True)
            self._update_command_display(controls.channel)
        if self.send_command("zero", "zero_all"):
            self.status_message.setText("Zeroing all eight outputs...")

    # ----- Ports and connection -----------------------------------------

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

    def toggle_connection(self) -> None:
        if self.connected_flag or self.worker is not None:
            self.disconnect_device()
            return
        if self.demo:
            self._on_connected(self._demo_info())
            return

        port = self.port_combo.currentData()
        if not port:
            QMessageBox.information(
                self,
                "No serial port",
                "Connect the Tiny 2040 with a USB data cable and press Refresh.",
            )
            return

        self._set_connection_badge("Connecting…", "statusWarn")
        self.connect_button.setEnabled(False)
        self.status_message.setText("Opening {}...".format(port))
        self.worker = SerialWorker(str(port), self)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.response.connect(self._handle_response)
        self.worker.request_failed.connect(self._handle_request_failure)
        self.worker.start()

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
            self._set_connection_badge("Connected", "statusOn")
            self.status_message.setText("Tiny 2040 and MCP47FEB28 are online")
        else:
            self._set_connection_badge("DAC missing", "statusWarn")
            self.status_message.setText("Tiny 2040 connected, but DAC address 0x60 is missing")

        self._set_write_controls_enabled(self.dac_present)
        for controls in self.channel_controls:
            controls.last_code = None
            controls.last_estimated_bias = None
        self.clear_history()
        if self.dac_present:
            self.request_state()
        self._update_poll_timer()

    def _on_disconnected(self, reason: str) -> None:
        self.connected_flag = False
        self.dac_present = False
        self.pending_tags.clear()
        self.worker = None
        self.poll_timer.stop()
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Connect")
        self.refresh_ports_button.setEnabled(True)
        self.port_combo.setEnabled(True)
        self._set_connection_badge("Disconnected", "statusOff")
        self._set_write_controls_enabled(False)
        self.status_message.setText(reason)
        self._update_poll_status()

    def _set_connection_badge(self, text: str, object_name: str) -> None:
        self.connection_badge.setText(text)
        self.connection_badge.setObjectName(object_name)
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)

    def _set_write_controls_enabled(self, enabled: bool) -> None:
        self.apply_all_button.setEnabled(enabled)
        self.header_zero_button.setEnabled(enabled)
        for controls in self.channel_controls:
            controls.apply_button.setEnabled(enabled)

    # ----- Request routing ----------------------------------------------

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
            QTimer.singleShot(
                50,
                lambda: self._run_demo_command(tag, command, parameters or {}),
            )
        elif self.worker is not None:
            self.worker.submit(tag, command, parameters or {}, timeout)
        self._update_poll_status()
        return tag

    def _handle_response(self, tag: str, result: object) -> None:
        self.pending_tags.discard(tag)
        data = dict(result) if isinstance(result, dict) else {}
        prefix = tag.split(":", 1)[0]

        if prefix == "state":
            self._apply_state(data)
        elif prefix == "channel":
            parts = tag.split(":")
            channel = int(parts[1]) if len(parts) >= 3 else -1
            if 0 <= channel < 8:
                self._set_channel_dirty(channel, False)
            if "state" in data:
                self._apply_state(data["state"])
            self.status_message.setText("{} applied".format(CHANNEL_NAMES[channel]))
        elif prefix in ("apply", "zero"):
            for channel in range(8):
                self._set_channel_dirty(channel, False)
            if "state" in data:
                self._apply_state(data["state"])
            self.status_message.setText("All channel outputs updated")
        self._update_poll_status()

    def _handle_request_failure(self, tag: str, message: str) -> None:
        self.pending_tags.discard(tag)
        prefix = tag.split(":", 1)[0]
        if prefix == "channel":
            parts = tag.split(":")
            channel = int(parts[1]) if len(parts) >= 3 else -1
            if 0 <= channel < 8:
                self._set_state_label(self.channel_controls[channel], "Write failed", "error")
        elif prefix in ("apply", "zero"):
            for controls in self.channel_controls:
                self._set_state_label(controls, "Write failed", "error")
        self.status_message.setText(message)
        self._update_poll_status()

    # ----- Polling and readback -----------------------------------------

    def _update_poll_timer(self) -> None:
        if not hasattr(self, "poll_timer"):
            return
        self.poll_timer.setInterval(self.poll_interval_spin.value())
        if self.connected_flag and self.dac_present:
            self.poll_timer.start()
        else:
            self.poll_timer.stop()
        self._update_poll_status()

    def _update_poll_status(self) -> None:
        if hasattr(self, "poll_timer") and self.poll_timer.isActive():
            text = "Readback every {} ms".format(self.poll_interval_spin.value())
            if self.pending_tags:
                text += "  •  request pending"
        else:
            text = "Polling stopped"
        self.poll_status.setText(text)

    def _poll_tick(self) -> None:
        if not self.pending_tags:
            self.request_state()

    def request_state(self) -> None:
        self.send_command("state", "get_state")

    def _apply_state(
        self,
        state: dict[str, Any],
        record_sample: bool = True,
        preserve_dirty: bool = True,
    ) -> None:
        channels = state.get("channels", [])
        if len(channels) != 8:
            self.status_message.setText("Firmware returned an invalid channel count")
            return
        self.last_state = state

        estimated_values: list[float] = []
        dac_values: list[float] = []
        codes: list[int] = []

        for item in channels:
            channel = int(item["channel"])
            controls = self.channel_controls[channel]
            initialize_target_from_device = controls.last_code is None
            code = min(max(int(item["code"]), 0), DAC_MAX_CODE)
            vref = int(item.get("vref", 0))
            dac_gain = int(item.get("gain", 0))
            power_down = int(item.get("power_down", 0))
            wiperlock = int(item.get("wiperlock", 0))

            dac_voltage = dac_code_to_voltage(code, self.dac_vdd_spin.value())
            estimated = code_to_estimated_bias(
                code,
                self.dac_vdd_spin.value(),
                self._gain(),
                self._maximum_bias(),
            )
            if power_down != 0:
                estimated = 0.0

            controls.last_code = code
            controls.last_estimated_bias = estimated
            controls.readback_value.setText("{:.4f} V".format(dac_voltage))
            controls.readback_code.setText("{} / 0x{:03X}".format(code, code))
            controls.estimated_bias.setText("≈ {:.3f} V".format(estimated))

            standard_configuration = vref == 0 and dac_gain == 0 and power_down == 0
            if wiperlock != 0:
                self._set_state_label(controls, "Locked", "error")
            elif not standard_configuration and not controls.dirty:
                self._set_state_label(controls, "Config differs", "error")
            elif not controls.dirty:
                self._set_state_label(controls, "Applied", "applied")

            if (
                (not preserve_dirty or not controls.dirty)
                and not self._editor_is_active(controls)
                and standard_configuration
                and initialize_target_from_device
            ):
                controls.syncing = True
                try:
                    target = min(estimated, self._maximum_bias())
                    controls.target_spin.setValue(target)
                    controls.slider.setValue(
                        int(round(target * SLIDER_UNITS_PER_VOLT))
                    )
                finally:
                    controls.syncing = False
                controls.dirty = False
                controls.apply_button.setText("Apply")

            self._update_command_display(channel)
            codes.append(code)
            dac_values.append(dac_voltage)
            estimated_values.append(estimated if standard_configuration else float("nan"))

        if record_sample:
            self._record_history_sample(codes, dac_values, estimated_values)
        self.status_message.setText("DAC register readback updated")

    # ----- History graph and CSV ----------------------------------------

    def _record_history_sample(
        self,
        codes: list[int],
        dac_values: list[float],
        estimated_values: list[float],
    ) -> None:
        elapsed = time.monotonic() - self.history_epoch
        self.history_elapsed.append(elapsed)
        self.history_wall_time.append(datetime.now().isoformat(timespec="milliseconds"))
        for channel in range(8):
            self.history_codes[channel].append(codes[channel])
            self.history_dac[channel].append(dac_values[channel])
            self.history_bias[channel].append(estimated_values[channel])
        self.sample_count_label.setText("{} samples".format(len(self.history_elapsed)))
        self._update_plot()

    def _history_slice_start(self, elapsed: list[float]) -> int:
        window = float(self.history_window_combo.currentData() or 0.0)
        if window <= 0 or not elapsed:
            return 0
        threshold = elapsed[-1] - window
        return bisect.bisect_left(elapsed, threshold)

    def _series_hovered(self, channel: int, point: QPointF, entered: bool) -> None:
        if entered:
            self.graph_cursor_label.setText(
                "{}  {:.3f} V at {:.2f} s".format(
                    CHANNEL_NAMES[channel], point.y(), point.x()
                )
            )
        else:
            self.graph_cursor_label.setText("Hover a trace for its exact value")

    def _update_plot(self) -> None:
        if not hasattr(self, "plot_curves") or self.pause_plot_check.isChecked():
            return
        if not self.history_elapsed:
            for series in self.plot_curves:
                series.clear()
            return

        elapsed = list(self.history_elapsed)
        start = self._history_slice_start(elapsed)
        visible_x = elapsed[start:]
        visible_biases: list[float] = []
        for channel, series in enumerate(self.plot_curves):
            if self.channel_controls[channel].plot_check.isChecked():
                series.setVisible(True)
                values = list(self.history_bias[channel])[start:]
                points = [
                    QPointF(x_value, y_value)
                    for x_value, y_value in zip(visible_x, values)
                    if math.isfinite(y_value)
                ]
                series.replace(points)
                visible_biases.extend(point.y() for point in points)
            else:
                series.setVisible(False)

        if len(visible_x) >= 2:
            lower_x = float(visible_x[0])
            upper_x = float(visible_x[-1])
            if upper_x <= lower_x:
                upper_x = lower_x + 1.0
            self.time_axis.setRange(lower_x, upper_x)
        elif visible_x:
            self.time_axis.setRange(max(0.0, visible_x[0] - 1.0), visible_x[0] + 1.0)

        if self.scale_to_source_check.isChecked():
            upper = max(1.0, self.main_source_spin.value() * 1.04)
            self.bias_axis.setRange(0.0, upper)
        elif visible_biases:
            lower = min(visible_biases)
            upper = max(visible_biases)
            span = upper - lower
            padding = max(0.25, span * 0.08)
            if span < 0.001:
                padding = max(0.5, abs(upper) * 0.05)
            self.bias_axis.setRange(max(0.0, lower - padding), upper + padding)

    def clear_history(self) -> None:
        self.history_epoch = time.monotonic()
        self.history_elapsed.clear()
        self.history_wall_time.clear()
        for channel in range(8):
            self.history_bias[channel].clear()
            self.history_dac[channel].clear()
            self.history_codes[channel].clear()
        self.sample_count_label.setText("0 samples")
        for series in self.plot_curves:
            series.clear()
        self.time_axis.setRange(0.0, float(self.history_window_combo.currentData() or 30.0))
        self.graph_cursor_label.setText("Hover a trace for its exact value")

    def export_history_csv(self) -> None:
        if not self.history_elapsed:
            QMessageBox.information(self, "No history", "No readback samples have been recorded yet.")
            return
        default_name = "sipm_bias_history_{}.csv".format(
            datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export bias history",
            default_name,
            "CSV files (*.csv)",
        )
        if not filename:
            return

        path = Path(filename)
        headers = ["timestamp", "elapsed_s"]
        for name in CHANNEL_NAMES:
            headers.extend(
                [
                    "{}_estimated_bias_v".format(name),
                    "{}_dac_readback_v".format(name),
                    "{}_dac_code".format(name),
                ]
            )

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for index, elapsed in enumerate(self.history_elapsed):
                row: list[Any] = [self.history_wall_time[index], "{:.6f}".format(elapsed)]
                for channel in range(8):
                    bias = self.history_bias[channel][index]
                    row.extend(
                        [
                            "" if not math.isfinite(bias) else "{:.6f}".format(bias),
                            "{:.6f}".format(self.history_dac[channel][index]),
                            self.history_codes[channel][index],
                        ]
                    )
                writer.writerow(row)
        self.status_message.setText("Exported {} samples to {}".format(len(self.history_elapsed), path.name))

    # ----- Demo firmware -------------------------------------------------

    def _demo_info(self) -> dict[str, Any]:
        return {
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

    def _make_demo_state(self) -> dict[str, Any]:
        gain = amplifier_gain(DEFAULT_R_FEEDBACK_KOHM, DEFAULT_R_GROUND_KOHM)
        maximum = usable_bias_maximum(
            DEFAULT_MAIN_SOURCE_V,
            DEFAULT_DAC_VDD_V,
            gain,
            DEFAULT_OUTPUT_HEADROOM_V,
        )
        demo_biases = (5.0, 7.5, 10.0, 12.5, 15.0, 2.5, 17.5, 0.0)
        channels = []
        for channel, bias in enumerate(demo_biases):
            channels.append(
                {
                    "channel": channel,
                    "name": CHANNEL_NAMES[channel],
                    "code": bias_voltage_to_code(
                        min(bias, maximum), DEFAULT_DAC_VDD_V, gain, maximum
                    ),
                    "vref": 0,
                    "gain": 0,
                    "power_down": 0,
                    "wiperlock": 0,
                }
            )
        return {
            "channels": channels,
            "status": {"por_seen": False, "eeprom_busy": False},
            "uptime_ms": 0,
        }

    def _run_demo_command(
        self, tag: str, command: str, parameters: dict[str, Any]
    ) -> None:
        try:
            if command == "get_state":
                self.demo_state["uptime_ms"] = int(
                    (time.monotonic() - self.demo_started) * 1000
                )
                result: dict[str, Any] = {
                    **self.demo_state,
                    "channels": [dict(item) for item in self.demo_state["channels"]],
                }
            elif command == "set_channel":
                channel = int(parameters["channel"])
                item = self.demo_state["channels"][channel]
                item["code"] = int(parameters["code"])
                item["vref"] = int(parameters.get("vref", 0))
                item["gain"] = int(parameters.get("gain", 0))
                item["power_down"] = int(parameters.get("power_down", 0))
                result = {
                    "state": {
                        **self.demo_state,
                        "channels": [dict(entry) for entry in self.demo_state["channels"]],
                    }
                }
            elif command == "apply_state":
                for channel, item in enumerate(self.demo_state["channels"]):
                    item["code"] = int(parameters["codes"][channel])
                    item["vref"] = int(parameters["vrefs"][channel])
                    item["gain"] = int(parameters["gains"][channel])
                    item["power_down"] = int(parameters["power_downs"][channel])
                result = {
                    "state": {
                        **self.demo_state,
                        "channels": [dict(entry) for entry in self.demo_state["channels"]],
                    }
                }
            elif command == "zero_all":
                for item in self.demo_state["channels"]:
                    item["code"] = 0
                    item["vref"] = 0
                    item["gain"] = 0
                    item["power_down"] = 0
                result = {
                    "state": {
                        **self.demo_state,
                        "channels": [dict(entry) for entry in self.demo_state["channels"]],
                    }
                }
            else:
                raise ValueError("demo command '{}' is not implemented".format(command))
            self._handle_response(tag, result)
        except Exception as error:
            self._handle_request_failure(tag, str(error))

    # ----- Window lifecycle ---------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(1200)
        event.accept()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run with a simulated Tiny 2040 and DAC",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    application = QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    application.setOrganizationName("OpenAI")
    window = BiasControlWindow(demo=arguments.demo)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
