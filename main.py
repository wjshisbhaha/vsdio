import sys
import json
import math
import re
import shutil
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import Qt, QEvent, QPoint, QPointF, QRectF, QSize, QTimer, QDateTime, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QPolygonF, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from device_controllers import (
    AcsMotionController, AxisStatus, CH375ShutterClient, PositionPoller, ScanPoint
)

from serial_controller.serial_gui import Window as SerialControlPage
from galvo_scan import GalvoScanSettings, GalvoScanRunner, load_galvo_master
from hologram_report import export_report
from hologram_player import HologramSettings, HologramPlayer, ShutterTimeline, load_hologram_excel, schedule_offsets, column_sequences

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "app_config.json"

@dataclass(frozen=True)
class ScanJobPoint:
    x: float
    y: float
    image_path: str = ""
    exposure_ms: int | None = None
    stable_ms: int | None = None
    shutter_hold_ms: int | None = None
    shutter_no: int | None = None


class Dot(QLabel):
    def __init__(self, color="#aab0b7", diameter=10, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._diameter = diameter
        self.setFixedSize(diameter + 2, diameter + 2)

    def set_color(self, color: str):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(1, 1, self._diameter, self._diameter)


class StatusText(QWidget):
    def __init__(self, text="未连接", color="#aab0b7", text_color="#7b838d", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.dot = Dot(color, 10)
        self.label = QLabel(text)
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        self.set_state(text, color, text_color)

    def set_state(self, text, color, text_color=None):
        self.label.setText(text)
        self.dot.set_color(color)
        self.label.setStyleSheet(f"color:{text_color or color}; font-weight:600;")


class TitleBar(QFrame):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.drag_pos = QPoint()
        self.setObjectName("titleBar")
        self.setFixedHeight(50)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 14, 0)
        layout.setSpacing(10)
        logo = QLabel("PI")
        logo.setObjectName("logoText")
        logo.setFixedWidth(44)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("L-412 Linear Stage Controller")
        title.setObjectName("windowTitle")
        layout.addWidget(logo)
        layout.addWidget(title)
        layout.addStretch()
        for text, slot, name in (
            ("−", window.showMinimized, "windowControlButton"),
            ("□", self.toggle_maximize, "windowControlButton"),
            ("×", window.close, "closeButton"),
        ):
            btn = QPushButton(text)
            btn.setObjectName(name)
            btn.setFixedSize(46, 42)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def toggle_maximize(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and not self.window.isMaximized():
            self.window.move(event.globalPosition().toPoint() - self.drag_pos)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()


class IconFactory:
    @staticmethod
    def make(kind: str, color="#ffffff", size=22):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(color), 2))
        s = float(size)
        if kind == "home":
            p.drawPolyline(QPolygonF([QPointF(s*.15, s*.48), QPointF(s*.5, s*.18), QPointF(s*.85, s*.48)]))
            p.drawRect(QRectF(s*.26, s*.46, s*.48, s*.38))
        elif kind == "scan":
            for v in (.25, .5, .75):
                p.drawLine(int(s*v), int(s*.2), int(s*v), int(s*.8))
                p.drawLine(int(s*.2), int(s*v), int(s*.8), int(s*v))
        elif kind == "shutter":
            p.drawRoundedRect(QRectF(s*.2, s*.22, s*.6, s*.56), 3, 3)
            p.drawLine(int(s*.36), int(s*.22), int(s*.36), int(s*.78))
        elif kind == "gear":
            p.drawEllipse(QRectF(s*.25, s*.25, s*.5, s*.5))
            p.drawEllipse(QRectF(s*.42, s*.42, s*.16, s*.16))
        elif kind == "io":
            p.drawEllipse(QRectF(s*.18, s*.18, s*.64, s*.64))
        elif kind == "log":
            p.drawRoundedRect(QRectF(s*.24, s*.16, s*.52, s*.68), 3, 3)
            for y in (.34, .5, .66):
                p.drawLine(int(s*.34), int(s*y), int(s*.66), int(s*y))
        else:
            p.drawEllipse(QRectF(s*.18, s*.18, s*.64, s*.64))
            p.drawLine(int(s*.5), int(s*.43), int(s*.5), int(s*.66))
        p.end()
        return QIcon(pm)


class NavButton(QPushButton):
    def __init__(self, text, icon):
        super().__init__(text)
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setIcon(IconFactory.make(icon))
        self.setIconSize(QSize(22, 22))
        self.setFixedHeight(52)


class LimitCell(QFrame):
    def __init__(self, title, state="未知"):
        super().__init__()
        self.setMinimumHeight(62)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("limitTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout()
        row.addStretch()
        self.dot = Dot()
        self.state = QLabel(state)
        row.addWidget(self.dot)
        row.addWidget(self.state)
        row.addStretch()
        layout.addWidget(self.title)
        layout.addLayout(row)
        self.set_state(state, False, unknown=True)

    def set_state(self, text: str, active: bool, unknown=False):
        self.state.setText(text)
        if unknown:
            color = "#aab0b7"
            object_name = "limitCell"
            label_name = "limitState"
        elif active:
            color = "#ef353b"
            object_name = "limitCellActive"
            label_name = "limitStateActive"
        else:
            color = "#45bd63"
            object_name = "limitCell"
            label_name = "limitState"
        self.setObjectName(object_name)
        self.state.setObjectName(label_name)
        self.dot.set_color(color)
        for widget in (self, self.state):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class MotionButton(QPushButton):
    def __init__(self, symbol, caption):
        super().__init__()
        self.setObjectName("motionButton")
        self.setMinimumHeight(64)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 7, 3, 6)
        layout.setSpacing(2)
        sym = QLabel(symbol)
        sym.setObjectName("motionSymbol")
        sym.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap = QLabel(caption)
        cap.setObjectName("motionCaption")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(sym)
        layout.addWidget(cap)


class AxisCard(QFrame):
    commandRequested = pyqtSignal(str, str)

    def __init__(self, axis, accent):
        super().__init__()
        self.axis = axis
        self.accent = accent
        self.setObjectName("card")
        self.setMinimumWidth(345)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 18, 14)
        root.setSpacing(9)

        header = QHBoxLayout()
        title = QLabel(f"{axis} 轴")
        title.setStyleSheet(f"font-size:23px; font-weight:700; color:{accent};")
        self.axis_status = StatusText()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.axis_status)
        root.addLayout(header)

        current_title = QLabel("当前位置（ACS 实时读取）")
        current_title.setObjectName("fieldTitle")
        root.addWidget(current_title)
        pos_row = QHBoxLayout()
        pos_row.addStretch()
        self.current_label = QLabel("0.000")
        self.current_label.setStyleSheet(f"font-size:34px; font-weight:700; color:{accent};")
        pos_row.addWidget(self.current_label)
        pos_row.addStretch()
        pos_row.addWidget(QLabel("mm"))
        root.addLayout(pos_row)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("目标位置"))
        target_row.addStretch()
        self.target = QDoubleSpinBox()
        self.target.setRange(-9999, 9999)
        self.target.setDecimals(6)
        self.target.setFixedWidth(145)
        target_row.addWidget(self.target)
        target_row.addWidget(QLabel("mm"))
        self.move_btn = QPushButton("移动到")
        self.move_btn.setObjectName("primaryButton")
        self.move_btn.clicked.connect(lambda: self.commandRequested.emit(self.axis, "move_absolute"))
        target_row.addWidget(self.move_btn)
        root.addLayout(target_row)

        setpos_title = QLabel("SetFPosition（更改当前轴位置，不移动）")
        setpos_title.setObjectName("smallFieldLabel")
        root.addWidget(setpos_title)
        setpos_row = QHBoxLayout()
        setpos_row.setSpacing(6)
        self.setpos_value = QDoubleSpinBox()
        self.setpos_value.setRange(-9999, 9999)
        self.setpos_value.setDecimals(6)
        self.setpos_value.setFixedWidth(150)
        setpos_row.addWidget(self.setpos_value)
        setpos_row.addWidget(QLabel("mm"))
        self.setpos_btn = QPushButton("SetFPosition")
        self.setpos_btn.setObjectName("smallButton")
        self.setpos_btn.setMinimumWidth(115)
        self.setpos_btn.clicked.connect(lambda: self.commandRequested.emit(self.axis, "set_f_position"))
        setpos_row.addWidget(self.setpos_btn)
        root.addLayout(setpos_row)

        enable_row = QHBoxLayout()
        self.enable_btn = QPushButton("启动轴")
        self.enable_btn.setObjectName("greenButton")
        self.disable_btn = QPushButton("关闭轴")
        self.disable_btn.setObjectName("disabledLikeButton")
        self.enable_btn.clicked.connect(lambda: self.commandRequested.emit(self.axis, "enable_axis"))
        self.disable_btn.clicked.connect(lambda: self.commandRequested.emit(self.axis, "disable_axis"))
        enable_row.addWidget(self.enable_btn)
        enable_row.addWidget(self.disable_btn)
        root.addLayout(enable_row)

        limits = QHBoxLayout()
        limits.setSpacing(8)
        self.neg_limit = LimitCell("- 硬件限位")
        self.motor_state = LimitCell("轴状态")
        self.pos_limit = LimitCell("+ 硬件限位")
        limits.addWidget(self.neg_limit)
        limits.addWidget(self.motor_state)
        limits.addWidget(self.pos_limit)
        root.addLayout(limits)

        buttons = QHBoxLayout()
        buttons.setSpacing(7)
        for symbol, caption, command in (
            ("<<", "- 大步进", "large_negative"),
            ("<", "- 步进", "step_negative"),
            ("■", "停止", "stop"),
            (">", "+ 步进", "step_positive"),
            (">>", "+ 大步进", "large_positive"),
        ):
            btn = MotionButton(symbol, caption)
            btn.clicked.connect(lambda checked=False, c=command: self.commandRequested.emit(self.axis, c))
            buttons.addWidget(btn)
        root.addLayout(buttons)

        jog_title = QLabel("连续运动")
        jog_title.setObjectName("sectionMiniTitle")
        root.addWidget(jog_title)
        jog = QHBoxLayout()
        for text, command in (("◀\n负方向", "jog_negative"), ("停止", "stop"), ("▶\n正方向", "jog_positive")):
            btn = QPushButton(text)
            btn.setObjectName("jogButton")
            btn.setMinimumHeight(58)
            btn.clicked.connect(lambda checked=False, c=command: self.commandRequested.emit(self.axis, c))
            jog.addWidget(btn)
        root.addLayout(jog)
        self.set_connected(False)

    def set_connected(self, connected: bool):
        if connected:
            self.axis_status.set_state("未启动", "#f0a020", "#d58a12")
        else:
            self.axis_status.set_state("未连接", "#aab0b7", "#7b838d")
        self.enable_btn.setEnabled(connected)
        self.disable_btn.setEnabled(connected)
        self.move_btn.setEnabled(connected)
        self.setpos_btn.setEnabled(connected)
        if not connected:
            self.neg_limit.set_state("未知", False, unknown=True)
            self.pos_limit.set_state("未知", False, unknown=True)
            self.motor_state.set_state("未连接", False, unknown=True)

    def set_runtime_state(self, state: AxisStatus):
        self.current_label.setText(f"{state.position:.6f}")
        self.neg_limit.set_state("触发" if state.negative_limit else "未触发", state.negative_limit)
        self.pos_limit.set_state("触发" if state.positive_limit else "未触发", state.positive_limit)
        self.motor_state.set_state("已启动" if state.enabled else "未启动", not state.enabled)
        if state.enabled:
            text = "运动中" if state.moving else "正常"
            self.axis_status.set_state(text, "#45bd63", "#36b757")
            self.enable_btn.setEnabled(False)
            self.disable_btn.setEnabled(True)
        else:
            self.axis_status.set_state("未启动", "#f0a020", "#d58a12")
            self.enable_btn.setEnabled(True)
            self.disable_btn.setEnabled(False)


class SC20Card(QFrame):
    commandRequested = pyqtSignal(str)
    refreshRequested = pyqtSignal()
    connectRequested = pyqtSignal(str, bool)

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        root = QVBoxLayout(self)
        root.setContentsMargins(15, 13, 15, 13)
        root.setSpacing(5)

        header = QHBoxLayout()
        title = QLabel("CH375 三路快门控制")
        title.setObjectName("cardTitle")
        self.status = StatusText()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.status)
        root.addLayout(header)

        conn_box = QFrame()
        conn_box.setObjectName("innerBox")
        conn = QGridLayout(conn_box)
        conn.setContentsMargins(8, 8, 8, 8)
        conn.setHorizontalSpacing(6)
        conn.setVerticalSpacing(6)
        self.service_url = QLineEdit("http://127.0.0.1:37501")
        self.service_url.setPlaceholderText("32 位快门服务地址")
        self.refresh_btn = QPushButton("检测")
        self.refresh_btn.setObjectName("smallButton")
        self.simulate = QCheckBox("主程序模拟模式")
        self.simulate.setChecked(False)
        self.connect_btn = QPushButton("连接")
        self.connect_btn.setObjectName("smallButton")
        conn.addWidget(QLabel("32 位快门服务"), 0, 0, 1, 3)
        conn.addWidget(self.service_url, 1, 0, 1, 2)
        conn.addWidget(self.refresh_btn, 1, 2)
        conn.addWidget(self.simulate, 2, 0, 1, 2)
        conn.addWidget(self.connect_btn, 2, 2)
        root.addWidget(conn_box)
        self.refresh_btn.clicked.connect(self.refreshRequested)
        self.connect_btn.clicked.connect(
            lambda: self.connectRequested.emit(
                self.service_url.text().strip(), self.simulate.isChecked()
            )
        )

        manual_title = QLabel("手动控制")
        manual_title.setObjectName("sectionMiniTitle")
        root.addWidget(manual_title)

        manual = QGridLayout()
        manual.setHorizontalSpacing(7)
        manual.setVerticalSpacing(7)
        self.channel_buttons = []
        for col, (label, code, command) in enumerate((
            ("快门 1", "0x8A", "toggle_1"),
            ("快门 2", "0x4A", "toggle_2"),
            ("快门 3", "0x2A", "toggle_3"),
        )):
            btn = QPushButton(f"{label}\n{code}")
            btn.setObjectName("shutterChannelButton")
            btn.setCheckable(True)
            btn.setProperty("active", False)
            btn.setMinimumHeight(44)
            btn.clicked.connect(lambda checked=False, c=command: self.commandRequested.emit(c))
            manual.addWidget(btn, 0, col)
            self.channel_buttons.append(btn)
        close_btn = QPushButton("全部关闭    0x0A")
        close_btn.setObjectName("shutterCloseButton")
        close_btn.setMinimumHeight(34)
        close_btn.clicked.connect(lambda: self.commandRequested.emit("close"))
        manual.addWidget(close_btn, 1, 0, 1, 3)
        root.addLayout(manual)

        timer_box = QFrame()
        timer_box.setObjectName("innerBox")
        timer_box.setMinimumHeight(138)
        form = QGridLayout(timer_box)
        form.setContentsMargins(8, 7, 8, 7)
        form.setHorizontalSpacing(5)
        form.setVerticalSpacing(4)
        timer_title = QLabel("定时控制")
        timer_title.setObjectName("sectionMiniTitle")
        form.addWidget(timer_title, 0, 0, 1, 3)
        self.timer_shutter = QComboBox()
        self.timer_shutter.addItems(["快门 1（0x8A）", "快门 2（0x4A）", "快门 3（0x2A）"])
        form.addWidget(self.timer_shutter, 1, 0, 1, 3)
        for col, text in enumerate(("延时", "保持", "关闭")):
            lab = QLabel(text + " ms")
            lab.setObjectName("timerFieldLabel")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            form.addWidget(lab, 2, col)
        self.delay = self.make_spin(100)
        self.hold = self.make_spin(500)
        self.delay_close = self.make_spin(100)
        form.addWidget(self.delay, 3, 0)
        form.addWidget(self.hold, 3, 1)
        form.addWidget(self.delay_close, 3, 2)
        timer_buttons = QHBoxLayout()
        start = QPushButton("开始定时")
        stop = QPushButton("停止并关闭")
        start.setObjectName("primaryButton")
        stop.setObjectName("disabledLikeButton")
        start.setFixedHeight(34)
        stop.setFixedHeight(34)
        start.clicked.connect(lambda: self.commandRequested.emit("start_timer"))
        stop.clicked.connect(lambda: self.commandRequested.emit("stop_timer"))
        timer_buttons.addWidget(start)
        timer_buttons.addWidget(stop)
        form.addLayout(timer_buttons, 4, 0, 1, 3)
        root.addWidget(timer_box)

        status_box = QFrame()
        status_box.setObjectName("shutterStateBox")
        status_layout = QVBoxLayout(status_box)
        status_layout.setContentsMargins(10, 5, 10, 5)
        status_layout.setSpacing(2)
        self.state_label = QLabel("状态：全部关闭")
        self.state_label.setObjectName("shutterStateLabel")
        self.countdown = QLabel("服务：未连接")
        self.countdown.setObjectName("shutterCountdownLabel")
        self.countdown.setWordWrap(True)
        status_layout.addWidget(self.state_label)
        status_layout.addWidget(self.countdown)
        root.addWidget(status_box)
        root.addStretch()

    @staticmethod
    def make_spin(value):
        spin = QDoubleSpinBox()
        spin.setRange(0, 99999999)
        spin.setDecimals(3)
        spin.setValue(value)
        spin.setDecimals(0)
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setSuffix("")
        spin.setMinimumWidth(66)
        spin.setFixedHeight(28)
        return spin

    def selected_shutter(self) -> int:
        return self.timer_shutter.currentIndex() + 1

    def set_connected(self, connected, text=""):
        if connected:
            self.status.set_state("已连接", "#45bd63", "#36b757")
            self.connect_btn.setText("断开")
            self.service_url.setEnabled(False)
            self.simulate.setEnabled(False)
        else:
            self.status.set_state("未连接", "#aab0b7", "#7b838d")
            self.connect_btn.setText("连接")
            self.service_url.setEnabled(True)
            self.simulate.setEnabled(True)
        if text:
            self.countdown.setText(text)

    def selected_manual_shutters(self):
        return [i for i, button in enumerate(self.channel_buttons, start=1) if button.isChecked()]

    def set_open(self, opened, shutter_no=0, shutter_numbers=None):
        selected = set(shutter_numbers or ([] if not opened else [shutter_no]))
        for index, button in enumerate(self.channel_buttons, start=1):
            active = bool(opened and index in selected)
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)
            button.setProperty("active", active)
            button.style().unpolish(button)
            button.style().polish(button)
        if opened and selected:
            text = "、".join(str(n) for n in sorted(selected))
            self.state_label.setText(f"状态：快门 {text} 已打开")
            self.state_label.setProperty("opened", True)
        else:
            self.state_label.setText("状态：全部关闭")
            self.state_label.setProperty("opened", False)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)


class ScanControlCard(QFrame):
    commandRequested = pyqtSignal(str)
    parametersChanged = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        root = QVBoxLayout(self)
        root.setContentsMargins(9, 7, 9, 7)
        root.setSpacing(5)
        title = QLabel("扫描控制（坐标范围）")
        title.setObjectName("cardTitle")
        root.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(2)
        self.x_start = self.line("X 起始")
        self.x_end = self.line("X 结束")
        self.x_step = self.line("X 步进")
        self.y_start = self.line("Y 起始")
        self.y_end = self.line("Y 结束")
        self.y_step = self.line("Y 步进")
        fields = [
            ("X 起始 (mm)", self.x_start), ("X 结束 (mm)", self.x_end), ("X 步进 (mm)", self.x_step),
            ("Y 起始 (mm)", self.y_start), ("Y 结束 (mm)", self.y_end), ("Y 步进 (mm)", self.y_step),
        ]
        for i, (label, widget) in enumerate(fields):
            row, col = divmod(i, 3)
            widget.setMinimumHeight(28)
            grid.addWidget(self.field_cell(label, widget), row, col)
            widget.textChanged.connect(self.parametersChanged)
        root.addLayout(grid)

        self.point_count_label = QLabel("扫描点数：等待输入")
        self.point_count_label.setObjectName("scanSummaryLabel")
        self.point_count_label.setFixedHeight(68)
        root.addWidget(self.point_count_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("未开始")
        self.progress.setFixedHeight(36)
        root.addWidget(self.progress)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.start_btn = QPushButton("开始扫描")
        self.pause_btn = QPushButton("暂停扫描")
        self.stop_btn = QPushButton("停止扫描")
        self.start_btn.setObjectName("greenButton")
        self.pause_btn.setObjectName("disabledLikeButton")
        self.stop_btn.setObjectName("redButton")
        self.start_btn.clicked.connect(lambda: self.commandRequested.emit("start"))
        self.pause_btn.clicked.connect(lambda: self.commandRequested.emit("pause"))
        self.stop_btn.clicked.connect(lambda: self.commandRequested.emit("stop"))
        row.addWidget(self.start_btn)
        row.addWidget(self.pause_btn)
        row.addWidget(self.stop_btn)
        root.addLayout(row)
        root.addStretch()
        self.set_running(False)

    @staticmethod
    def line(placeholder=""):
        edit = QLineEdit()
        edit.setPlaceholderText("手动输入 " + placeholder)
        return edit

    @staticmethod
    def field_cell(label, widget):
        cell = QWidget()
        cell.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        lab = QLabel(label)
        lab.setObjectName("smallFieldLabel")
        lab.setFixedHeight(16)
        layout.addWidget(lab)
        layout.addWidget(widget)
        return cell

    @staticmethod
    def _float(edit: QLineEdit, name: str) -> float:
        text = edit.text().strip()
        if not text:
            raise ValueError(f"请填写{name}。")
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError(f"{name}必须是数字，当前为：{text}") from exc

    @staticmethod
    def _int_ms(edit: QLineEdit, name: str) -> int:
        value = ScanControlCard._float(edit, name)
        if value < 0:
            raise ValueError(f"{name}不能小于 0。")
        return int(round(value))

    @staticmethod
    def _int_ms_value(raw, name: str) -> int:
        if raw is None or str(raw).strip() == "":
            raise ValueError(f"{name}不能为空。")
        try:
            value = float(raw)
        except Exception as exc:
            raise ValueError(f"{name}必须是数字，当前为：{raw}") from exc
        if value < 0:
            raise ValueError(f"{name}不能小于 0。")
        return int(round(value))

    def values(self, require_y_step=True):
        x_step = self._float(self.x_step, "X 步进")
        y_step = 0.0
        if require_y_step:
            y_step = self._float(self.y_step, "Y 步进")
        if x_step <= 0 or (require_y_step and y_step <= 0):
            raise ValueError("X/Y 步进必须大于 0。" if require_y_step else "X 步进必须大于 0。")
        values = {
            "x_start": self._float(self.x_start, "X 起始"),
            "x_end": self._float(self.x_end, "X 结束"),
            "x_step": x_step,
            "y_start": self._float(self.y_start, "Y 起始"),
            "y_end": self._float(self.y_end, "Y 结束"),
        }
        if require_y_step:
            values["y_step"] = y_step
        return values

    def config_values(self):
        return {
            "x_start": self.x_start.text(),
            "x_end": self.x_end.text(),
            "x_step": self.x_step.text(),
            "y_start": self.y_start.text(),
            "y_end": self.y_end.text(),
            "y_step": self.y_step.text(),
        }

    def apply_config(self, config: dict):
        for key, widget in {
            "x_start": self.x_start, "x_end": self.x_end, "x_step": self.x_step,
            "y_start": self.y_start, "y_end": self.y_end, "y_step": self.y_step,
        }.items():
            if key in config:
                widget.setText(str(config.get(key, "")))

    def set_running(self, running, stopping=False):
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running and not stopping)
        self.stop_btn.setEnabled(running and not stopping)
        if stopping:
            self.progress.setFormat("正在停止…")

    def set_progress(self, index, total):
        total = max(1, int(total))
        index = max(0, min(int(index), total))
        pct = round(index / total * 100)
        self.progress.setValue(pct)
        self.progress.setFormat(f"{index}/{total}  ({pct}%)")

    def set_point_count(self, count=None, error_text=""):
        if error_text:
            self.point_count_label.setText("扫描点数：" + error_text)
        elif count is None:
            self.point_count_label.setText("扫描点数：等待输入")
        else:
            self.point_count_label.setText(f"扫描点数：{count} 点")

    def set_y_step_required(self, required: bool):
        self.y_step.setEnabled(bool(required))
        if required:
            self.y_step.setPlaceholderText("手动输入 Y 步进")
        else:
            self.y_step.setPlaceholderText("匀速扫描无需输入 Y 步进")


class ScanSettingsCard(QFrame):
    parametersChanged = pyqtSignal()

    def __init__(self, enable_uniform=True):
        super().__init__()
        self.enable_uniform = bool(enable_uniform)
        self.setObjectName("card")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        title = QLabel("扫描设置")
        title.setObjectName("cardTitle")
        root.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        self.mode = QComboBox()
        modes = ["蛇形扫描", "栅格扫描", "文件扫描", "文件扫描plus", "匀速扫描"]
        if self.enable_uniform:
            modes.append("匀速运动曝光")
        self.mode.addItems(modes)
        self.order = QComboBox(); self.order.addItems(["先 X 后 Y", "先 Y 后 X"])
        self.speed = self.line("扫描速度")
        self.stable_ms = self.line("到位稳定时间 ms")
        self.shutter_hold_ms = self.line("快门开启保持时间 ms")
        self.scan_shutter = QComboBox(); self.scan_shutter.addItems(["快门 1（0x8A）", "快门 2（0x4A）", "快门 3（0x2A）"])
        self.display_screen = QComboBox(); self.display_screen.addItem("显示器 1")
        fields = [
            ("扫描模式", self.mode, 1),
            ("运动顺序", self.order, 1),
            ("速度 (mm/s)", self.speed, 1),
            ("稳定时间 (ms)", self.stable_ms, 1),
            ("快门保持 (ms)", self.shutter_hold_ms, 1),
            ("扫描快门", self.scan_shutter, 1),
            ("全屏显示器", self.display_screen, 2),
        ]
        row = col = 0
        for label, widget, span in fields:
            if col + span > 2:
                row += 1
                col = 0
            widget.setMinimumHeight(30)
            cell = ScanControlCard.field_cell(label, widget)
            grid.addWidget(cell, row, col, 1, span)
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.parametersChanged)
            else:
                widget.currentIndexChanged.connect(self.parametersChanged)
            col += span
            if col >= 2:
                row += 1
                col = 0
        root.addLayout(grid)

        countdown_box = QFrame()
        countdown_box.setObjectName("scanCountdownBox")
        countdown_layout = QVBoxLayout(countdown_box)
        countdown_layout.setContentsMargins(8, 6, 8, 6)
        countdown_layout.setSpacing(2)
        self.scan_phase = QLabel("扫描状态：未开始")
        self.scan_phase.setObjectName("scanCountdownTitle")
        self.stable_countdown = QLabel("稳定倒计时：--")
        self.shutter_countdown = QLabel("快门保持倒计时：--")
        self.stable_countdown.setObjectName("scanCountdownLabel")
        self.shutter_countdown.setObjectName("scanCountdownLabel")
        countdown_layout.addWidget(self.scan_phase)
        countdown_layout.addWidget(self.stable_countdown)
        countdown_layout.addWidget(self.shutter_countdown)
        countdown_box.setMaximumHeight(76)
        root.addWidget(countdown_box)
        root.addStretch()

    @staticmethod
    def line(placeholder=""):
        edit = QLineEdit()
        edit.setPlaceholderText("手动输入 " + placeholder)
        return edit

    def values(self):
        mode = self.mode.currentText()
        speed = ScanControlCard._float(self.speed, "扫描速度")
        if speed <= 0:
            raise ValueError("扫描速度必须大于 0。")
        stable_ms = 0
        if self.stable_ms.text().strip() or mode != "匀速扫描":
            stable_ms = ScanControlCard._int_ms(self.stable_ms, "稳定时间")
        shutter_hold_ms = 0
        if self.shutter_hold_ms.text().strip() or mode != "匀速扫描":
            shutter_hold_ms = ScanControlCard._int_ms(self.shutter_hold_ms, "快门保持时间")
        values = {
            "mode": mode,
            "order": self.order.currentText(),
            "speed": speed,
            "stable_ms": stable_ms,
            "shutter_hold_ms": shutter_hold_ms,
            "scan_shutter": self.scan_shutter.currentIndex() + 1,
            "display_index": max(0, self.display_screen.currentIndex()),
        }
        return values

    def config_values(self):
        values = {
            "speed": self.speed.text(),
            "stable_ms": self.stable_ms.text(),
            "shutter_hold_ms": self.shutter_hold_ms.text(),
            "mode": self.mode.currentText(),
            "order": self.order.currentText(),
            "scan_shutter": self.scan_shutter.currentIndex(),
            "display_index": self.display_screen.currentIndex(),
        }
        return values

    def apply_config(self, config: dict):
        for key, widget in {
            "speed": self.speed,
            "stable_ms": self.stable_ms,
            "shutter_hold_ms": self.shutter_hold_ms,
        }.items():
            if key in config:
                widget.setText(str(config.get(key, "")))
        if "mode" in config:
            i = self.mode.findText(str(config["mode"]))
            self.mode.setCurrentIndex(max(0, i))
        if "order" in config:
            i = self.order.findText(str(config["order"]))
            self.order.setCurrentIndex(max(0, i))
        if "scan_shutter" in config:
            try:
                self.scan_shutter.setCurrentIndex(int(config["scan_shutter"]))
            except Exception:
                pass
        if "display_index" in config:
            try:
                self.display_screen.setCurrentIndex(max(0, int(config["display_index"])))
            except Exception:
                pass

    def set_screens(self, screens):
        current = self.display_screen.currentIndex()
        self.display_screen.clear()
        for i, screen in enumerate(screens):
            g = screen.geometry()
            self.display_screen.addItem(f"显示器 {i + 1}  {g.width()}×{g.height()}  ({g.x()},{g.y()})")
        if self.display_screen.count() == 0:
            self.display_screen.addItem("显示器 1")
        self.display_screen.setCurrentIndex(min(max(0, current), self.display_screen.count() - 1))

    def reset_countdown(self):
        self.scan_phase.setText("扫描状态：未开始")
        self.stable_countdown.setText("稳定倒计时：--")
        self.shutter_countdown.setText("快门保持倒计时：--")

    def set_countdown(self, phase: str, stable_ms: int = -1, shutter_ms: int = -1):
        self.scan_phase.setText("扫描状态：" + str(phase))
        if stable_ms >= 0:
            self.stable_countdown.setText(f"稳定倒计时：{stable_ms} ms")
        if shutter_ms >= 0:
            self.shutter_countdown.setText(f"快门保持倒计时：{shutter_ms} ms")


class UniformScanSettingsCard(QFrame):
    parametersChanged = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        title = QLabel("匀速扫描设置")
        title.setObjectName("cardTitle")
        root.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        self.uniform_axis = QComboBox(); self.uniform_axis.addItems(["X 轴", "Y 轴"])
        self.uniform_limit = QComboBox(); self.uniform_limit.addItems(["负限位", "正限位"])
        self.uniform_shutter_position = ScanSettingsCard.line("开快门位置")
        self.uniform_shutter_close_position = ScanSettingsCard.line("关快门位置")
        fields = [
            ("匀速运动轴", self.uniform_axis, 1),
            ("匀速终点", self.uniform_limit, 1),
            ("开快门位置 (mm)", self.uniform_shutter_position, 2),
            ("关快门位置 (mm)", self.uniform_shutter_close_position, 2),
        ]
        row = col = 0
        for label, widget, span in fields:
            if col + span > 2:
                row += 1
                col = 0
            widget.setMinimumHeight(30)
            grid.addWidget(ScanControlCard.field_cell(label, widget), row, col, 1, span)
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.parametersChanged)
            else:
                widget.currentIndexChanged.connect(self.parametersChanged)
            col += span
            if col >= 2:
                row += 1
                col = 0
        root.addLayout(grid)
        self.shutter_selection = QWidget()
        shutter_layout = QHBoxLayout(self.shutter_selection)
        shutter_layout.setContentsMargins(0, 0, 0, 0)
        self.uniform_shutters = {}
        for number in (1, 2, 3):
            checkbox = QCheckBox(f"快门 {number}")
            checkbox.setChecked(number == 1)
            checkbox.toggled.connect(self.parametersChanged)
            self.uniform_shutters[number] = checkbox
            shutter_layout.addWidget(checkbox)
        root.addWidget(ScanControlCard.field_cell("扫描快门（可多选，仅匀速扫描）", self.shutter_selection))
        root.addStretch()

    def values(self, require_position=False, require_close_position=False):
        values = {
            "uniform_axis": "Y" if self.uniform_axis.currentIndex() == 1 else "X",
            "uniform_limit": "positive" if self.uniform_limit.currentIndex() == 1 else "negative",
        }
        if require_close_position:
            values["uniform_shutters"] = self.selected_shutters()
            if not values["uniform_shutters"]:
                raise ValueError("请在匀速扫描设置中至少选择一个扫描快门。")
        if require_position or require_close_position:
            values["uniform_shutter_position"] = ScanControlCard._float(
                self.uniform_shutter_position,
                "开快门位置",
            )
        if require_close_position:
            values["uniform_shutter_close_position"] = ScanControlCard._float(
                self.uniform_shutter_close_position,
                "关快门位置",
            )
        return values

    def selected_shutters(self):
        return [number for number, checkbox in self.uniform_shutters.items() if checkbox.isChecked()]

    def config_values(self):
        return {
            "uniform_shutters": self.selected_shutters(),
            "uniform_axis": self.uniform_axis.currentIndex(),
            "uniform_limit": self.uniform_limit.currentIndex(),
            "uniform_shutter_position": self.uniform_shutter_position.text(),
            "uniform_shutter_close_position": self.uniform_shutter_close_position.text(),
        }

    def apply_config(self, config: dict):
        selected = config.get("uniform_shutters")
        if selected is None:
            try:
                selected = [max(1, min(3, int(config.get("scan_shutter", 0)) + 1))]
            except (TypeError, ValueError):
                selected = [1]
        if isinstance(selected, list):
            for number, checkbox in self.uniform_shutters.items():
                checkbox.setChecked(number in selected)
        if "uniform_shutter_position" in config:
            self.uniform_shutter_position.setText(str(config.get("uniform_shutter_position", "")))
        if "uniform_shutter_close_position" in config:
            self.uniform_shutter_close_position.setText(str(config.get("uniform_shutter_close_position", "")))
        if "uniform_axis" in config:
            try:
                value = config["uniform_axis"]
                if isinstance(value, str):
                    self.uniform_axis.setCurrentIndex(1 if value.upper().startswith("Y") else 0)
                else:
                    self.uniform_axis.setCurrentIndex(max(0, min(1, int(value))))
            except Exception:
                pass
        if "uniform_limit" in config:
            try:
                value = config["uniform_limit"]
                if isinstance(value, str):
                    self.uniform_limit.setCurrentIndex(1 if value.lower().startswith("p") or "正" in value else 0)
                else:
                    self.uniform_limit.setCurrentIndex(max(0, min(1, int(value))))
            except Exception:
                pass


class ScanPreview(QWidget):
    """未扫描点灰色、已扫描点蓝色、当前点红色、已走路径绿色。"""
    def __init__(self):
        super().__init__()
        self.setObjectName("scanPreviewCanvas")
        self.setMinimumSize(310, 230)
        self.points: list[tuple[float, float]] = []
        self.completed_count = 0
        self.current_index = -1
        self.x_range = (-5.0, 5.0)
        self.y_range = (-5.0, 5.0)

    def set_points(self, points):
        self.points = [(float(p.x), float(p.y)) for p in points]
        if self.points:
            xs = [p[0] for p in self.points]; ys = [p[1] for p in self.points]
            self.x_range = (min(xs), max(xs)); self.y_range = (min(ys), max(ys))
        self.completed_count = 0
        self.current_index = -1
        self.update()

    def set_progress(self, completed_count, current_index=-1):
        self.completed_count = max(0, min(int(completed_count), len(self.points)))
        self.current_index = int(current_index)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(46, 28, max(150, self.width()-130), max(145, self.height()-70))
        p.setBrush(QColor("#ffffff")); p.setPen(QPen(QColor("#cfd5dc"), 1)); p.drawRect(rect)
        p.setPen(QPen(QColor("#edf0f3"), 1))
        for i in range(1, 5):
            x = rect.left()+rect.width()*i/5; y = rect.top()+rect.height()*i/5
            p.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            p.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
        if not self.points:
            return
        x0, x1 = self.x_range; y0, y1 = self.y_range
        xs = max(1e-9, x1-x0); ys = max(1e-9, y1-y0)
        mapped = [
            (rect.left()+(x-x0)/xs*rect.width(), rect.bottom()-(y-y0)/ys*rect.height())
            for x, y in self.points
        ]
        # 计划路径：浅灰细线，避免预览一开始全蓝。
        p.setPen(QPen(QColor("#dce1e7"), 1, Qt.PenStyle.DashLine))
        for i in range(len(mapped)-1):
            p.drawLine(int(mapped[i][0]), int(mapped[i][1]), int(mapped[i+1][0]), int(mapped[i+1][1]))
        # 已完成路径：绿色。
        if self.completed_count > 1:
            p.setPen(QPen(QColor("#39b95d"), 2))
            for i in range(self.completed_count-1):
                p.drawLine(int(mapped[i][0]), int(mapped[i][1]), int(mapped[i+1][0]), int(mapped[i+1][1]))
        # 未扫描点：灰色空心。
        p.setBrush(QColor("#ffffff")); p.setPen(QPen(QColor("#c9d0d8"), 1))
        for x, y in mapped[self.completed_count:]:
            p.drawEllipse(QRectF(x-3, y-3, 6, 6))
        # 已扫描点：小蓝点，仅标已完成部分。
        p.setBrush(QColor("#2f7de8")); p.setPen(Qt.PenStyle.NoPen)
        for x, y in mapped[:self.completed_count]:
            p.drawEllipse(QRectF(x-3, y-3, 6, 6))
        # 当前点：只有一个红点。
        if 0 <= self.current_index < len(mapped):
            x, y = mapped[self.current_index]
            p.setBrush(QColor("#ef343a")); p.setPen(QPen(QColor("white"), 2))
            p.drawEllipse(QRectF(x-6, y-6, 12, 12))
        p.setPen(QColor("#252a31"))
        p.drawText(int(rect.left()-15), int(rect.bottom()+20), f"{x0:.1f}")
        p.drawText(int(rect.right()-18), int(rect.bottom()+20), f"{x1:.1f}")
        p.drawText(int(rect.center().x()-18), int(rect.bottom()+40), "X (mm)")


class FullscreenImageWindow(QWidget):
    def __init__(self, image_path: str, screen, parent=None):
        super().__init__(None)
        self.image_path = str(image_path)
        self.setObjectName("fullscreenImageWindow")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setStyleSheet("background:#000000;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("background:#000000;")
        layout.addWidget(self.label)
        if screen is not None:
            self.setGeometry(screen.geometry())
        self._pixmap = QPixmap(self.image_path)
        self._update_pixmap()

    def _update_pixmap(self):
        if self._pixmap.isNull():
            self.label.setText("图片加载失败\n" + self.image_path)
            self.label.setStyleSheet("background:#000000; color:#ffffff; font-size:24px;")
            return
        scaled = self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_pixmap()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


class PhaseScanWorker(QThread):
    pointStarted = pyqtSignal(int, int, float, float)
    pointFinished = pyqtSignal(int, int, float, float, str)
    scanFinished = pyqtSignal(str)
    scanStopped = pyqtSignal(str)
    scanError = pyqtSignal(str)
    scanCountdownChanged = pyqtSignal(str, int, int)
    displayImageRequested = pyqtSignal(str, int)
    closeImageRequested = pyqtSignal()

    def __init__(self, controller, points, scan_params, motion_params, shutter_base_url, shutter_simulate, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.points = list(points)
        self.scan_params = dict(scan_params)
        self.motion_params = dict(motion_params)
        self.shutter_base_url = str(shutter_base_url or "").strip()
        self.shutter_simulate = bool(shutter_simulate)
        self._stop_event = False
        self._pause_event = False
        self.records = []

    def stop(self):
        self._stop_event = True
        self._pause_event = False
        try:
            self.controller.halt_all()
        except Exception:
            pass
        try:
            self._close_shutter()
        except Exception:
            pass

    def set_paused(self, paused: bool):
        self._pause_event = bool(paused)
        if paused:
            try: self.controller.halt_all()
            except Exception: pass

    def _sleep_interruptible(self, ms: int, phase: str = "等待") -> bool:
        total_ms = max(0, int(ms))
        deadline = time.monotonic() + total_ms / 1000.0
        last_emit = None
        if total_ms == 0:
            if phase == "稳定":
                self.scanCountdownChanged.emit("位置已稳定", 0, -1)
            elif phase == "快门保持":
                self.scanCountdownChanged.emit("快门保持完成", -1, 0)
            return not self._stop_event
        while time.monotonic() < deadline:
            if self._stop_event:
                return False
            while self._pause_event and not self._stop_event:
                self.scanCountdownChanged.emit("扫描暂停", -1, -1)
                self.msleep(20)
            remain_ms = max(0, int(round((deadline - time.monotonic()) * 1000)))
            bucket = remain_ms // 100
            if bucket != last_emit:
                last_emit = bucket
                if phase == "稳定":
                    self.scanCountdownChanged.emit("到位稳定等待", remain_ms, -1)
                elif phase == "曝光":
                    self.scanCountdownChanged.emit("曝光等待", remain_ms, -1)
                elif phase == "快门保持":
                    self.scanCountdownChanged.emit("快门保持中", -1, remain_ms)
                else:
                    self.scanCountdownChanged.emit(phase, -1, -1)
            self.msleep(10)
        if phase == "稳定":
            self.scanCountdownChanged.emit("位置已稳定", 0, -1)
        elif phase == "曝光":
            self.scanCountdownChanged.emit("曝光等待完成", 0, -1)
        elif phase == "快门保持":
            self.scanCountdownChanged.emit("快门保持完成", -1, 0)
        return not self._stop_event

    def _wait_axis(self, axis: str, target: float, tolerance=0.001, timeout_ms=30000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        stable_count = 0
        while time.monotonic() < deadline:
            if self._stop_event:
                try: self.controller.halt_all()
                except Exception: pass
                return False
            while self._pause_event and not self._stop_event:
                self.msleep(20)
            pos = self.controller.get_position(axis)
            if abs(pos - target) <= tolerance:
                stable_count += 1
                if stable_count >= 3:
                    return True
            else:
                stable_count = 0
            self.msleep(20)
        raise TimeoutError(f"{axis} 轴到位超时，目标={target:.6f}，当前位置={self.controller.get_position(axis):.6f}")

    def _move_to_point(self, x: float, y: float):
        order = self.scan_params.get("order", "先 X 后 Y")
        if order == "先 Y 后 X":
            seq = (("Y", y), ("X", x))
        else:
            seq = (("X", x), ("Y", y))
        for axis, target in seq:
            self.controller.move_absolute(axis, target)
            if not self._wait_axis(axis, target):
                return False
        return True

    def _open_shutter(self, shutter_no=None):
        shutter_no = int(shutter_no or self.scan_params["scan_shutter"])
        if self.shutter_simulate:
            print(f"[扫描模拟] 打开快门 {shutter_no}")
        else:
            CH375ShutterClient(self.shutter_base_url).open_shutter(shutter_no)
        timeline = self.scan_params.get('_hologram_timeline')
        if timeline is not None:
            timeline.mark(True)
        if self._stop_event:
            self._close_shutter()
            raise RuntimeError("扫描被停止")

    def _close_shutter(self):
        timeline = self.scan_params.get('_hologram_timeline')
        if timeline is not None:
            timeline.mark(False)
        if self.shutter_simulate:
            print("[扫描模拟] 关闭快门 0x0A")
            return
        if self.shutter_base_url:
            CH375ShutterClient(self.shutter_base_url).close_shutter()

    @staticmethod
    def _coord_text(value: float) -> str:
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text else "0"

    @staticmethod
    def _safe_filename(name: str) -> str:
        return re.sub(r'[\\/:*?"<>|]+', "_", name)

    def _phase_image_path(self, folder: Path, x: float, y: float) -> Path:
        name = f"p_x{self._coord_text(x)}_p_y{self._coord_text(y)}.png"
        return folder / self._safe_filename(name)

    def _expected_image_path(self, folder: Path, point) -> Path:
        custom = str(getattr(point, "image_path", "") or "").strip()
        if custom:
            path = Path(custom).expanduser()
            if not path.is_absolute():
                path = folder / path
            return path
        return self._phase_image_path(folder, point.x, point.y)

    def _find_phase_image(self, expected_path: Path, wait_ms: int = 3000, exact_only: bool = False) -> Path:
        """
        查找外部相位软件/相机保存的同名图片。
        本程序不再自己生成相位图；必须在输出文件夹中找到同名文件。
        """
        folder = expected_path.parent
        exact_name = expected_path.name
        candidates = [expected_path]
        # 坐标扫描允许按同名 stem 兼容常见图片扩展名；
        # 文件扫描必须严格使用 Excel 中填写的文件名/路径，不再回退到坐标拼接或其他扩展名。
        if not exact_only:
            for ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"):
                candidate = folder / (expected_path.stem + ext)
                if candidate not in candidates:
                    candidates.append(candidate)

        deadline = time.monotonic() + max(0, int(wait_ms)) / 1000.0
        while True:
            for candidate in candidates:
                if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
            # 大小写不敏感兜底。
            if folder.exists():
                target_lower = exact_name.lower()
                for file in folder.iterdir():
                    if file.is_file() and file.name.lower() == target_lower and file.stat().st_size > 0:
                        return file
            if time.monotonic() >= deadline or self._stop_event:
                break
            self.msleep(50)
        if exact_only:
            raise FileNotFoundError(
                f"文件扫描未找到 Excel 指定图片：{expected_path.name}。"
                f"请确认 Excel 图片列填写正确，并且图片在该路径下存在：{folder}"
            )
        raise FileNotFoundError(
            f"未找到相位图：{expected_path.name}。请确认外部相位软件已把同名图片保存到：{folder}"
        )

    def _save_excel(self, folder: Path) -> Path:
        try:
            from openpyxl import Workbook
            from openpyxl.drawing.image import Image as XLImage
            from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
        except Exception as exc:
            raise RuntimeError("保存 Excel 需要安装 openpyxl：pip install openpyxl Pillow") from exc

        def pixels_to_excel_width(px: int) -> float:
            """
            Excel 的列宽单位不是像素。这里使用常用换算把图片列撑到
            与原始图片像素宽度尽量一致。Excel 列宽最大约 255，超过后
            图片仍保持原始像素插入，但列宽只能到 Excel 上限。
            """
            px = max(1, int(px))
            if px <= 12:
                return round(px / 12.0, 2)
            return round((px - 5) / 7.0, 2)

        def pixels_to_row_points(px: int) -> float:
            # Excel 行高单位是 pt；96 DPI 下 1 px ≈ 0.75 pt。
            return round(max(1, int(px)) * 0.75, 2)

        def required_row_count(image_height_px: int) -> int:
            """
            Excel 单行高度最大约 409.5 pt，约 546 px。
            如果图片更高，使用多行合并成一个图片单元格，让合并区域总高度
            与原图高度一致，图片不缩放、不裁剪。
            """
            total_pt = pixels_to_row_points(image_height_px)
            return max(1, int(math.ceil(total_pt / 409.5)))

        def set_rows_to_image_height(start_row: int, row_count: int, image_height_px: int):
            total_pt = pixels_to_row_points(image_height_px)
            if row_count <= 1:
                ws.row_dimensions[start_row].height = total_pt
                return

            # 前面的行用最大高度，最后一行补足剩余高度。
            remaining = total_pt
            for offset in range(row_count):
                row_no = start_row + offset
                if offset < row_count - 1:
                    height = min(409.5, remaining)
                else:
                    height = max(1.0, min(409.5, remaining))
                ws.row_dimensions[row_no].height = round(height, 2)
                remaining -= height

        excel_path = folder / f"scan_result_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "扫描结果"
        headers = [
            "序号", "X坐标(mm)", "Y坐标(mm)", "曝光时间(ms)", "稳定时间(ms)", "快门通道", "快门保持(ms)",
            "图片名称", "图片路径", "原始分辨率(px)", "相位图图片"
        ]
        ws.append(headers)

        fill = PatternFill("solid", fgColor="1F4E78")
        font = Font(color="FFFFFF", bold=True)
        border = Border(bottom=Side(style="thin", color="A6A6A6"))
        for cell in ws[1]:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
        ws.row_dimensions[1].height = 24

        ws.column_dimensions["A"].width = 8
        ws.column_dimensions["B"].width = 15
        ws.column_dimensions["C"].width = 15
        ws.column_dimensions["D"].width = 14
        ws.column_dimensions["E"].width = 14
        ws.column_dimensions["F"].width = 12
        ws.column_dimensions["G"].width = 14
        ws.column_dimensions["H"].width = 28
        ws.column_dimensions["I"].width = 60
        ws.column_dimensions["J"].width = 18
        ws.column_dimensions["K"].width = 80

        max_image_width_px = 0
        current_row = 2
        for seq, record in enumerate(self.records, start=1):
            image_path = Path(record["image_path"])
            img = XLImage(str(image_path))

            # 图片保持原始像素尺寸插入 Excel，不缩放、不裁剪。
            original_w = int(img.width)
            original_h = int(img.height)
            img.width = original_w
            img.height = original_h

            row_count = required_row_count(original_h)
            start_row = current_row
            end_row = current_row + row_count - 1

            # 数据写在记录起始行；如果图片太高需要多行承载，则把 A:K 都合并，
            # 让“图片所在单元格/合并单元格区域”的总尺寸跟图片一致。
            ws.cell(start_row, 1, seq)
            ws.cell(start_row, 2, record["x"])
            ws.cell(start_row, 3, record["y"])
            ws.cell(start_row, 4, record.get("exposure_ms", ""))
            ws.cell(start_row, 5, record.get("stable_ms", ""))
            ws.cell(start_row, 6, record.get("shutter_no", ""))
            ws.cell(start_row, 7, record.get("shutter_hold_ms", ""))
            ws.cell(start_row, 8, record["image_name"])
            ws.cell(start_row, 9, str(image_path))
            ws.cell(start_row, 10, f"{original_w} × {original_h}")

            if row_count > 1:
                for col in range(1, 12):
                    ws.merge_cells(
                        start_row=start_row,
                        start_column=col,
                        end_row=end_row,
                        end_column=col,
                    )

            # 关键修改：按原图高度设置图片所在行/合并单元格高度。
            set_rows_to_image_height(start_row, row_count, original_h)

            # 图片锚定在 K 列起始单元格，尺寸仍是原始像素。
            ws.add_image(img, f"K{start_row}")
            max_image_width_px = max(max_image_width_px, original_w)

            current_row = end_row + 1

        # 关键修改：按原图宽度设置图片列宽，使图片单元格宽度跟图片宽度一致。
        if max_image_width_px:
            ws.column_dimensions["K"].width = min(pixels_to_excel_width(max_image_width_px), 255)

        # 文本区域垂直居中；图片列不强制 wrap。
        for row in ws.iter_rows(min_row=2):
            for cell in row[:10]:
                cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
        ws.freeze_panes = "A2"
        wb.save(excel_path)
        return excel_path

    def run(self):
        try:
            output_dir = Path(self.scan_params["output_dir"]).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            stable_ms = int(self.scan_params["stable_ms"])
            hold_ms = int(self.scan_params["shutter_hold_ms"])
            speed = float(self.scan_params["speed"])
            acc = float(self.motion_params["acceleration"])
            dec = float(self.motion_params["deceleration"])
            for axis in ("X", "Y"):
                status = self.controller.get_axis_status(axis)
                if not status.enabled:
                    raise RuntimeError(f"{axis} 轴未启动，无法开始扫描。")
                self.controller.set_motion_parameters(axis, speed, acc, dec)
            total = len(self.points)
            for index, point in enumerate(self.points, start=1):
                point_exposure_ms = int(getattr(point, "exposure_ms", 0) or 0)
                point_stable_ms = int(getattr(point, "stable_ms", stable_ms) if getattr(point, "stable_ms", None) is not None else stable_ms)
                point_shutter_no = getattr(point, "shutter_no", None)
                point_hold_ms = int(getattr(point, "shutter_hold_ms", hold_ms) if getattr(point, "shutter_hold_ms", None) is not None else hold_ms)
                if point_shutter_no is not None:
                    point_shutter_no = int(point_shutter_no)
                    point_hold_ms = point_exposure_ms
                else:
                    point_shutter_no = int(self.scan_params["scan_shutter"])
                if self._stop_event:
                    self.scanStopped.emit("扫描被停止")
                    return
                self.pointStarted.emit(index, total, point.x, point.y)
                self.scanCountdownChanged.emit("移动到扫描点", -1, -1)
                if not self._move_to_point(point.x, point.y):
                    self.scanStopped.emit("扫描被停止")
                    return

                if self.scan_params.get("hologram_enabled"):
                    image_path = Path(self.scan_params["_hologram_first_path"])
                else:
                    expected_path = self._expected_image_path(output_dir, point)
                    self.scanCountdownChanged.emit("查找并显示相位图 " + expected_path.name, 0, 0)
                    exact_from_excel = bool(str(getattr(point, "image_path", "") or "").strip())
                    image_path = self._find_phase_image(expected_path, exact_only=exact_from_excel)
                    self.displayImageRequested.emit(str(image_path), int(self.scan_params.get("display_index", 0)))
                    self.msleep(80)

                if getattr(point, "shutter_no", None) is None and point_exposure_ms > 0:
                    if not self._sleep_interruptible(point_exposure_ms, "曝光"):
                        self.scanStopped.emit("扫描被停止")
                        return
                if not self._sleep_interruptible(point_stable_ms, "稳定"):
                    self.scanStopped.emit("扫描被停止")
                    return
                self.scanCountdownChanged.emit(f"打开快门 {point_shutter_no}", 0, -1)
                self._open_shutter(point_shutter_no)
                if not self._sleep_interruptible(point_hold_ms, "快门保持"):
                    self._close_shutter()
                    self.scanStopped.emit("扫描被停止")
                    return
                self._close_shutter()
                record = {
                    "x": point.x,
                    "y": point.y,
                    "image_name": image_path.name,
                    "image_path": image_path,
                    "exposure_ms": point_exposure_ms,
                    "stable_ms": point_stable_ms,
                    "shutter_no": point_shutter_no,
                    "shutter_hold_ms": point_hold_ms,
                }
                self.records.append(record)
                self.pointFinished.emit(index, total, point.x, point.y, str(image_path))

            if self.points:
                start = self.points[0]
                self.closeImageRequested.emit()
                self.scanCountdownChanged.emit(f"扫描完成，回到起始点 X={start.x:.6f}, Y={start.y:.6f}", 0, 0)
                if not self._move_to_point(start.x, start.y):
                    self.scanStopped.emit("回到起始点时扫描被停止")
                    return
            excel_path = self._save_excel(output_dir)
            self.closeImageRequested.emit()
            self.scanFinished.emit(str(excel_path))
        except Exception as exc:
            try: self._close_shutter()
            except Exception: pass
            self.closeImageRequested.emit()
            if self._stop_event:
                self.scanStopped.emit(str(exc))
            else:
                self.scanError.emit(str(exc))


class UniformExposureWorker(QThread):
    scanFinished = pyqtSignal(str)
    scanStopped = pyqtSignal(str)
    scanError = pyqtSignal(str)
    scanCountdownChanged = pyqtSignal(str, int, int)

    def __init__(self, controller, scan_params, motion_params, shutter_base_url, shutter_simulate, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.scan_params = dict(scan_params)
        self.motion_params = dict(motion_params)
        self.shutter_base_url = str(shutter_base_url or "").strip()
        self.shutter_simulate = bool(shutter_simulate)
        self._stop_event = False

    def stop(self):
        self._stop_event = True
        try:
            self.controller.halt_all()
        except Exception:
            pass
        try:
            self._close_shutter()
        except Exception:
            pass

    def set_paused(self, paused: bool):
        if paused:
            self.stop()

    def _open_shutter(self):
        shutter_no = int(self.scan_params["scan_shutter"])
        if self.shutter_simulate:
            print(f"[匀速曝光模拟] 打开快门 {shutter_no}")
        else:
            CH375ShutterClient(self.shutter_base_url).open_shutter(shutter_no)
        timeline = self.scan_params.get('_hologram_timeline')
        if timeline is not None:
            timeline.mark(True)
        if self._stop_event:
            self._close_shutter()
            raise RuntimeError("扫描被停止")

    def _close_shutter(self):
        timeline = self.scan_params.get('_hologram_timeline')
        if timeline is not None:
            timeline.mark(False)
        if self.shutter_simulate:
            print("[匀速曝光模拟] 关闭快门 0x0A")
            return
        if self.shutter_base_url:
            CH375ShutterClient(self.shutter_base_url).close_shutter()

    def _sleep_interruptible(self, ms: int, phase: str) -> bool:
        total_ms = max(0, int(ms))
        deadline = time.monotonic() + total_ms / 1000.0
        last_bucket = None
        if total_ms == 0:
            if phase == "稳定":
                self.scanCountdownChanged.emit("位置已稳定", 0, -1)
            elif phase == "快门保持":
                self.scanCountdownChanged.emit("快门保持完成", -1, 0)
            return not self._stop_event
        while time.monotonic() < deadline:
            if self._stop_event:
                return False
            remain_ms = max(0, int(round((deadline - time.monotonic()) * 1000)))
            bucket = remain_ms // 100
            if bucket != last_bucket:
                last_bucket = bucket
                if phase == "稳定":
                    self.scanCountdownChanged.emit("到位稳定等待", remain_ms, -1)
                elif phase == "快门保持":
                    self.scanCountdownChanged.emit("快门保持中", -1, remain_ms)
                else:
                    self.scanCountdownChanged.emit(phase, -1, -1)
            self.msleep(10)
        if phase == "稳定":
            self.scanCountdownChanged.emit("位置已稳定", 0, -1)
        elif phase == "快门保持":
            self.scanCountdownChanged.emit("快门保持完成", -1, 0)
        return not self._stop_event

    def _wait_axis(self, axis: str, target: float, tolerance=0.001, timeout_ms=30000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        stable_count = 0
        while time.monotonic() < deadline:
            if self._stop_event:
                try:
                    self.controller.halt(axis)
                except Exception:
                    pass
                return False
            pos = self.controller.get_position(axis)
            if abs(pos - target) <= tolerance:
                stable_count += 1
                if stable_count >= 3:
                    return True
            else:
                stable_count = 0
            self.msleep(20)
        raise TimeoutError(f"{axis} 轴到位超时，目标={target:.6f}，当前位置={self.controller.get_position(axis):.6f}")

    def run(self):
        shutter_open = False
        try:
            axis = str(self.scan_params["uniform_axis"]).upper()
            target_limit = str(self.scan_params["uniform_limit"])
            preset = float(self.scan_params["uniform_shutter_position"])
            stable_ms = int(self.scan_params["stable_ms"])
            hold_ms = int(self.scan_params["shutter_hold_ms"])
            speed = float(self.scan_params["speed"])
            acc = float(self.motion_params["acceleration"])
            dec = float(self.motion_params["deceleration"])
            low, high = self.controller.soft_limits[axis]
            target = high if target_limit == "positive" else low
            start = self.controller.get_position(axis)
            direction = 1 if target >= start else -1
            if direction > 0 and not start <= preset <= target:
                raise ValueError(f"开快门位置 {preset:.6f} mm 不在当前位置 {start:.6f} 到正限位 {target:.6f} 之间。")
            if direction < 0 and not target <= preset <= start:
                raise ValueError(f"开快门位置 {preset:.6f} mm 不在当前位置 {start:.6f} 到负限位 {target:.6f} 之间。")
            status = self.controller.get_axis_status(axis)
            if not status.enabled:
                raise RuntimeError(f"{axis} 轴未启动，无法开始匀速运动曝光。")

            self.controller.set_motion_parameters(axis, speed, acc, dec)
            self.scanCountdownChanged.emit(
                f"{axis} 轴移动到开快门位置 {preset:.6f} mm",
                -1,
                -1,
            )
            self.controller.move_absolute(axis, preset)
            if not self._wait_axis(axis, preset):
                self.scanStopped.emit("匀速运动曝光被停止")
                return
            if not self._sleep_interruptible(stable_ms, "稳定"):
                self.scanStopped.emit("匀速运动曝光被停止")
                return

            self.scanCountdownChanged.emit("打开快门，轴继续匀速运动", 0, hold_ms)
            self._open_shutter()
            shutter_open = True
            hold_deadline = time.monotonic() + max(0, hold_ms) / 1000.0
            self.controller.move_absolute(axis, target)
            reached_end_count = 0
            last_bucket = None
            while not self._stop_event:
                pos = self.controller.get_position(axis)
                if shutter_open:
                    remain_ms = max(0, int(round((hold_deadline - time.monotonic()) * 1000)))
                    bucket = remain_ms // 100
                    if bucket != last_bucket:
                        last_bucket = bucket
                        self.scanCountdownChanged.emit("快门保持中，轴继续匀速运动", -1, remain_ms)
                    if remain_ms <= 0:
                        self._close_shutter()
                        shutter_open = False
                        self.scanCountdownChanged.emit("快门保持完成，快门已关闭", -1, 0)

                at_target = abs(pos - target) <= 0.001
                if at_target:
                    reached_end_count += 1
                    if reached_end_count >= 3 and not shutter_open:
                        break
                else:
                    reached_end_count = 0
                self.msleep(20)
            if self._stop_event:
                self.scanStopped.emit("匀速运动曝光被停止")
                return
            try:
                self.controller.halt(axis)
            except Exception:
                pass
            if shutter_open:
                self._close_shutter()
            self.scanCountdownChanged.emit("匀速运动曝光完成", -1, 0)
            self.scanFinished.emit("匀速运动曝光完成")
        except Exception as exc:
            try:
                if shutter_open:
                    self._close_shutter()
            except Exception:
                pass
            if self._stop_event:
                self.scanStopped.emit(str(exc))
            else:
                self.scanError.emit(str(exc))


class UniformLineScanWorker(QThread):
    shutterCommandSent = pyqtSignal(str)
    scanFinished = pyqtSignal(str)
    scanStopped = pyqtSignal(str)
    scanError = pyqtSignal(str)
    scanCountdownChanged = pyqtSignal(str, int, int)
    imageColumnCompleted = pyqtSignal(int)
    videoPrepareRequested = pyqtSignal(int)
    lineStarted = pyqtSignal(int, int, float, float, float)
    lineFinished = pyqtSignal(int, int, float, float, float)

    def __init__(self, controller, scan_params, motion_params, shutter_base_url, shutter_simulate, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.scan_params = dict(scan_params)
        self.motion_params = dict(motion_params)
        self.shutter_base_url = str(shutter_base_url or "").strip()
        self.shutter_simulate = bool(shutter_simulate)
        self._stop_event = False
        self.cache_session = self.scan_params.get("_hologram_cache_session")
        self.video_session = self.scan_params.get("_hologram_video_session")
        self.galvo_runner = self.scan_params.get("_galvo_runner")
        if self.scan_params.get('galvo_enabled') and self.galvo_runner is None:
            raise ValueError('振镜联动已勾选，但未加载有效的振镜两路点列。')
        if self.galvo_runner is not None:
            self.galvo_runner.message = self.shutterCommandSent.emit

    def stop(self):
        self._stop_event = True
        if self.cache_session is not None:
            self.cache_session.cancel()
        if self.video_session is not None:
            self.video_session.cancel()
        if self.galvo_runner is not None:
            self.galvo_runner.abort()
        try:
            self.controller.halt_all()
        except Exception:
            pass
        try:
            self._close_shutter()
        except Exception:
            pass

    def set_paused(self, paused: bool):
        if paused:
            self.stop()

    def _open_shutter(self):
        shutters = self.scan_params.get("uniform_shutters")
        if shutters is None:
            shutters = [self.scan_params["scan_shutter"]]
        if not shutters or any(number not in (1, 2, 3) for number in shutters):
            raise ValueError("请至少选择一个有效的扫描快门（1、2、3）。")
        command = CH375ShutterClient.shutters_command(shutters)
        try:
            if self._stop_event:
                raise RuntimeError("匀速扫描被停止")
            if self.cache_session is not None:
                self.cache_session.raise_if_failed()
            if self.video_session is not None:
                self.video_session.raise_if_failed()
            if self.shutter_simulate:
                print(f"[匀速扫描模拟] 快门 {shutters}，组合命令 0x{command:02X}")
            else:
                CH375ShutterClient(self.shutter_base_url).write_command(command)
            timeline = self.scan_params.get('_hologram_timeline')
            if timeline is not None:
                timeline.mark(True, getattr(self, "_hologram_line_context", None))
            if self.galvo_runner is not None:
                self.galvo_runner.start_line(self._hologram_line_context['column'])
            self.shutterCommandSent.emit(
                f"{'[模拟] ' if self.shutter_simulate else ''}匀速扫描：快门 {'、'.join(str(n) for n in shutters)}，"
                f"已发送组合指令 0x{command:02X}"
            )
            if self._stop_event:
                raise RuntimeError("匀速扫描被停止")
        except Exception:
            # 发送异常或扫描停止时，关闭可能已经打开的快门。
            try:
                self._close_shutter()
            except Exception:
                pass
            raise

    def _close_shutter(self):
        # Cancel future serial targets immediately; close the shutter before waiting
        # for an in-flight serial packet and sending the two stop-wave commands.
        if self.galvo_runner is not None:
            self.galvo_runner.request_stop()
        timeline = self.scan_params.get('_hologram_timeline')
        if timeline is not None:
            timeline.mark(False)
        try:
            if self.shutter_simulate:
                print("[匀速扫描模拟] 关闭快门 0x0A")
            elif self.shutter_base_url:
                CH375ShutterClient(self.shutter_base_url).close_shutter()
        finally:
            if self.galvo_runner is not None:
                self.galvo_runner.stop_line()

    def _sleep_interruptible(self, ms: int, phase: str) -> bool:
        total_ms = max(0, int(ms))
        deadline = time.monotonic() + total_ms / 1000.0
        last_bucket = None
        while time.monotonic() < deadline:
            if self._stop_event:
                return False
            if self.cache_session is not None:
                self.cache_session.raise_if_failed()
            if self.video_session is not None:
                self.video_session.raise_if_failed()
            remain_ms = max(0, int(round((deadline - time.monotonic()) * 1000)))
            bucket = remain_ms // 100
            if bucket != last_bucket:
                last_bucket = bucket
                self.scanCountdownChanged.emit(phase, remain_ms if phase == "到位稳定等待" else -1, -1)
            self.msleep(10)
        if phase == "到位稳定等待":
            self.scanCountdownChanged.emit("位置已稳定", 0, -1)
        return not self._stop_event

    def _wait_axis(self, axis: str, target: float, tolerance=0.001, timeout_ms=30000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        stable_count = 0
        while time.monotonic() < deadline:
            if self._stop_event:
                try:
                    self.controller.halt(axis)
                except Exception:
                    pass
                return False
            if self.cache_session is not None:
                self.cache_session.raise_if_failed()
            if self.video_session is not None:
                self.video_session.raise_if_failed()
            pos = self.controller.get_position(axis)
            if abs(pos - target) <= tolerance:
                stable_count += 1
                if stable_count >= 3:
                    return True
            else:
                stable_count = 0
            self.msleep(20)
        raise TimeoutError(f"{axis} 轴到位超时，目标={target:.6f}，当前位置={self.controller.get_position(axis):.6f}")

    @staticmethod
    def _crossed(position: float, target: float, direction: int) -> bool:
        return position >= target if direction > 0 else position <= target

    @staticmethod
    def _inclusive_values(start, end, step):
        if step <= 0:
            raise ValueError("X 步进必须大于 0。")
        direction = 1 if end >= start else -1
        values = []
        value = start
        for _ in range(200000):
            if direction > 0 and value > end + step * 1e-9 or direction < 0 and value < end - step * 1e-9:
                break
            values.append(round(value, 9))
            value += direction * step
        if not values or abs(values[-1] - end) > step * 1e-7:
            values.append(end)
        return values

    def run(self):
        shutter_open = False
        try:
            x_start = float(self.scan_params["x_start"])
            x_end = float(self.scan_params["x_end"])
            x_step = float(self.scan_params["x_step"])
            y_start = float(self.scan_params["y_start"])
            y_end = float(self.scan_params["y_end"])
            open_y = float(self.scan_params["uniform_shutter_position"])
            close_y = float(self.scan_params["uniform_shutter_close_position"])
            stable_ms = int(self.scan_params["stable_ms"])
            scan_speed = float(self.scan_params["speed"])
            reset_speed = float(self.motion_params["velocity"])
            acc = float(self.motion_params["acceleration"])
            dec = float(self.motion_params["deceleration"])
            shutters = self.scan_params.get("uniform_shutters")
            if shutters is None:
                shutters = [int(self.scan_params["scan_shutter"])]
            if not shutters or any(number not in (1, 2, 3) for number in shutters):
                raise ValueError("请至少选择一个有效的扫描快门（1、2、3）。")
            shutter_text = "、".join(str(number) for number in shutters)

            y_direction = 1 if y_end >= y_start else -1
            if y_direction > 0 and not y_start <= open_y <= close_y <= y_end:
                raise ValueError("Y 正向匀速扫描时，必须满足 Y起始 <= 开快门位置 <= 关快门位置 <= Y结束。")
            if y_direction < 0 and not y_start >= open_y >= close_y >= y_end:
                raise ValueError("Y 反向匀速扫描时，必须满足 Y起始 >= 开快门位置 >= 关快门位置 >= Y结束。")

            for axis in ("X", "Y"):
                status = self.controller.get_axis_status(axis)
                if not status.enabled:
                    raise RuntimeError(f"{axis} 轴未启动，无法开始匀速扫描。")

            xs = self._inclusive_values(x_start, x_end, x_step)
            total = len(xs)
            for index, x in enumerate(xs, start=1):
                if self._stop_event:
                    self.scanStopped.emit("匀速扫描被停止")
                    return
                self._hologram_line_context = {"kind": "匀速扫描线", "index": index, "column": index, "x": x}
                if self.video_session is not None:
                    self.scanCountdownChanged.emit(f"第 {index}/{total} 条：加载全息视频", -1, -1)
                    self.video_session.request(index)
                    self.videoPrepareRequested.emit(index)
                    if not self.video_session.wait_ready(lambda: self._stop_event):
                        self.scanStopped.emit("全息视频准备被停止")
                        return
                if self.galvo_runner is not None:
                    self.scanCountdownChanged.emit(f"第 {index}/{total} 条：准备振镜两路点列", -1, -1)
                    self.galvo_runner.prepare_line(index)
                    self.shutterCommandSent.emit(f"振镜第 {index} 列两路点列已准备，等待开快门启动。")
                self.lineStarted.emit(index, total, x, y_start, y_end)
                self.scanCountdownChanged.emit(f"第 {index}/{total} 条：移动到 X={x:.6f}", -1, -1)
                self.controller.set_motion_parameters("X", reset_speed, acc, dec)
                self.controller.move_absolute("X", x)
                if not self._wait_axis("X", x):
                    self.scanStopped.emit("匀速扫描被停止")
                    return

                self.scanCountdownChanged.emit(f"第 {index}/{total} 条：Y 回到起始位置 {y_start:.6f}", -1, -1)
                self.controller.set_motion_parameters("Y", reset_speed, acc, dec)
                self.controller.move_absolute("Y", y_start)
                if not self._wait_axis("Y", y_start):
                    self.scanStopped.emit("匀速扫描被停止")
                    return
                if not self._sleep_interruptible(stable_ms, "到位稳定等待"):
                    self.scanStopped.emit("匀速扫描被停止")
                    return

                self.scanCountdownChanged.emit(
                    f"第 {index}/{total} 条：Y 匀速扫描，等待开快门位置 {open_y:.6f}",
                    0,
                    -1,
                )
                self.controller.set_motion_parameters("Y", scan_speed, acc, dec)
                self.controller.move_absolute("Y", y_end)
                reached_end_count = 0
                shutter_opened_this_line = False
                shutter_closed_this_line = False
                while not self._stop_event:
                    if self.cache_session is not None:
                        self.cache_session.raise_if_failed()
                    if self.video_session is not None:
                        self.video_session.raise_if_failed()
                    if self.galvo_runner is not None:
                        self.galvo_runner.raise_if_failed()
                    pos = self.controller.get_position("Y")
                    if not shutter_opened_this_line and self._crossed(pos, open_y, y_direction):
                        self._open_shutter()
                        shutter_open = True
                        shutter_opened_this_line = True
                        self.scanCountdownChanged.emit(
                            f"快门 {shutter_text} 打开指令已发送，等待关快门位置 {close_y:.6f}",
                            -1,
                            -1,
                        )
                    if shutter_open and not shutter_closed_this_line and self._crossed(pos, close_y, y_direction):
                        self._close_shutter()
                        shutter_open = False
                        shutter_closed_this_line = True
                        self.scanCountdownChanged.emit("已到关快门位置，快门已关闭", -1, 0)

                    if abs(pos - y_end) <= 0.001:
                        reached_end_count += 1
                        if reached_end_count >= 3:
                            break
                    else:
                        reached_end_count = 0
                    self.msleep(10)
                if self._stop_event:
                    self.scanStopped.emit("匀速扫描被停止")
                    return
                if shutter_open:
                    self._close_shutter()
                    shutter_open = False
                if self.galvo_runner is not None:
                    self.galvo_runner.raise_if_failed()
                self.lineFinished.emit(index, total, x, y_start, y_end)
                if self.cache_session is not None:
                    self.scanCountdownChanged.emit(f"第{index}列已完成：释放图片并补载后续列", -1, -1)
                    self.cache_session.request(index)
                    self.imageColumnCompleted.emit(index)
                    if not self.cache_session.wait_ready(lambda: self._stop_event):
                        self.scanStopped.emit("全息图缓存补载被停止")
                        return

            self.scanCountdownChanged.emit("匀速扫描完成", -1, 0)
            self.scanFinished.emit("匀速扫描完成")
        except Exception as exc:
            try:
                self.controller.halt_all()
            except Exception:
                pass
            try:
                self._close_shutter()
            except Exception:
                pass
            if self._stop_event:
                self.scanStopped.emit(str(exc))
            else:
                self.scanError.emit(str(exc))
        finally:
            if self.cache_session is not None:
                self.cache_session.cancel()
            if self.video_session is not None:
                self.video_session.cancel()
            if self.galvo_runner is not None:
                try:
                    self.galvo_runner.stop_line()
                except Exception as exc:
                    self.scanError.emit('振镜清理失败：' + str(exc))


class LimitCaptureWorker(QThread):
    limitCaptured = pyqtSignal(str, float)
    captureMessage = pyqtSignal(str)
    captureFinished = pyqtSignal()
    captureStopped = pyqtSignal(str)
    captureError = pyqtSignal(str)

    def __init__(self, controller, velocity: float, acceleration: float, deceleration: float, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.velocity = abs(float(velocity))
        self.acceleration = float(acceleration)
        self.deceleration = float(deceleration)
        self._stop_event = False

    def stop(self):
        self._stop_event = True
        try:
            self.controller.halt_all()
        except Exception:
            pass

    def _capture_one(self, axis: str, positive: bool):
        name = f"{axis} {'正' if positive else '负'}限位"
        self.captureMessage.emit(f"正在抓取{name}")
        self.controller.set_motion_parameters(axis, self.velocity, self.acceleration, self.deceleration)
        self.controller.jog(axis, self.velocity if positive else -self.velocity)
        try:
            while not self._stop_event:
                state = self.controller.get_axis_status(axis)
                if state.positive_limit if positive else state.negative_limit:
                    self.controller.halt(axis)
                    pos = self.controller.get_position(axis)
                    key = f"{axis.lower()}_{'positive' if positive else 'negative'}_limit"
                    self.limitCaptured.emit(key, pos)
                    self.captureMessage.emit(f"{name}={pos:.6f} mm")
                    return
                self.msleep(20)
        finally:
            try:
                self.controller.halt(axis)
            except Exception:
                pass
        raise RuntimeError(f"抓取{name}被停止。")

    def run(self):
        try:
            if self.controller.simulate:
                for axis in ("X", "Y"):
                    low, high = self.controller.soft_limits[axis]
                    self.limitCaptured.emit(f"{axis.lower()}_negative_limit", low)
                    self.limitCaptured.emit(f"{axis.lower()}_positive_limit", high)
                self.captureFinished.emit()
                return
            for axis in ("X", "Y"):
                if not self.controller.get_axis_status(axis).enabled:
                    raise RuntimeError(f"{axis} 轴未启动，无法抓取限位。")
                self._capture_one(axis, False)
                if self._stop_event:
                    self.captureStopped.emit("限位抓取已停止")
                    return
                self._capture_one(axis, True)
                if self._stop_event:
                    self.captureStopped.emit("限位抓取已停止")
                    return
            self.captureFinished.emit()
        except Exception as exc:
            if self._stop_event:
                self.captureStopped.emit(str(exc))
            else:
                self.captureError.emit(str(exc))


class MovementParametersCard(QFrame):
    parametersChanged = pyqtSignal()
    browseFolderRequested = pyqtSignal()
    browseFileRequested = pyqtSignal()
    saveConfigRequested = pyqtSignal()
    captureLimitsRequested = pyqtSignal()
    holdRequested = pyqtSignal()

    def __init__(self, show_limits=True):
        super().__init__()
        self.show_limits = bool(show_limits)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)

        title = QLabel("运动参数")
        title.setObjectName("cardTitle")
        layout.addWidget(title)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        self.controls = {}
        for row, (label, value) in enumerate((
            ("速度 (mm/s)", 5), ("步进 (mm)", .01), ("大步进 (mm)", .1),
            ("加速度 (mm/s²)", 50), ("减速度 (mm/s²)", 50),
        )):
            lab = QLabel(label)
            lab.setObjectName("smallFieldLabel")
            spin = QDoubleSpinBox()
            spin.setDecimals(6)
            spin.setRange(0.000001, 999999)
            spin.setValue(value)
            spin.setMinimumHeight(34)
            spin.valueChanged.connect(self.parametersChanged)
            grid.addWidget(lab, row, 0)
            grid.addWidget(spin, row, 1)
            self.controls[label] = spin
        layout.addLayout(grid)

        self.limit_edits = {}
        if self.show_limits:
            limit_box = QFrame()
            limit_box.setObjectName("scanFileBox")
            limit_layout = QVBoxLayout(limit_box)
            limit_layout.setContentsMargins(10, 9, 10, 10)
            limit_layout.setSpacing(7)
            limit_title = QLabel("软件限位")
            limit_title.setObjectName("cardTitle")
            limit_layout.addWidget(limit_title)
            limit_grid = QGridLayout()
            limit_grid.setHorizontalSpacing(8)
            limit_grid.setVerticalSpacing(6)
            for index, (key, label, value) in enumerate((
                ("x_negative_limit", "X 负限位", "-6.5"),
                ("x_positive_limit", "X 正限位", "6.5"),
                ("y_negative_limit", "Y 负限位", "-6.5"),
                ("y_positive_limit", "Y 正限位", "6.5"),
            )):
                row, col = divmod(index, 2)
                lab = QLabel(label + " (mm)")
                lab.setObjectName("smallFieldLabel")
                edit = QLineEdit(value)
                edit.setMinimumHeight(32)
                edit.textChanged.connect(self.parametersChanged)
                limit_grid.addWidget(lab, row * 2, col)
                limit_grid.addWidget(edit, row * 2 + 1, col)
                self.limit_edits[key] = edit
            limit_layout.addLayout(limit_grid)
            limit_buttons = QHBoxLayout()
            capture = QPushButton("抓取限位")
            capture.setObjectName("smallButton")
            hold = QPushButton("保持/停止")
            hold.setObjectName("disabledLikeButton")
            capture.clicked.connect(self.captureLimitsRequested)
            hold.clicked.connect(self.holdRequested)
            limit_buttons.addWidget(capture)
            limit_buttons.addWidget(hold)
            limit_layout.addLayout(limit_buttons)
            layout.addWidget(limit_box)

        file_box = QFrame()
        file_box.setObjectName("scanFileBox")
        file_layout = QVBoxLayout(file_box)
        file_layout.setContentsMargins(10, 9, 10, 10)
        file_layout.setSpacing(7)

        file_title = QLabel("扫描文件与图片文件夹")
        file_title.setObjectName("cardTitle")
        file_layout.addWidget(file_title)

        folder_label = QLabel("图片/结果文件夹")
        folder_label.setObjectName("smallFieldLabel")
        file_layout.addWidget(folder_label)
        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("选择图片文件夹 / Excel 保存文件夹")
        self.output_dir.setMinimumHeight(36)
        self.output_dir.textChanged.connect(self.parametersChanged)
        browse_folder = QPushButton("选择")
        browse_folder.setObjectName("smallButton")
        browse_folder.setFixedWidth(66)
        browse_folder.setMinimumHeight(36)
        browse_folder.clicked.connect(self.browseFolderRequested)
        folder_row.addWidget(self.output_dir, 1)
        folder_row.addWidget(browse_folder)
        file_layout.addLayout(folder_row)

        file_label = QLabel("文件扫描 Excel")
        file_label.setObjectName("smallFieldLabel")
        file_layout.addWidget(file_label)
        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        self.scan_file = QLineEdit()
        self.scan_file.setPlaceholderText("选择包含 X / Y / 图片列的 Excel 文件")
        self.scan_file.setMinimumHeight(36)
        self.scan_file.textChanged.connect(self.parametersChanged)
        browse_file = QPushButton("选择")
        browse_file.setObjectName("smallButton")
        browse_file.setFixedWidth(66)
        browse_file.setMinimumHeight(36)
        browse_file.clicked.connect(self.browseFileRequested)
        file_row.addWidget(self.scan_file, 1)
        file_row.addWidget(browse_file)
        file_layout.addLayout(file_row)

        self.galvo = GalvoScanSettings()
        self.galvo.changed.connect(self.parametersChanged)
        file_layout.addWidget(self.galvo)

        self.hologram = HologramSettings()
        self.hologram.changed.connect(self.parametersChanged)
        file_layout.addWidget(self.hologram)

        save = QPushButton("保存设置")
        save.setObjectName("primaryButton")
        save.setMinimumHeight(38)
        save.clicked.connect(self.saveConfigRequested)
        file_layout.addWidget(save)

        layout.addWidget(file_box)
        layout.addStretch()

    def values(self):
        return {
            "velocity": self.controls["速度 (mm/s)"].value(),
            "step": self.controls["步进 (mm)"].value(),
            "large_step": self.controls["大步进 (mm)"].value(),
            "acceleration": self.controls["加速度 (mm/s²)"].value(),
            "deceleration": self.controls["减速度 (mm/s²)"].value(),
        }

    def scan_file_values(self):
        return {
            "output_dir": self.output_dir.text().strip(),
            "scan_file": self.scan_file.text().strip(),
            **self.hologram.config_values(),
            **self.galvo.config_values(),
        }

    def config_values(self):
        values = {
            "velocity": self.controls["速度 (mm/s)"].value(),
            "step": self.controls["步进 (mm)"].value(),
            "large_step": self.controls["大步进 (mm)"].value(),
            "acceleration": self.controls["加速度 (mm/s²)"].value(),
            "deceleration": self.controls["减速度 (mm/s²)"].value(),
            "output_dir": self.output_dir.text(),
            "scan_file": self.scan_file.text(),
            **self.hologram.config_values(),
            **self.galvo.config_values(),
        }
        values.update({key: edit.text() for key, edit in self.limit_edits.items()})
        return values

    def apply_config(self, config: dict):
        self.hologram.apply_config(config)
        self.galvo.apply_config(config)
        for key, label in {
            "velocity": "速度 (mm/s)",
            "step": "步进 (mm)",
            "large_step": "大步进 (mm)",
            "acceleration": "加速度 (mm/s²)",
            "deceleration": "减速度 (mm/s²)",
        }.items():
            if key in config:
                try:
                    self.controls[label].setValue(float(config.get(key)))
                except Exception:
                    pass
        if "output_dir" in config:
            self.output_dir.setText(str(config.get("output_dir", "")))
        if "scan_file" in config:
            self.scan_file.setText(str(config.get("scan_file", "")))
        for key, edit in self.limit_edits.items():
            if key in config:
                edit.setText(str(config.get(key, "")))

    def limit_values(self):
        if not self.limit_edits:
            return {
                "x_negative_limit": -6.5,
                "x_positive_limit": 6.5,
                "y_negative_limit": -6.5,
                "y_positive_limit": 6.5,
            }
        values = {}
        for key, edit in self.limit_edits.items():
            text = edit.text().strip()
            if not text:
                raise ValueError(f"请填写{edit.placeholderText() or key}。")
            try:
                values[key] = float(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是数字，当前为：{text}") from exc
        if values["x_negative_limit"] >= values["x_positive_limit"]:
            raise ValueError("X 负限位必须小于 X 正限位。")
        if values["y_negative_limit"] >= values["y_positive_limit"]:
            raise ValueError("Y 负限位必须小于 Y 正限位。")
        return values

    def set_limit_value(self, key: str, value: float):
        if key in self.limit_edits:
            self.limit_edits[key].setText(f"{float(value):.6f}")


class LogCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumHeight(75)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)
        top = QHBoxLayout()
        title = QLabel("状态与日志"); title.setObjectName("cardTitle")
        clear = QPushButton("清空日志"); clear.setObjectName("smallButton")
        top.addWidget(title); top.addStretch(); top.addWidget(clear)
        layout.addLayout(top)
        self.log = QTextEdit(); self.log.setObjectName("logText"); self.log.setReadOnly(True)
        layout.addWidget(self.log)
        clear.clicked.connect(self.log.clear)

    def append_line(self, text, alarm=False):
        color = "#ef343a" if alarm else "#252a31"
        self.log.append(f"<span style='color:{color}'>{datetime.now():%H:%M:%S}&nbsp;&nbsp;&nbsp;{text}</span>")


class AxisDashboard(QWidget):
    axisCommandRequested = pyqtSignal(str, str)
    scanCommandRequested = pyqtSignal(str)
    scanParametersChanged = pyqtSignal()
    shutterCommandRequested = pyqtSignal(str)
    shutterRefreshRequested = pyqtSignal()
    shutterConnectRequested = pyqtSignal(str, bool)

    def __init__(self):
        super().__init__()
        root = QGridLayout(self)
        root.setContentsMargins(22, 18, 28, 14)
        root.setHorizontalSpacing(12); root.setVerticalSpacing(12)
        root.setColumnStretch(0, 38); root.setColumnStretch(1, 35); root.setColumnStretch(2, 25)
        root.setRowStretch(0, 46); root.setRowStretch(1, 42); root.setRowStretch(2, 12)
        self.x_card = AxisCard("X", "#2575e6")
        self.y_card = AxisCard("Y", "#45b95f")
        self.sc20 = SC20Card()
        self.scan = ScanControlCard()
        self.scan_settings = ScanSettingsCard(enable_uniform=True)
        self.uniform_scan_settings = UniformScanSettingsCard()
        self.logs = LogCard()
        root.addWidget(self.x_card, 0, 0); root.addWidget(self.y_card, 0, 1); root.addWidget(self.sc20, 0, 2)
        root.addWidget(self.scan, 1, 0); root.addWidget(self.scan_settings, 1, 1); root.addWidget(self.uniform_scan_settings, 1, 2)
        root.addWidget(self.logs, 2, 0, 1, 3)
        self.x_card.commandRequested.connect(self.axisCommandRequested)
        self.y_card.commandRequested.connect(self.axisCommandRequested)
        self.scan.commandRequested.connect(self.scanCommandRequested)
        self.scan.parametersChanged.connect(self.scanParametersChanged)
        self.scan_settings.parametersChanged.connect(self.scanParametersChanged)
        self.uniform_scan_settings.parametersChanged.connect(self.scanParametersChanged)
        self.sc20.commandRequested.connect(self.shutterCommandRequested)
        self.sc20.refreshRequested.connect(self.shutterRefreshRequested)
        self.sc20.connectRequested.connect(self.shutterConnectRequested)


class MotionSettingsPage(QWidget):
    parametersChanged = pyqtSignal()
    browseFolderRequested = pyqtSignal()
    browseFileRequested = pyqtSignal()
    saveConfigRequested = pyqtSignal()
    captureLimitsRequested = pyqtSignal()
    holdRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        self.params = MovementParametersCard(show_limits=True)
        self.params.setMaximumWidth(430)
        layout.addWidget(self.params)
        layout.addStretch()
        self.params.parametersChanged.connect(self.parametersChanged)
        self.params.browseFolderRequested.connect(self.browseFolderRequested)
        self.params.browseFileRequested.connect(self.browseFileRequested)
        self.params.saveConfigRequested.connect(self.saveConfigRequested)
        self.params.captureLimitsRequested.connect(self.captureLimitsRequested)
        self.params.holdRequested.connect(self.holdRequested)


class LeftSidebar(QFrame):
    pageSelected = pyqtSignal(int)
    connectRequested = pyqtSignal()
    disconnectRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("leftSidebar")
        self.setFixedWidth(236)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        panel = QFrame(); panel.setObjectName("connectionPanel"); panel.setFixedHeight(684)
        layout = QVBoxLayout(panel); layout.setContentsMargins(20, 17, 20, 14); layout.setSpacing(8)
        title = QLabel("连接"); title.setObjectName("sidebarSectionTitle"); layout.addWidget(title)
        layout.addWidget(QLabel("设备型号"))
        model = QComboBox(); model.addItem("L-412.XX9232_B × 2"); layout.addWidget(model)
        layout.addWidget(QLabel("连接方式"))
        self.mode = QComboBox(); self.mode.addItems(["ACS TCP/IP", "SPiiPlus 模拟器", "软件模拟"]); layout.addWidget(self.mode)
        layout.addWidget(QLabel("控制器 IP"))
        self.ip = QLineEdit("10.0.0.100"); layout.addWidget(self.ip)
        port_row = QHBoxLayout(); port_row.addWidget(QLabel("TCP 端口")); port_row.addStretch()
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(701); self.port.setFixedWidth(100)
        port_row.addWidget(self.port); layout.addLayout(port_row)
        axes = QHBoxLayout(); axes.addWidget(QLabel("轴号 X / Y")); axes.addStretch()
        self.x_axis = QSpinBox(); self.y_axis = QSpinBox(); self.y_axis.setValue(1)
        self.x_axis.setFixedWidth(58); self.y_axis.setFixedWidth(58)
        axes.addWidget(self.x_axis); axes.addWidget(self.y_axis); layout.addLayout(axes)
        self.connect_btn = QPushButton("连接"); self.connect_btn.setObjectName("primaryButton")
        self.disconnect_btn = QPushButton("断开连接"); self.disconnect_btn.setObjectName("disabledLikeButton")
        layout.addWidget(self.connect_btn); layout.addWidget(self.disconnect_btn)
        info_title = QLabel("设备信息"); info_title.setObjectName("sidebarSectionTitle"); layout.addWidget(info_title)
        grid = QGridLayout(); self.info = {}
        for row, (key, value) in enumerate((("通信", "未连接"), ("通道", "-"), ("库状态", "待检测"))):
            grid.addWidget(QLabel(key+":"), row, 0)
            lab = QLabel(value); lab.setObjectName("deviceInfoValue"); lab.setWordWrap(True)
            grid.addWidget(lab, row, 1); self.info[key] = lab
        layout.addLayout(grid); root.addWidget(panel)
        nav = QFrame(); nav.setObjectName("navPanel"); nv = QVBoxLayout(nav); nv.setContentsMargins(0,0,0,0); nv.setSpacing(0)
        group = QButtonGroup(self); group.setExclusive(True); self.buttons=[]
        for i, (text, icon) in enumerate((("轴控制","home"),("振镜控制","shutter"),("运动设置","gear"),("I/O 与状态","io"),("日志记录","log"),("关于","info"))):
            btn = NavButton(text, icon); btn.clicked.connect(lambda checked=False, n=i: self.pageSelected.emit(n))
            group.addButton(btn, i); nv.addWidget(btn); self.buttons.append(btn)
        nv.addStretch(); root.addWidget(nav, 1); self.buttons[0].setChecked(True)
        self.connect_btn.clicked.connect(self.connectRequested); self.disconnect_btn.clicked.connect(self.disconnectRequested)
        self.mode.currentIndexChanged.connect(self._mode_changed); self._mode_changed(); self.set_connected(False)

    def _mode_changed(self):
        enabled = self.mode.currentText() == "ACS TCP/IP"
        self.ip.setEnabled(enabled); self.port.setEnabled(enabled)

    def settings(self):
        return {"mode":self.mode.currentText(), "ip":self.ip.text().strip(), "port":self.port.value(), "x_axis":self.x_axis.value(), "y_axis":self.y_axis.value()}

    def set_connected(self, connected, communication="未连接", channel="-", library=""):
        self.connect_btn.setEnabled(not connected); self.disconnect_btn.setEnabled(connected)
        self.mode.setEnabled(not connected); self.x_axis.setEnabled(not connected); self.y_axis.setEnabled(not connected)
        self.ip.setEnabled(not connected and self.mode.currentText()=="ACS TCP/IP")
        self.port.setEnabled(not connected and self.mode.currentText()=="ACS TCP/IP")
        self.info["通信"].setText(communication); self.info["通道"].setText(str(channel))
        if library: self.info["库状态"].setText(library)


class EmptyPage(QFrame):
    def __init__(self, title, subtitle):
        super().__init__(); self.setObjectName("contentPage")
        layout = QVBoxLayout(self); layout.setContentsMargins(22,22,22,22)
        card = QFrame(); card.setObjectName("card"); inner=QVBoxLayout(card)
        t=QLabel(title); t.setObjectName("pageLargeTitle"); s=QLabel(subtitle); s.setWordWrap(True); s.setObjectName("pageSubtitle")
        inner.addWidget(t); inner.addWidget(s); inner.addStretch(); layout.addWidget(card)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("L-412 Linear Stage Controller")
        self.resize(1302, 1302); self.setMinimumSize(900, 650)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.motion = AcsMotionController()
        self.poller = None; self.scan_worker = None; self.scan_paused = False
        self.limit_capture_worker = None
        self.shutter_client = None; self.shutter_connected=False; self.shutter_simulate=False; self.shutter_open=False; self.current_shutter=0; self.timer_shutter_no=1
        self.scan_image_window = None
        self.hologram_player = None
        self.hologram_video = None
        self.galvo_runner = None
        self._position_feedback = {}
        self._hologram_context = {}
        self._pending_hologram_reports = []
        self.open_at=0; self.close_at=0
        self.zoom_factor = 1.0
        self._base_style = ""
        self.open_timer=QTimer(self); self.open_timer.setSingleShot(True); self.open_timer.timeout.connect(self._timer_open)
        self.close_timer=QTimer(self); self.close_timer.setSingleShot(True); self.close_timer.timeout.connect(self._timer_close)
        self.shutter_ui_timer=QTimer(self); self.shutter_ui_timer.setInterval(10); self.shutter_ui_timer.timeout.connect(self._update_countdown)
        self.clock_timer=QTimer(self); self.clock_timer.setInterval(1000); self.clock_timer.timeout.connect(self._update_clock)
        self.build_ui(); self.load_style(); self._setup_zoom_shortcuts(); self._apply_cursors(); self.populate_display_screens(); self.load_config(); self.clock_timer.start(); self._update_clock()
        self.add_log("软件已启动，等待连接控制器")
        QTimer.singleShot(100, self.refresh_scan_preview)

    def build_ui(self):
        central=QWidget(); central.setObjectName("centralRoot"); self.setCentralWidget(central)
        outer=QVBoxLayout(central); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        outer.addWidget(TitleBar(self))
        body=QFrame(); body.setObjectName("bodyFrame"); bl=QHBoxLayout(body); bl.setContentsMargins(0,0,0,0); bl.setSpacing(0)
        self.sidebar=LeftSidebar(); bl.addWidget(self.sidebar)
        right=QFrame(); right.setObjectName("rightArea"); rl=QVBoxLayout(right); rl.setContentsMargins(0,0,0,0); rl.setSpacing(0)
        rl.addWidget(self.build_topbar())
        self.stack=QStackedWidget(); self.dashboard=AxisDashboard(); self.stack.addWidget(self.dashboard)
        self.sc20_page = SerialControlPage(self)
        self.sc20_page.shutdownFinished.connect(self.close)
        self.stack.addWidget(self.sc20_page)
        self.motion_settings_page = MotionSettingsPage(); self.stack.addWidget(self.motion_settings_page)
        # Move the existing frequency editor; configuration still reads this same widget.
        uniform_layout = self.dashboard.uniform_scan_settings.layout()
        uniform_layout.insertWidget(
            uniform_layout.count() - 1,
            self.motion_settings_page.params.hologram.frequency_settings,
        )
        for title, sub in (("I/O 与状态","硬件限位由 ACS FAULT 实时读取。"),("日志记录","运行日志位于主页面底部。"),("关于","L-412 双轴控制软件。")):
            self.stack.addWidget(EmptyPage(title, sub))
        self.page_scroll = QScrollArea()
        self.page_scroll.setObjectName("pageScroll")
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.page_scroll.setWidget(self.stack)
        rl.addWidget(self.page_scroll,1); bl.addWidget(right,1); outer.addWidget(body,1); outer.addWidget(self.build_statusbar())
        self.sidebar.pageSelected.connect(self.switch_page); self.sidebar.connectRequested.connect(self.connect_device); self.sidebar.disconnectRequested.connect(self.disconnect_device)
        d=self.dashboard
        d.axisCommandRequested.connect(self.handle_axis_command); d.scanCommandRequested.connect(self.handle_scan_command); d.scanParametersChanged.connect(self.refresh_scan_preview)
        d.shutterCommandRequested.connect(self.handle_shutter_command); d.shutterRefreshRequested.connect(self.refresh_shutter_resources); d.shutterConnectRequested.connect(self.connect_shutter)
        self.motion_settings_page.parametersChanged.connect(self.refresh_scan_preview)
        self.motion_settings_page.browseFolderRequested.connect(self.choose_scan_folder)
        self.motion_settings_page.browseFileRequested.connect(self.choose_scan_file)
        self.motion_settings_page.saveConfigRequested.connect(lambda: self.save_config(show_message=True))
        self.motion_settings_page.captureLimitsRequested.connect(self.capture_limits)
        self.motion_settings_page.holdRequested.connect(self.hold_motion)

    def build_topbar(self):
        bar=QFrame(); bar.setObjectName("topTabBar"); bar.setFixedHeight(58); l=QHBoxLayout(bar); l.setContentsMargins(24,0,26,0); l.setSpacing(22)
        self.top_tabs=[]; group=QButtonGroup(self); group.setExclusive(True)
        for i,text in enumerate(("轴控制","振镜控制","运动设置")):
            b=QPushButton(text); b.setObjectName("topTabButton"); b.setCheckable(True); b.setFixedHeight(56); b.clicked.connect(lambda checked=False,n=i:self.switch_page(n)); group.addButton(b,i); l.addWidget(b); self.top_tabs.append(b)
        self.top_tabs[0].setChecked(True); l.addStretch()
        alarm_icon = QLabel("▲"); alarm_icon.setObjectName("alarmIcon"); l.addWidget(alarm_icon)
        self.alarm=QLabel("限位状态: 未连接"); self.alarm.setObjectName("alarmText"); l.addWidget(self.alarm)
        clear=QPushButton("清除报警"); clear.setObjectName("smallButton"); clear.clicked.connect(lambda:self.alarm.setText("限位状态: 正常")); l.addWidget(clear)
        l.addSpacing(8)
        zoom_out = QPushButton("-"); zoom_out.setObjectName("smallButton"); zoom_out.setFixedWidth(34)
        zoom_reset = QPushButton("100%"); zoom_reset.setObjectName("smallButton"); zoom_reset.setFixedWidth(58)
        zoom_in = QPushButton("+"); zoom_in.setObjectName("smallButton"); zoom_in.setFixedWidth(34)
        zoom_out.clicked.connect(lambda: self.adjust_zoom(-0.1))
        zoom_reset.clicked.connect(lambda: self.set_zoom(1.0))
        zoom_in.clicked.connect(lambda: self.adjust_zoom(0.1))
        self.zoom_label = zoom_reset
        l.addWidget(zoom_out); l.addWidget(zoom_reset); l.addWidget(zoom_in)
        return bar

    def build_statusbar(self):
        bar=QFrame(); bar.setObjectName("bottomStatusBar"); bar.setFixedHeight(31); l=QHBoxLayout(bar); l.setContentsMargins(20,0,22,0)
        self.conn_dot=Dot(); self.conn_label=QLabel("未连接"); self.conn_label.setObjectName("statusBarText"); self.time_label=QLabel(); self.time_label.setObjectName("statusBarText")
        l.addWidget(self.conn_dot); l.addWidget(self.conn_label); l.addStretch(); l.addWidget(QLabel("位置单位: mm")); l.addSpacing(18); l.addWidget(QLabel("速度单位: mm/s")); l.addSpacing(18); l.addWidget(self.time_label)
        for i in range(l.count()):
            w=l.itemAt(i).widget()
            if isinstance(w,QLabel): w.setObjectName("statusBarText")
        return bar

    @property
    def scan_card(self):
        return self.dashboard.scan

    @property
    def scan_settings_card(self):
        return self.dashboard.scan_settings

    @property
    def uniform_scan_settings_card(self):
        return self.dashboard.uniform_scan_settings

    def load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            scan_cfg = data.get("scan", {})
            self.scan_card.apply_config(scan_cfg)
            self.scan_settings_card.apply_config(scan_cfg)
            self.uniform_scan_settings_card.apply_config(scan_cfg)
            self.motion_settings_page.params.apply_config(scan_cfg)
            self.apply_soft_limits_from_ui()
            shutter = data.get("shutter", {})
            if "service_url" in shutter:
                self.dashboard.sc20.service_url.setText(str(shutter["service_url"]))
            ui_cfg = data.get("ui", {})
            if "zoom" in ui_cfg:
                self.set_zoom(float(ui_cfg["zoom"]))
            self.add_log("已加载上次保存的扫描配置")
        except Exception as exc:
            self.add_log("配置读取失败：" + str(exc), True)

    def save_config(self, show_message=False):
        try:
            self.apply_soft_limits_from_ui()
        except Exception as exc:
            self.add_log("限位配置无效：" + str(exc), True)
            if show_message:
                QMessageBox.critical(self, "保存配置失败", str(exc))
            return
        data = {
            "scan": {
                **self.scan_card.config_values(),
                **self.scan_settings_card.config_values(),
                **self.uniform_scan_settings_card.config_values(),
                **self.motion_settings_page.params.config_values(),
            },
            "shutter": {
                "service_url": self.dashboard.sc20.service_url.text().strip(),
            },
            "ui": {
                "zoom": self.zoom_factor,
            },
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.add_log("扫描配置已保存，下次打开会自动显示")
            if show_message:
                QMessageBox.information(self, "保存配置", f"配置已保存：\n{CONFIG_PATH}")
        except Exception as exc:
            self.add_log("配置保存失败：" + str(exc), True)
            if show_message:
                QMessageBox.critical(self, "保存配置失败", str(exc))

    def choose_scan_folder(self):
        current = self.motion_settings_page.params.output_dir.text().strip() or str(BASE_DIR)
        folder = QFileDialog.getExistingDirectory(self, "选择图片/扫描结果文件夹", current)
        if folder:
            self.motion_settings_page.params.output_dir.setText(folder)
            self.save_config(show_message=False)

    def choose_scan_file(self):
        current = self.motion_settings_page.params.scan_file.text().strip() or str(BASE_DIR)
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择文件扫描 Excel",
            current,
            "Excel 文件 (*.xlsx *.xlsm);;所有文件 (*.*)",
        )
        if file_path:
            self.motion_settings_page.params.scan_file.setText(file_path)
            self.save_config(show_message=False)

    def _update_clock(self): self.time_label.setText("时间: "+QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"))
    def add_log(self,text,alarm=False): self.dashboard.logs.append_line(text,alarm)

    def apply_soft_limits_from_ui(self):
        limits = self.motion_settings_page.params.limit_values()
        self.motion.soft_limits = {
            "X": (limits["x_negative_limit"], limits["x_positive_limit"]),
            "Y": (limits["y_negative_limit"], limits["y_positive_limit"]),
        }
        return self.motion.soft_limits

    def switch_page(self,index):
        self.stack.setCurrentIndex(index)
        if 0<=index<len(self.sidebar.buttons): self.sidebar.buttons[index].setChecked(True)
        for i,b in enumerate(self.top_tabs): b.setChecked(i==index)

    def connect_device(self):
        self._position_feedback = {}
        s=self.sidebar.settings()
        try:
            self.motion.axis_map={"X":s["x_axis"],"Y":s["y_axis"]}
            channel=self.motion.connect(s["ip"] or "127.0.0.1",s["port"],use_simulator=s["mode"]=="SPiiPlus 模拟器",software_simulation=s["mode"]=="软件模拟")
            comm="软件模拟" if s["mode"]=="软件模拟" else "SPiiPlus Simulator" if s["mode"]=="SPiiPlus 模拟器" else f'{s["ip"]}:{s["port"]}'
            self.sidebar.set_connected(True,comm,channel,"可用" if self.motion.library_available else "软件模拟")
            self.dashboard.x_card.set_connected(True); self.dashboard.y_card.set_connected(True)
            self.conn_dot.set_color("#45bd63"); self.conn_label.setText("已连接: "+comm); self.add_log("控制器连接成功："+comm)
            self.start_poller()
        except Exception as exc:
            self.add_log(f"连接失败：{exc}",True); QMessageBox.critical(self,"连接失败",str(exc))

    def disconnect_device(self):
        self._position_feedback = {}
        self.stop_scan(); self.stop_poller()
        try: self.motion.disconnect()
        except Exception as exc: self.add_log(f"断开异常：{exc}",True)
        self.sidebar.set_connected(False,"未连接","-","未安装" if not self.motion.library_available else "可用")
        self.dashboard.x_card.set_connected(False); self.dashboard.y_card.set_connected(False)
        self.conn_dot.set_color("#aab0b7"); self.conn_label.setText("未连接"); self.alarm.setText("限位状态: 未连接")

    def start_poller(self):
        self.stop_poller(); self.poller=PositionPoller(self.motion,200,self); self.poller.statesReady.connect(self.update_axis_states); self.poller.pollError.connect(lambda e:self.add_log("状态读取停止："+e,True)); self.poller.start()
    def stop_poller(self):
        if self.poller: self.poller.stop(); self.poller.wait(1200); self.poller=None

    def update_axis_states(self,states):
        for axis in ("X", "Y"):
            times = states.get('_sample_times', {}).get(axis)
            if times is not None:
                self._position_feedback[axis] = {'position': states[axis].position,
                                                  'began': times[0], 'ended': times[1]}
        self.dashboard.x_card.set_runtime_state(states["X"]); self.dashboard.y_card.set_runtime_state(states["Y"])
        alarms=[]
        for axis in ("X","Y"):
            st=states[axis]
            if st.negative_limit: alarms.append(axis+"- 硬件限位")
            if st.positive_limit: alarms.append(axis+"+ 硬件限位")
        self.alarm.setText("限位状态: "+("、".join(alarms) if alarms else "正常"))

    def handle_axis_command(self,axis,command):
        if not self.motion.connected: QMessageBox.warning(self,"提示","请先连接控制器"); return
        p=self.motion_settings_page.params.values(); card=self.dashboard.x_card if axis=="X" else self.dashboard.y_card
        try:
            self.apply_soft_limits_from_ui()
            if command=="enable_axis": self.motion.enable(axis); self.add_log(f"{axis} 轴已启动")
            elif command=="disable_axis": self.motion.halt(axis); self.motion.disable(axis); self.add_log(f"{axis} 轴已关闭")
            elif command=="stop": self.motion.halt(axis); self.add_log(f"{axis} 轴停止")
            elif command=="set_f_position":
                value = card.setpos_value.value()
                self.motion.halt(axis)
                self.motion.set_f_position(axis, value)
                self.add_log(f"{axis} 轴当前位置已重设为 {value:.6f} mm（SetFPosition）")
            else:
                self.motion.set_motion_parameters(axis,p["velocity"],p["acceleration"],p["deceleration"])
                if command=="move_absolute": self.motion.move_absolute(axis,card.target.value())
                elif command=="step_negative": self.motion.move_relative(axis,-p["step"])
                elif command=="step_positive": self.motion.move_relative(axis,p["step"])
                elif command=="large_negative": self.motion.move_relative(axis,-p["large_step"])
                elif command=="large_positive": self.motion.move_relative(axis,p["large_step"])
                elif command=="jog_negative":
                    target = self.motion.soft_limits[axis][0]
                    current = self.motion.get_position(axis)
                    if current <= target + 1e-6:
                        self.motion.halt(axis)
                        self.add_log(f"{axis} 轴已在负向软件限位 {target:.6f} mm")
                    else:
                        self.motion.move_absolute(axis, target)
                elif command=="jog_positive":
                    target = self.motion.soft_limits[axis][1]
                    current = self.motion.get_position(axis)
                    if current >= target - 1e-6:
                        self.motion.halt(axis)
                        self.add_log(f"{axis} 轴已在正向软件限位 {target:.6f} mm")
                    else:
                        self.motion.move_absolute(axis, target)
        except Exception as exc:
            self.add_log(f"{axis} 轴命令失败：{exc}",True); QMessageBox.critical(self,"轴命令失败",str(exc))

    def hold_motion(self):
        try:
            if self.limit_capture_worker and self.limit_capture_worker.isRunning():
                self.limit_capture_worker.stop()
            if self.motion.connected:
                self.motion.halt_all()
            self.add_log("已保持/停止 X、Y 轴运动")
        except Exception as exc:
            self.add_log("保持/停止失败：" + str(exc), True)
            QMessageBox.warning(self, "保持/停止", str(exc))

    def capture_limits(self):
        if not self.motion.connected:
            QMessageBox.warning(self, "抓取限位", "请先连接控制器")
            return
        if self.limit_capture_worker and self.limit_capture_worker.isRunning():
            QMessageBox.information(self, "抓取限位", "限位抓取正在进行中。")
            return
        try:
            self.apply_soft_limits_from_ui()
            p = self.motion_settings_page.params.values()
            self.limit_capture_worker = LimitCaptureWorker(
                self.motion,
                p["velocity"],
                p["acceleration"],
                p["deceleration"],
                parent=self,
            )
            self.limit_capture_worker.limitCaptured.connect(self.on_limit_captured)
            self.limit_capture_worker.captureMessage.connect(lambda msg: self.add_log(msg))
            self.limit_capture_worker.captureFinished.connect(self.on_limit_capture_finished)
            self.limit_capture_worker.captureStopped.connect(self.on_limit_capture_stopped)
            self.limit_capture_worker.captureError.connect(self.on_limit_capture_error)
            self.limit_capture_worker.finished.connect(lambda: setattr(self, "limit_capture_worker", None))
            self.limit_capture_worker.start()
            self.add_log("开始抓取 X/Y 轴硬件限位")
        except Exception as exc:
            self.add_log("抓取限位启动失败：" + str(exc), True)
            QMessageBox.critical(self, "抓取限位失败", str(exc))

    def on_limit_captured(self, key, value):
        self.motion_settings_page.params.set_limit_value(str(key), float(value))
        self.apply_soft_limits_from_ui()

    def on_limit_capture_finished(self):
        self.save_config(show_message=False)
        self.add_log("限位抓取完成，配置已保存")
        QMessageBox.information(self, "抓取限位", "限位抓取完成，已写入文本框并保存配置。")

    def on_limit_capture_stopped(self, msg):
        self.add_log("限位抓取已停止" + ("：" + msg if msg else ""))

    def on_limit_capture_error(self, msg):
        self.add_log("限位抓取失败：" + str(msg), True)
        QMessageBox.critical(self, "抓取限位失败", str(msg))

    @staticmethod
    def inclusive_values(start,end,step):
        if step<=0: raise ValueError("步进必须大于0")
        direction=1 if end>=start else -1; values=[]; value=start
        for _ in range(200000):
            if direction>0 and value>end+step*1e-9 or direction<0 and value<end-step*1e-9: break
            values.append(round(value,9)); value+=direction*step
        if not values or abs(values[-1]-end)>step*1e-7: values.append(end)
        return values

    @staticmethod
    def _norm_header(value) -> str:
        text = str(value or "").strip().lower()
        text = text.replace("（", "(").replace("）", ")")
        return re.sub(r"[\s_\-()/（）\.]+", "", text)

    @classmethod
    def _find_column(cls, headers, candidates):
        normalised = [cls._norm_header(h) for h in headers]
        candidate_set = {cls._norm_header(c) for c in candidates}
        for index, name in enumerate(normalised):
            if name in candidate_set:
                return index
        # 模糊匹配：例如 X坐标(mm)、图片路径 等
        for index, name in enumerate(normalised):
            for candidate in candidate_set:
                if candidate and candidate in name:
                    return index
        return None

    def read_file_scan_points(self, excel_file: str, image_folder: str):
        try:
            from openpyxl import load_workbook
        except Exception as exc:
            raise RuntimeError("文件扫描需要安装 openpyxl：pip install openpyxl") from exc

        path = Path(excel_file).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"文件扫描 Excel 不存在：{path}")
        wb = load_workbook(path, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValueError("文件扫描 Excel 为空。")

        first = list(rows[0])
        has_header = any(isinstance(v, str) and str(v).strip() for v in first)
        x_col = y_col = image_col = None
        start_row = 0
        if has_header:
            x_col = self._find_column(first, ["x", "x坐标", "x轴", "x位置", "x轴位置", "xmm", "x坐标mm", "xaxis"])
            y_col = self._find_column(first, ["y", "y坐标", "y轴", "y位置", "y轴位置", "ymm", "y坐标mm", "yaxis"])
            image_col = self._find_column(first, ["图片", "图片名称", "图片路径", "相位图", "全屏图片", "image", "imagepath", "imagename", "path", "file", "filename"])
            if x_col is not None and y_col is not None:
                if image_col is None:
                    raise ValueError("文件扫描 Excel 必须包含图片列，不能再按坐标自动拼接图片名。请添加 图片/图片名称/图片路径 列。")
                start_row = 1
            else:
                # 表头识别失败时按无表头处理：A=X，B=Y，C=图片。
                x_col, y_col, image_col, start_row = 0, 1, 2, 0
        else:
            x_col, y_col, image_col = 0, 1, 2

        points = []
        for excel_row_no, row in enumerate(rows[start_row:], start=start_row + 1):
            if row is None or not any(v is not None and str(v).strip() != "" for v in row):
                continue
            try:
                x_raw = row[x_col] if x_col < len(row) else None
                y_raw = row[y_col] if y_col < len(row) else None
                if x_raw is None or y_raw is None:
                    raise ValueError("X 或 Y 为空")
                x = float(x_raw)
                y = float(y_raw)
            except Exception as exc:
                raise ValueError(f"文件扫描 Excel 第 {excel_row_no} 行坐标无效：{row}") from exc

            image_text = ""
            if image_col is not None and image_col < len(row) and row[image_col] is not None:
                image_text = str(row[image_col]).strip()
            if not image_text:
                raise ValueError(
                    f"文件扫描 Excel 第 {excel_row_no} 行图片列为空。"
                    "文件扫描必须使用 Excel 中指定的文件名/路径，不再按坐标自动拼接图片名。"
                )
            points.append(ScanJobPoint(x, y, image_text))

        if not points:
            raise ValueError("文件扫描 Excel 没有有效扫描点。")
        if len(points) > 100000:
            raise ValueError(f"文件扫描点过多：{len(points)}")
        return points

    def read_file_scan_plus_points(self, excel_file: str):
        try:
            from openpyxl import load_workbook
        except Exception as exc:
            raise RuntimeError("文件扫描plus需要安装 openpyxl：pip install openpyxl") from exc

        path = Path(excel_file).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"文件扫描plus Excel 不存在：{path}")
        wb = load_workbook(path, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValueError("文件扫描plus Excel 为空。")

        first = list(rows[0])
        has_header = any(isinstance(v, str) and str(v).strip() for v in first)
        x_col = y_col = image_col = exposure_col = stable_col = shutter_col = None
        start_row = 0
        if has_header:
            x_col = self._find_column(first, ["x", "x坐标", "x轴", "x位置", "x轴位置", "xmm", "x坐标mm", "xaxis"])
            y_col = self._find_column(first, ["y", "y坐标", "y轴", "y位置", "y轴位置", "ymm", "y坐标mm", "yaxis"])
            image_col = self._find_column(first, ["图片", "图片名称", "图片路径", "相位图", "相位图名称", "全屏图片", "image", "imagepath", "imagename", "path", "file", "filename"])
            exposure_col = self._find_column(first, ["曝光", "曝光时间", "曝光时间ms", "exposure", "exposurems"])
            stable_col = self._find_column(first, ["稳定", "稳定时间", "稳定时间ms", "到位稳定", "到位稳定时间", "stable", "stablems"])
            shutter_col = self._find_column(first, ["快门", "快门通道", "通道", "快门号", "快门编号", "shutter", "shutterno", "shutterchannel", "channel"])
            if all(col is not None for col in (x_col, y_col, image_col, exposure_col, stable_col, shutter_col)):
                start_row = 1
            else:
                # 表头识别失败时按无表头处理：A=X，B=Y，C=相位图，D/E/F=曝光/稳定/快门通道。
                x_col, y_col, image_col, exposure_col, stable_col, shutter_col = 0, 1, 2, 3, 4, 5
                start_row = 0
        else:
            x_col, y_col, image_col, exposure_col, stable_col, shutter_col = 0, 1, 2, 3, 4, 5

        points = []
        for excel_row_no, row in enumerate(rows[start_row:], start=start_row + 1):
            if row is None or not any(v is not None and str(v).strip() != "" for v in row):
                continue
            if len(row) < 6:
                raise ValueError(f"文件扫描plus Excel 第 {excel_row_no} 行不足 6 列：{row}")
            try:
                x = float(row[x_col] if x_col < len(row) else None)
                y = float(row[y_col] if y_col < len(row) else None)
                image_text = str(row[image_col] if image_col < len(row) else "").strip()
                if not image_text:
                    raise ValueError("相位图名称为空")
                exposure_ms = ScanControlCard._int_ms_value(row[exposure_col] if exposure_col < len(row) else None, "曝光时间")
                stable_ms = ScanControlCard._int_ms_value(row[stable_col] if stable_col < len(row) else None, "稳定时间")
                shutter_no = int(ScanControlCard._int_ms_value(row[shutter_col] if shutter_col < len(row) else None, "快门通道"))
                if shutter_no not in (1, 2, 3):
                    raise ValueError("快门通道只能是 1、2 或 3")
            except Exception as exc:
                raise ValueError(
                    f"文件扫描plus Excel 第 {excel_row_no} 行无效。"
                    "前 6 列必须依次为 X、Y、相位图名称、曝光时间(ms)、稳定时间(ms)、快门通道(1/2/3)。"
                    f" 当前行：{row}"
                ) from exc
            points.append(ScanJobPoint(x, y, image_text, exposure_ms, stable_ms, exposure_ms, shutter_no))

        if not points:
            raise ValueError("文件扫描plus Excel 没有有效扫描点。")
        if len(points) > 100000:
            raise ValueError(f"文件扫描plus点过多：{len(points)}")
        return points

    def validate_points_within_limits(self, points, source_name="扫描点"):
        self.apply_soft_limits_from_ui()
        x_low, x_high = self.motion.soft_limits["X"]
        y_low, y_high = self.motion.soft_limits["Y"]
        errors = []
        for index, point in enumerate(points, start=1):
            if not x_low <= float(point.x) <= x_high:
                errors.append(f"第 {index} 点 X={point.x:.6f} 超出 [{x_low:.6f}, {x_high:.6f}]")
            if not y_low <= float(point.y) <= y_high:
                errors.append(f"第 {index} 点 Y={point.y:.6f} 超出 [{y_low:.6f}, {y_high:.6f}]")
            if len(errors) >= 5:
                break
        if errors:
            suffix = "" if len(errors) < 5 else "\n仅显示前 5 个越界项。"
            raise ValueError(f"{source_name}超出设定限位：\n" + "\n".join(errors) + suffix)

    def build_scan_points(self):
        settings = self.scan_settings_card.values()
        uniform_cfg = self.uniform_scan_settings_card.values(
            require_position=settings.get("mode") == "匀速运动曝光",
            require_close_position=settings.get("mode") == "匀速扫描",
        )
        file_cfg = self.motion_settings_page.params.scan_file_values()
        v = {**settings, **uniform_cfg, **file_cfg}
        if v["mode"] not in ("匀速运动曝光", "匀速扫描") and not v.get("output_dir"):
            raise RuntimeError("请在运动参数区域填写或选择图片/结果文件夹。")

        if v["mode"] in ("文件扫描", "文件扫描plus"):
            if not v.get("scan_file"):
                raise RuntimeError("文件扫描模式需要在运动参数区域选择 Excel 文件。")
            if v["mode"] == "文件扫描plus":
                points = self.read_file_scan_plus_points(v["scan_file"])
                self.validate_points_within_limits(points, "文件扫描plus坐标")
            else:
                points = self.read_file_scan_points(v["scan_file"], v["output_dir"])
                self.validate_points_within_limits(points, "文件扫描坐标")
            return points, v

        if v["mode"] == "匀速运动曝光":
            self.apply_soft_limits_from_ui()
            axis = v["uniform_axis"]
            low, high = self.motion.soft_limits[axis]
            preset = float(v["uniform_shutter_position"])
            if not low <= preset <= high:
                raise ValueError(f"开快门位置 {preset:.6f} mm 超出 {axis} 轴限位 [{low:.6f}, {high:.6f}] mm。")
            return [], v

        if v["mode"] == "匀速扫描":
            coord_values = self.scan_card.values(require_y_step=False)
            v.update(coord_values)
            xs = self.inclusive_values(v["x_start"], v["x_end"], v["x_step"])
            self.apply_soft_limits_from_ui()
            x_low, x_high = self.motion.soft_limits["X"]
            y_low, y_high = self.motion.soft_limits["Y"]
            for x in xs:
                if not x_low <= float(x) <= x_high:
                    raise ValueError(f"匀速扫描 X={x:.6f} 超出 X 轴限位 [{x_low:.6f}, {x_high:.6f}] mm。")
            for name, y in (
                ("Y 起始", v["y_start"]),
                ("Y 结束", v["y_end"]),
                ("开快门位置", v["uniform_shutter_position"]),
                ("关快门位置", v["uniform_shutter_close_position"]),
            ):
                if not y_low <= float(y) <= y_high:
                    raise ValueError(f"{name} {float(y):.6f} mm 超出 Y 轴限位 [{y_low:.6f}, {y_high:.6f}] mm。")
            y_start = float(v["y_start"])
            y_end = float(v["y_end"])
            open_y = float(v["uniform_shutter_position"])
            close_y = float(v["uniform_shutter_close_position"])
            if y_end >= y_start and not y_start <= open_y <= close_y <= y_end:
                raise ValueError("Y 正向匀速扫描时，必须满足 Y起始 <= 开快门位置 <= 关快门位置 <= Y结束。")
            if y_end < y_start and not y_start >= open_y >= close_y >= y_end:
                raise ValueError("Y 反向匀速扫描时，必须满足 Y起始 >= 开快门位置 >= 关快门位置 >= Y结束。")
            points = [ScanPoint(float(x), y_start) for x in xs]
            if len(points) > 100000:
                raise ValueError(f"匀速扫描线数过多：{len(points)}")
            return points, v

        coord_values = self.scan_card.values()
        v.update(coord_values)
        xs = self.inclusive_values(v["x_start"], v["x_end"], v["x_step"])
        ys = self.inclusive_values(v["y_start"], v["y_end"], v["y_step"])
        points = []
        snake = v["mode"] == "蛇形扫描"
        if v["order"] == "先 X 后 Y":
            for i, y in enumerate(ys):
                row = list(xs)
                if snake and i % 2:
                    row.reverse()
                points.extend(ScanPoint(x, y) for x in row)
        else:
            for i, x in enumerate(xs):
                col = list(ys)
                if snake and i % 2:
                    col.reverse()
                points.extend(ScanPoint(x, y) for y in col)
        if len(points) > 100000:
            raise ValueError(f"扫描点过多：{len(points)}")
        self.validate_points_within_limits(points, "坐标扫描点")
        return points, v


    def refresh_scan_preview(self):
        uniform_scan = self.scan_settings_card.mode.currentText() == "匀速扫描"
        self.uniform_scan_settings_card.shutter_selection.setEnabled(uniform_scan)
        self.scan_settings_card.scan_shutter.setEnabled(not uniform_scan)
        try:
            self.scan_card.set_y_step_required(self.scan_settings_card.mode.currentText() != "匀速扫描")
            points, _ = self.build_scan_points()
            mode = self.scan_settings_card.mode.currentText()
            if mode == "匀速运动曝光":
                self.scan_card.set_point_count(error_text="匀速运动曝光：1 次")
            elif mode == "匀速扫描":
                self.scan_card.set_point_count(error_text=f"匀速扫描：{len(points)} 条")
            else:
                self.scan_card.set_point_count(len(points))
        except Exception as exc:
            self.scan_card.set_point_count(error_text=str(exc))

    def handle_scan_command(self,command):
        if command=="start": self.start_scan()
        elif command=="pause": self.toggle_pause()
        elif command=="stop": self.stop_scan()

    def start_scan(self):
        if not self.motion.connected:
            QMessageBox.warning(self, "扫描", "请先连接控制器")
            return
        if not self.shutter_connected:
            QMessageBox.warning(self, "扫描", "扫描需要打开/关闭快门，请先连接 CH375 快门服务或启用主程序模拟模式。")
            return
        if self.scan_worker and self.scan_worker.isRunning():
            return
        try:
            sx = self.motion.get_axis_status("X"); sy = self.motion.get_axis_status("Y")
            if not sx.enabled or not sy.enabled:
                raise RuntimeError("扫描前必须先启动 X 轴和 Y 轴。")
            points, v = self.build_scan_points()
            if v["mode"] not in ("匀速运动曝光", "匀速扫描") and not v.get("output_dir"):
                raise RuntimeError("请填写扫描结果保存文件夹地址。")
            p = self.motion_settings_page.params.values()
            self.prepare_holograms(v)
            self.prepare_galvo_scan(v)
            self.save_config(show_message=False)
            if v["mode"] == "匀速运动曝光":
                self.scan_card.set_point_count(error_text="匀速运动曝光：1 次")
            elif v["mode"] == "匀速扫描":
                self.scan_card.set_point_count(error_text=f"匀速扫描：{len(points)} 条")
            else:
                self.scan_card.set_point_count(len(points))
            self.scan_card.set_running(True)
            self.scan_card.set_progress(0, 1 if v["mode"] == "匀速运动曝光" else len(points))
            self.scan_settings_card.set_countdown("准备扫描", -1, -1)
            self.scan_paused = False
            if v["mode"] == "匀速运动曝光":
                self.scan_worker = UniformExposureWorker(
                    self.motion,
                    v,
                    p,
                    self.dashboard.sc20.service_url.text().strip(),
                    self.shutter_simulate,
                    parent=self,
                )
            elif v["mode"] == "匀速扫描":
                self.scan_worker = UniformLineScanWorker(
                    self.motion,
                    v,
                    p,
                    self.dashboard.sc20.service_url.text().strip(),
                    self.shutter_simulate,
                    parent=self,
                )
                self.scan_worker.shutterCommandSent.connect(self.add_log)
                if self.hologram_player is not None and self.hologram_player.cache_session is not None:
                    self.scan_worker.imageColumnCompleted.connect(self.hologram_player.complete_column)
                if self.hologram_video is not None:
                    self.scan_worker.videoPrepareRequested.connect(self.hologram_video.prepare_column)
                self.scan_worker.lineStarted.connect(self.on_uniform_line_started)
                self.scan_worker.lineFinished.connect(self.on_uniform_line_finished)
            else:
                self.scan_worker = PhaseScanWorker(
                    self.motion,
                    points,
                    v,
                    p,
                    self.dashboard.sc20.service_url.text().strip(),
                    self.shutter_simulate,
                    parent=self,
                )
                self.scan_worker.pointStarted.connect(self.on_point_started)
                self.scan_worker.pointFinished.connect(self.on_point_finished)
            self.scan_worker.scanFinished.connect(self.on_scan_finished)
            self.scan_worker.scanStopped.connect(self.on_scan_stopped)
            self.scan_worker.scanError.connect(self.on_scan_error)
            self.scan_worker.scanCountdownChanged.connect(self.on_scan_countdown)
            if hasattr(self.scan_worker, "displayImageRequested"):
                self.scan_worker.displayImageRequested.connect(self.show_scan_image)
            if hasattr(self.scan_worker, "closeImageRequested"):
                self.scan_worker.closeImageRequested.connect(self.close_scan_image)
            self.scan_worker.finished.connect(self.on_scan_thread_finished)
            if self.hologram_player is not None:
                self.hologram_player.arm()
            if self.hologram_video is not None:
                self.hologram_video.arm()
            self.scan_worker.start()
            if v["mode"] == "匀速运动曝光":
                self.add_log(
                    f"开始匀速运动曝光：{v['uniform_axis']} 轴到"
                    f"{'正' if v['uniform_limit'] == 'positive' else '负'}限位，"
                    f"开快门位置 {v['uniform_shutter_position']:.6f} mm"
                )
            elif v["mode"] == "匀速扫描":
                self.add_log(
                    f"开始匀速扫描，共 {len(points)} 条；"
                    f"Y 从 {v['y_start']:.6f} 到 {v['y_end']:.6f}，"
                    f"开快门 {v['uniform_shutter_position']:.6f}，"
                    f"关快门 {v['uniform_shutter_close_position']:.6f}，"
                    f"同时打开快门 {'、'.join(str(n) for n in v['uniform_shutters'])}"
                )
            else:
                first = points[0]
                self.add_log(f"开始扫描，共 {len(points)} 点；先移动到起始位置 X={first.x:.6f}, Y={first.y:.6f}")
        except Exception as exc:
            self.dispose_holograms()
            self.scan_card.set_running(False)
            self.release_galvo_scan()
            self.add_log("扫描启动失败：" + str(exc), True)
            QMessageBox.critical(self, "扫描失败", str(exc))

    def toggle_pause(self):
        if not self.scan_worker or not self.scan_worker.isRunning():
            return
        self.scan_paused = not self.scan_paused
        self.scan_worker.set_paused(self.scan_paused)
        self.scan_card.pause_btn.setText("继续扫描" if self.scan_paused else "暂停扫描")
        self.add_log("扫描已暂停" if self.scan_paused else "扫描已继续")

    def stop_scan(self):
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_card.set_running(True, stopping=True)
            self.scan_worker.stop()
            self.add_log("正在实时停止扫描，已向 X/Y 发送 Halt，并关闭快门")

    def on_point_started(self, index, total, x, y):
        self._hologram_context = {'kind': '点扫描', 'index': index, 'x': x, 'y': y}
        self.scan_card.set_progress(index - 1, total)
        self.add_log(f"扫描点 {index}/{total}：移动到 X={x:.6f}, Y={y:.6f}，到位后等待稳定时间")

    def on_point_finished(self, index, total, x, y, image_path):
        self.scan_card.set_progress(index, total)
        self.add_log(f"扫描点 {index}/{total} 完成：{Path(image_path).name}")

    def on_uniform_line_started(self, index, total, x, y_start, y_end):
        self._hologram_context = {'kind': '匀速扫描线', 'index': index, 'x': x}
        self.scan_card.set_progress(index - 1, total)
        self.add_log(f"匀速扫描线 {index}/{total}：X={x:.6f}，Y 从 {y_start:.6f} 匀速运动到 {y_end:.6f}")

    def on_uniform_line_finished(self, index, total, x, y_start, y_end):
        self.scan_card.set_progress(index, total)
        self.add_log(f"匀速扫描线 {index}/{total} 完成：X={x:.6f}")

    def prepare_galvo_scan(self, values):
        if not values.get('galvo_enabled'):
            if values.get('mode') == '匀速扫描':
                self.add_log('振镜联动未启用，本次匀速扫描不发送振镜点列。')
            return
        if values.get('mode') != '匀速扫描':
            raise ValueError('振镜联动仅用于匀速扫描，请选择匀速扫描模式或取消振镜联动。')
        if self.galvo_runner is not None:
            raise RuntimeError('上一轮振镜联动尚未释放，请先停止扫描。')
        count = len(UniformLineScanWorker._inclusive_values(
            float(values['x_start']), float(values['x_end']), float(values['x_step'])))
        columns = load_galvo_master(values.get('galvo_master_excel', ''), count, values.get('galvo_points_folder', ''))
        controller = self.sc20_page.reserve_for_scan()
        try:
            runner = GalvoScanRunner(controller, columns,
                [values.get('galvo_ch1_closed', False), values.get('galvo_ch2_closed', False)],
                feedback=self.sc20_page.worker.feedback)
        except Exception:
            self.sc20_page.release_from_scan()
            raise
        self.galvo_runner = runner
        values['_galvo_runner'] = runner
        self.add_log(f'振镜总表已检查：本次 {count} 列，两路点列已预加载；扫描中由点列调度器读取位移反馈。')

    def release_galvo_scan(self):
        runner = self.galvo_runner
        if runner is None:
            return
        try:
            runner.stop_line()
        except Exception as exc:
            self.add_log('振镜联动清理失败：' + str(exc), True)
        if runner.safe_to_release:
            self.galvo_runner = None
            self.sc20_page.release_from_scan()
            self.add_log('振镜联动结束，振镜串口已释放。')

    def prepare_holograms(self, values):
        self.dispose_holograms()
        if values.get('hologram_video_enabled'):
            if values.get('hologram_enabled'):
                raise ValueError('全息图片和全息视频不能同时启用。')
            self.prepare_hologram_video(values)
            return
        if not values.get("hologram_enabled"):
            return
        entries = load_hologram_excel(values.get("hologram_excel", ""), values.get("hologram_folder", ""))
        paths = [entry.path for entry in entries]
        report_directory = Path(values.get('output_dir') or Path(values['hologram_excel']).parent).expanduser().resolve()
        report_directory.mkdir(parents=True, exist_ok=True)
        # Check destination before any motion or exposure begins.
        import tempfile
        with tempfile.TemporaryFile(dir=report_directory):
            pass
        sequences = None
        if values.get("mode") == "匀速扫描":
            count = len(UniformLineScanWorker._inclusive_values(
                float(values['x_start']), float(values['x_end']), float(values['x_step'])))
            sequences, rates = column_sequences(entries, count, float(values['hologram_delay_ms']),
                                                 values['hologram_frequencies'])
            offsets = sequences[1][1]
        else:
            offsets, rates = schedule_offsets(len(paths), float(values["hologram_delay_ms"]),
                                              values["hologram_frequencies"])
        screens = QApplication.screens()
        index = int(values.get("display_index", 0))
        if not 0 <= index < len(screens):
            raise ValueError("全息图显示器不存在，请重新选择显示器。")
        refresh = screens[index].refreshRate()
        if refresh > 0 and max(rates) > refresh:
            raise ValueError(f"全息图频率超过所选显示器报告的刷新率 {refresh:g} Hz，无法逐张显示。")
        timeline = ShutterTimeline()
        self.close_scan_image()
        player = HologramPlayer(paths, offsets, screens[index], timeline, sequences=sequences)
        self.hologram_player = player
        player.sequences = sequences
        self._hologram_context = {'kind': values.get('mode', ''), 'index': 1}
        player.report_entries = entries
        player.report_config = dict(values)
        player.report_directory = report_directory
        player.position_provider = lambda: (dict(self._hologram_context),
                                             {axis: dict(sample) for axis, sample in self._position_feedback.items()})
        player.message.connect(self.add_log)
        player.dismissed.connect(self.stop_scan)
        values['_hologram_timeline'] = timeline
        values['_hologram_first_path'] = str(paths[0])
        if player.cache_session is not None:
            values['_hologram_cache_session'] = player.cache_session
        self.add_log(f"全息图 Excel 已读取 {len(entries)} 条记录、{len(set(paths))} 张不同图片；"
                     "按表格顺序播放，跳过空行，重复名称保留。")
        if sequences is not None:
            self.add_log(f"全息图按列播放：第 k 条扫描线使用 n=k，共 {len(sequences)} 列；"
                         f"首次仅预加载第1～{min(5, len(sequences))}列，打印完成逐列释放并补载，最多缓存5列。")
        self.add_log("m仅供查看，匀速扫描按n选择图片；原始像素显示，每列完成释放缓存，额外只保留当前显示帧。")

    def prepare_hologram_video(self, values):
        if values.get('mode') != '匀速扫描':
            raise ValueError('全息视频切换仅支持匀速扫描，请切换模式或取消视频勾选。')
        try:
            from hologram_video import HologramVideoPlayer, VideoSession, video_columns
        except ImportError as exc:
            raise RuntimeError('当前Python缺少Qt视频播放组件，请安装完整PyQt6。') from exc
        entries = load_hologram_excel(values.get('hologram_excel', ''), values.get('hologram_folder', ''))
        count = len(UniformLineScanWorker._inclusive_values(
            float(values['x_start']), float(values['x_end']), float(values['x_step'])))
        columns = video_columns(entries, count)
        screens = QApplication.screens()
        index = int(values.get('display_index', 0))
        if not 0 <= index < len(screens):
            raise ValueError('全息视频显示器不存在，请重新选择显示器。')
        session = VideoSession()
        timeline = ShutterTimeline()
        self.close_scan_image()
        player = HologramVideoPlayer(columns, screens[index], timeline, session)
        self.hologram_video = player
        player.message.connect(self.add_log)
        player.dismissed.connect(self.stop_scan)
        values['_hologram_timeline'] = timeline
        values['_hologram_video_session'] = session
        self.add_log(f'全息视频Excel已检查：{count}列、{sum(len(v) for v in columns.values())}条记录；'
                     '开快门播放，关快门停止并关闭画面，按n准备下一列。')
        self.add_log('视频使用原始像素及自身帧率，无音频输出；图片Hz和启动延迟不用于视频。')

    def finish_holograms(self, status="扫描完成"):
        if self.hologram_video is not None:
            self.hologram_video.finish()
        player = self.hologram_player
        if player is None:
            return
        player.finish()
        if player.report_path is not None or not player._has_started:
            return
        try:
            player.report_path = export_report(player, status)
            self.add_log("全息图切换核对 Excel 已保存：" + str(player.report_path))
        except Exception as exc:
            self.add_log("全息图切换记录导出失败，记录暂保留在内存：" + str(exc), True)
            if player not in self._pending_hologram_reports:
                self._pending_hologram_reports.append(player)

    def dispose_holograms(self):
        video = self.hologram_video
        self.hologram_video = None
        if video is not None:
            video._armed = False
            video.finish()
            video.close()
            video.deleteLater()
        player = self.hologram_player
        if player is not None and not player._finished:
            self.finish_holograms("停止或关闭预览")
        self.hologram_player = None
        if player is not None:
            player._armed = False
            player.close()
            player.deleteLater()

    def show_scan_image(self, image_path: str, display_index: int):
        self.close_scan_image()
        screens = QApplication.screens()
        if not screens:
            self.add_log("未检测到显示器，无法全屏显示相位图", True)
            return
        index = max(0, min(int(display_index), len(screens) - 1))
        self.scan_image_window = FullscreenImageWindow(image_path, screens[index])
        self.scan_image_window.showFullScreen()
        self.add_log(f"显示器 {index + 1} 全屏显示：{Path(image_path).name}")

    def close_scan_image(self):
        if self.scan_image_window is not None:
            try:
                self.scan_image_window.close()
                self.scan_image_window.deleteLater()
            except Exception:
                pass
            self.scan_image_window = None

    def on_scan_countdown(self, phase, stable_ms, shutter_ms):
        self.scan_settings_card.set_countdown(str(phase), int(stable_ms), int(shutter_ms))

    def on_scan_finished(self, excel_path):
        self.finish_holograms()
        self.close_scan_image()
        self.scan_card.set_running(False)
        if str(excel_path) != "匀速扫描完成":
            self.scan_card.set_progress(1, 1)
        self.scan_settings_card.set_countdown("扫描完成", 0, 0)
        if str(excel_path).lower().endswith((".xlsx", ".xlsm", ".xls")):
            self.add_log("扫描完成，Excel 已保存：" + str(excel_path))
            if self.hologram_player is None and self.hologram_video is None:
                QMessageBox.information(self, "扫描完成", f"扫描完成，Excel 已保存：\n{excel_path}")
        else:
            self.add_log(str(excel_path) or "扫描完成")
            if self.hologram_player is None and self.hologram_video is None:
                QMessageBox.information(self, "扫描完成", str(excel_path) or "扫描完成")

    def on_scan_stopped(self, msg=""):
        self.finish_holograms("扫描停止：" + str(msg))
        self.dispose_holograms()
        self.close_scan_image()
        self.scan_card.set_running(False)
        self.scan_settings_card.set_countdown("扫描已停止", -1, -1)
        self.add_log("扫描已实时停止" + ("：" + msg if msg else ""))

    def on_scan_error(self, msg):
        self.finish_holograms("扫描错误：" + str(msg))
        self.dispose_holograms()
        self.close_scan_image()
        self.scan_card.set_running(False)
        self.scan_settings_card.set_countdown("扫描错误", -1, -1)
        self.add_log("扫描错误：" + msg, True)
        QMessageBox.critical(self, "扫描错误", msg)

    def on_scan_thread_finished(self):
        self.release_galvo_scan()
        self.scan_worker = None
        self.scan_paused = False
        self.scan_card.pause_btn.setText("暂停扫描")


    def refresh_shutter_resources(self):
        c = self.dashboard.sc20
        if c.simulate.isChecked():
            c.countdown.setText("服务：主程序模拟模式，无需 32 位服务")
            self.add_log("快门控制处于主程序模拟模式")
            return
        try:
            client = CH375ShutterClient(c.service_url.text().strip())
            health = client.health()
            bits = health.get("architecture_bits", "?")
            mode = "模拟服务" if health.get("simulate") else "真实 CH375 服务"
            probe = client.probe()
            dll_name = Path(str(probe.get("dll_path", "CH375DLL.DLL"))).name
            c.countdown.setText(
                f"检测成功：{mode}，Python {bits} 位，设备索引 {probe.get('device_index', 0)}"
            )
            self.add_log(
                f"快门检测成功：服务在线；DLL={dll_name}；"
                f"OpenDevice={probe.get('open_result')}；CloseDevice={probe.get('close_result')}"
            )
        except Exception as exc:
            c.countdown.setText("服务或设备检测失败")
            self.add_log("快门服务/设备检测失败：" + str(exc), True)

    def connect_shutter(self, service_url, simulate):
        if self.shutter_connected:
            self.disconnect_shutter()
            return
        try:
            self.shutter_simulate = bool(simulate)
            if self.shutter_simulate:
                identity = "主程序模拟快门"
                self.shutter_client = None
            else:
                self.shutter_client = CH375ShutterClient(service_url)
                info = self.shutter_client.health()
                bits = int(info.get("architecture_bits", 0) or 0)
                if not info.get("simulate") and bits != 32:
                    raise RuntimeError(f"快门服务必须由 32 位 Python 运行，当前报告为 {bits} 位。")

                # 先真实执行 OpenDevice/CloseDevice，避免“服务能检测但设备不可用”。
                probe = self.shutter_client.probe()
                identity = (
                    f"CH375 32 位服务，设备索引 {probe.get('device_index', 0)}"
                    if not info.get("simulate")
                    else "CH375 模拟服务"
                )
                # 连接时再按已验证流程 Open -> Write 0x0A -> Close，确保安全状态。
                close_result = self.shutter_client.close_shutter()
                self.add_log(
                    f"快门设备探测成功：OpenDevice={probe.get('open_result')}，"
                    f"关闭命令返回={close_result.get('result')}"
                )
            self.shutter_connected = True
            self.shutter_open = False
            self.current_shutter = 0
            self.dashboard.sc20.set_connected(True, "服务：" + identity)
            self.dashboard.sc20.set_open(False)
            self.add_log("快门控制连接成功：" + identity)
        except Exception as exc:
            self.shutter_client = None
            self.shutter_connected = False
            self.dashboard.sc20.set_connected(False, "服务：连接失败")
            self.add_log("快门连接失败：" + str(exc), True)
            QMessageBox.critical(self, "快门连接失败", str(exc))

    def disconnect_shutter(self):
        self._stop_shutter_timers()
        try:
            if self.shutter_connected:
                self._close_shutter()
        except Exception as exc:
            self.add_log("断开前关闭快门失败：" + str(exc), True)
        finally:
            self.shutter_client = None
            self.shutter_connected = False
            self.shutter_open = False
            self.current_shutter = 0
            self.dashboard.sc20.set_connected(False, "服务：未连接")
            self.dashboard.sc20.set_open(False)
            self.add_log("快门控制已断开")

    def handle_shutter_command(self, cmd):
        try:
            if cmd.startswith("toggle_"):
                self._stop_shutter_timers()
                selected = self.dashboard.sc20.selected_manual_shutters()
                if selected:
                    self._open_shutters(selected)
                    text = "、".join(str(n) for n in selected)
                    self.dashboard.sc20.countdown.setText(f"手动：快门 {text} 已打开")
                else:
                    self._close_shutter()
                    self.dashboard.sc20.countdown.setText("手动：全部快门已关闭")
            elif cmd == "close":
                self._stop_shutter_timers()
                self._close_shutter()
                self.dashboard.sc20.countdown.setText("手动：全部快门已关闭")
            elif cmd == "start_timer":
                self.start_shutter_timer()
            elif cmd == "stop_timer":
                self._stop_shutter_timers()
                self._close_shutter()
                self.dashboard.sc20.countdown.setText("计时：已停止并关闭")
        except Exception as exc:
            self.add_log("快门控制失败：" + str(exc), True)
            QMessageBox.warning(self, "快门控制", str(exc))

    def _require_shutter(self):
        if not self.shutter_connected:
            raise RuntimeError("请先连接 32 位 CH375 快门服务，或启用主程序模拟模式。")

    def _open_shutter(self, shutter_no=None):
        self._require_shutter()
        shutter_no = int(shutter_no or self.dashboard.sc20.selected_shutter())
        if shutter_no not in (1, 2, 3):
            raise ValueError("快门编号只能是 1、2 或 3。")
        command = {1: 0x8A, 2: 0x4A, 3: 0x2A}[shutter_no]
        if self.shutter_simulate:
            print(f"[主程序模拟] 快门 {shutter_no} 打开，命令 0x{command:02X}")
        else:
            if self.shutter_client is None:
                raise RuntimeError("32 位快门服务客户端不存在。")
            self.shutter_client.open_shutter(shutter_no)
        self.shutter_open = True
        self.current_shutter = shutter_no
        self.dashboard.sc20.set_open(True, shutter_no)
        self.add_log(f"快门 {shutter_no} 已打开，命令 0x{command:02X}")

    def _open_shutters(self, shutter_numbers):
        self._require_shutter()
        shutters = sorted({int(n) for n in shutter_numbers})
        if not shutters or any(n not in (1, 2, 3) for n in shutters):
            raise ValueError("快门编号只能是 1、2 或 3。")
        command = CH375ShutterClient.shutters_command(shutters)
        if self.shutter_simulate:
            print(f"[主程序模拟] 快门 {shutters} 同时打开，组合命令 0x{command:02X}")
        else:
            if self.shutter_client is None:
                raise RuntimeError("32 位快门服务客户端不存在。")
            self.shutter_client.write_command(command)
        self.shutter_open = True
        self.current_shutter = command
        self.dashboard.sc20.set_open(True, shutter_numbers=shutters)
        text = "、".join(str(n) for n in shutters)
        self.add_log(f"快门 {text} 已打开，组合命令 0x{command:02X}")

    def _close_shutter(self):
        self._require_shutter()
        if self.shutter_simulate:
            print("[主程序模拟] 关闭全部快门，命令 0x0A")
        else:
            if self.shutter_client is None:
                raise RuntimeError("32 位快门服务客户端不存在。")
            self.shutter_client.close_shutter()
        self.shutter_open = False
        self.current_shutter = 0
        self.dashboard.sc20.set_open(False)
        self.add_log("全部快门已关闭，命令 0x0A")

    def _stop_shutter_timers(self):
        self.open_timer.stop()
        self.close_timer.stop()
        self.shutter_ui_timer.stop()

    def start_shutter_timer(self):
        self._require_shutter()
        self._stop_shutter_timers()
        c = self.dashboard.sc20
        self.timer_shutter_no = c.selected_shutter()
        delay = round(c.delay.value())
        hold = round(c.hold.value())
        delay_close = round(c.delay_close.value())
        now = QDateTime.currentMSecsSinceEpoch()
        self.open_at = now + delay
        self.close_at = now + delay + hold + delay_close
        self.open_timer.start(delay)
        self.shutter_ui_timer.start()
        c.countdown.setText(f"计时：快门 {self.timer_shutter_no} 将在 {delay} ms 后打开")
        self.add_log(
            f"快门 {self.timer_shutter_no} 定时开始：延时 {delay} ms，保持 {hold} ms，延时关闭 {delay_close} ms"
        )

    def _timer_open(self):
        self._open_shutter(self.timer_shutter_no)
        self.close_timer.start(
            round(self.dashboard.sc20.hold.value() + self.dashboard.sc20.delay_close.value())
        )

    def _timer_close(self):
        self._close_shutter()
        self.shutter_ui_timer.stop()
        self.dashboard.sc20.countdown.setText("计时：完成，全部快门已关闭")

    def _update_countdown(self):
        now = QDateTime.currentMSecsSinceEpoch()
        c = self.dashboard.sc20
        if not self.shutter_open:
            c.countdown.setText(
                f"计时：快门 {self.timer_shutter_no} 距离打开还有 {max(0, self.open_at-now)} ms"
            )
        else:
            c.countdown.setText(f"计时：距离关闭还有 {max(0, self.close_at-now)} ms")

    def _apply_cursors(self):
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        for combo in self.findChildren(QComboBox):
            combo.setCursor(Qt.CursorShape.PointingHandCursor)

    def _setup_zoom_shortcuts(self):
        QApplication.instance().installEventFilter(self)
        for key, delta in (("Ctrl++", 0.1), ("Ctrl+=", 0.1), ("Ctrl+-", -0.1)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda d=delta: self.adjust_zoom(d))
        reset = QShortcut(QKeySequence("Ctrl+0"), self)
        reset.activated.connect(lambda: self.set_zoom(1.0))

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = 0.1 if event.angleDelta().y() > 0 else -0.1
            self.adjust_zoom(delta)
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def adjust_zoom(self, delta):
        self.set_zoom(self.zoom_factor + float(delta))

    def set_zoom(self, factor):
        self.zoom_factor = max(0.75, min(1.5, float(factor)))
        self._apply_zoom_style()
        if hasattr(self, "zoom_label"):
            self.zoom_label.setText(f"{round(self.zoom_factor * 100):d}%")

    def _apply_zoom_style(self):
        if not self._base_style:
            return

        def scale_match(match):
            value = float(match.group(2))
            return f"{match.group(1)}{max(1, round(value * self.zoom_factor))}px"

        scaled = re.sub(
            r"(font-size:\s*)(\d+(?:\.\d+)?)px",
            scale_match,
            self._base_style,
        )
        self.setStyleSheet(scaled)
        if hasattr(self, "stack"):
            self.stack.setMinimumSize(round(1030 * self.zoom_factor), round(760 * self.zoom_factor))

    def load_style(self):
        path=BASE_DIR/"styles.qss"
        if path.exists():
            self._base_style = path.read_text(encoding="utf-8")
            self._apply_zoom_style()

    def populate_display_screens(self):
        self.scan_settings_card.set_screens(QApplication.screens())

    def closeEvent(self,event):
        self.stop_scan()
        self.dispose_holograms()
        self.close_scan_image()
        if not self.sc20_page.request_shutdown():
            event.ignore()
            return
        try:
            self.save_config(show_message=False)
        except Exception:
            pass
        self.stop_scan()
        if self.scan_worker: self.scan_worker.wait(2500)
        if self.limit_capture_worker and self.limit_capture_worker.isRunning():
            self.limit_capture_worker.stop()
            self.limit_capture_worker.wait(2500)
        self.stop_poller()
        try:
            if self.shutter_connected:
                self._close_shutter()
        except Exception:
            pass
        self.shutter_client = None
        self.motion.close(); event.accept()


if __name__ == "__main__":
    app=QApplication(sys.argv); app.setFont(QFont("Microsoft YaHei UI",10))
    win=MainWindow(); win.show(); sys.exit(app.exec())
