"""Original-pixel hologram playback, selected by scan column and shutter timing."""
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImageReader, QPixmap, QPainter
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                            QCheckBox, QPushButton, QDoubleSpinBox, QFileDialog)


@dataclass(frozen=True)
class HologramEntry:
    path: Path
    m: object
    n: object
    group: int
    excel_row: int


def load_hologram_excel(filename, folder=""):
    """Keep worksheet order and duplicate image rows; n selects a scan column."""
    file = Path(filename).expanduser()
    if not filename or not file.is_file():
        raise ValueError('请选择有效的全息图 Excel 文件。')
    base = Path(folder).expanduser() if folder.strip() else file.parent
    workbook = load_workbook(file, read_only=True, data_only=False)
    entries = []
    try:
        rows = iter(enumerate(workbook.active.iter_rows(values_only=True), 1))
        def blank(row):
            return all(value is None or str(value).strip() == '' for value in row)
        first = next(((i, row) for i, row in rows if not blank(row)), None)
        if first is None:
            raise ValueError('全息图 Excel 为空。')
        def normal(value):
            return re.sub(r'\s+', '', str(value)).replace('（', '(').replace('）', ')').lower()
        header = [normal(value) for value in first[1]]
        header = ['列号n(y向)' if name == '行号n(y向)' else name for name in header]
        required = ['相位图名称', '行号m(x向)', '列号n(y向)']
        for name in required:
            if header.count(name) != 1:
                raise ValueError(f'全息图 Excel 必须包含且仅包含一个“{name}”字段。')
        columns = [header.index(name) for name in required]
        group = 0
        boundary = False
        for row_number, row in rows:
            if blank(row):
                boundary = bool(entries)
                continue
            if boundary:
                group += 1
                boundary = False
            try:
                name, m, n = [row[col] if col < len(row) else None for col in columns]
                if name is None or not str(name).strip() or str(name).startswith('='):
                    raise ValueError('相位图名称必须为非空文件名，不支持公式')
                path = Path(str(name).strip()).expanduser()
                path = (path if path.is_absolute() else base / path).resolve()
                if not path.is_file():
                    raise ValueError(f'图片/视频文件不存在：{path}')
                entries.append(HologramEntry(path, m, n, group, row_number))
            except (ValueError, TypeError) as exc:
                raise ValueError(f'全息图 Excel 第 {row_number} 行：{exc}') from exc
    finally:
        workbook.close()
    if not entries:
        raise ValueError('全息图 Excel 没有有效图片记录。')
    return entries


def schedule_offsets(count, delay_ms, frequency_text):
    """Use absolute deadlines; each frequency describes the next image interval."""
    try:
        rates = [float(s) for s in re.split(r'[,，;；\s]+', frequency_text.strip()) if s]
    except ValueError as exc:
        raise ValueError('全息图频率必须为数字，以逗号分隔。') from exc
    if not rates or any(not math.isfinite(hz) or hz <= 0 for hz in rates):
        raise ValueError('全息图频率必须为大于 0 的有限数。')
    if len(rates) not in (1, max(1, count - 1)):
        raise ValueError(f'{count} 张图片需要 1 个统一频率，或 {count - 1} 个逐次切换频率。')
    if not math.isfinite(delay_ms) or delay_ms < 0:
        raise ValueError('全息图启动延迟必须为非负有限数。')
    offsets = [delay_ms / 1000]
    for i in range(count - 1):
        offsets.append(offsets[-1] + 1 / rates[0 if len(rates) == 1 else i])
    return offsets, rates


def column_entry_indices(entries, count):
    """Shared image/video column grouping; preserve sheet order and duplicates."""
    groups = {}
    for index, entry in enumerate(entries):
        try:
            number = float(entry.n)
            if isinstance(entry.n, bool) or not math.isfinite(number) or number < 1 or not number.is_integer():
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError(f'全息图 Excel 第 {entry.excel_row} 行：列号n(Y向)必须为正整数。')
        groups.setdefault(int(number), []).append(index)
    for column in range(1, count + 1):
        if column not in groups:
            raise ValueError(f'第 {column} 条扫描线缺少列号 n={column} 的相位图。')
    return {column: groups[column] for column in range(1, count + 1)}


def column_sequences(entries, count, delay_ms, frequency_text):
    """Scan line k uses n=k, keeping Excel order within each column."""
    sequences = {}
    all_rates = []
    for column, indices in column_entry_indices(entries, count).items():
        try:
            offsets, rates = schedule_offsets(len(indices), delay_ms, frequency_text)
        except ValueError as exc:
            raise ValueError(f'列号 n={column}：{exc}') from exc
        sequences[column] = (indices, offsets)
        all_rates.extend(rates)
    return sequences, all_rates


class ShutterTimeline:
    """Thread-safe shutter events and per-opening scan context."""
    def __init__(self):
        self._lock = threading.Lock()
        self._state = (0, False, 0.0)
        self._events = []
        self._contexts = {}

    def mark(self, opened, context=None):
        with self._lock:
            self._state = (self._state[0] + 1, opened, time.monotonic())
            self._events.append(self._state)
            if opened:
                self._contexts[self._state[0]] = dict(context or {})

    def context(self, generation):
        with self._lock:
            return dict(self._contexts.get(generation, {}))

    def history(self):
        with self._lock:
            return list(self._events)

    def snapshot(self):
        with self._lock:
            return self._state


class HologramSettings(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox('启用全息图片切换（随扫描快门）')
        box.addWidget(self.enabled)
        self.video_enabled = QCheckBox('启用全息视频切换（匀速扫描随快门）')
        box.addWidget(self.video_enabled)
        video_note = QLabel('视频复用下方Excel和文件夹，在“相位图名称”填写视频文件名。\n'
                            '第k条使用n=k，同列按表格顺序播放；开门播放，关门关闭并准备下一列。\n'
                            '视频按自身帧率播放，不使用图片Hz及启动延迟。图片与视频不能同时启用。')
        video_note.setWordWrap(True)
        box.addWidget(video_note)
        box.addWidget(QLabel('全息图 Excel（图片/视频共用）'))
        excel_row = QHBoxLayout()
        self.excel = QLineEdit()
        self.excel.setPlaceholderText('相位图名称、行号m(X向)、列号n(Y向)')
        browse_excel = QPushButton('选择')
        browse_excel.setFixedWidth(66)
        browse_excel.clicked.connect(self.choose_excel)
        excel_row.addWidget(self.excel)
        excel_row.addWidget(browse_excel)
        box.addLayout(excel_row)
        box.addWidget(QLabel('相位图所在文件夹（留空使用 Excel 所在目录）'))
        row = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText('用于查找 Excel 中填写的图片或视频文件名')
        choose = QPushButton('选择')
        choose.setFixedWidth(66)
        choose.clicked.connect(self.choose_folder)
        row.addWidget(self.folder)
        row.addWidget(choose)
        box.addLayout(row)
        box.addWidget(QLabel('开快门后启动延迟 (ms)'))
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 3600000)
        self.delay.setDecimals(3)
        box.addWidget(self.delay)
        self.frequency_settings = QWidget()
        frequency_box = QVBoxLayout(self.frequency_settings)
        frequency_box.setContentsMargins(0, 0, 0, 0)
        frequency_box.addWidget(QLabel('全息图切换频率 (Hz)'))
        self.frequencies = QLineEdit('10')
        self.frequencies.setPlaceholderText('例如 10，20，30')
        frequency_box.addWidget(self.frequencies)
        frequency_note = QLabel('多个值用逗号分隔；依次对应每列内相邻图片间隔。\n一个值用于全部间隔，例如 10 Hz = 100 ms。')
        frequency_note.setWordWrap(True)
        frequency_box.addWidget(frequency_note)
        box.addWidget(self.frequency_settings)
        note = QLabel('匀速扫描第1条使用n=1，第2条使用n=2，依次类推。\n'
                      '每列内按Excel顺序播放，空行跳过，名称可重复；m仅供查看。\n'
                      '原图按原始像素居中显示，超出屏幕部分裁切，不缩放。\n'
                      '切换频率在“轴控制 → 匀速扫描设置”中填写。\n'
                      '图片匀速扫描最多预加载5列；打印完释放该列，再补载新列。\n'
                      '第一张在启动延迟后显示，最后一张保持显示，不循环。\n'
                      '显示器沿用扫描设置。以开门指令响应计时；小于 16 ms 的实际画面误差需硬件实测。')
        note.setWordWrap(True)
        box.addWidget(note)
        self.enabled.toggled.connect(self.image_toggled)
        self.video_enabled.toggled.connect(self.video_toggled)
        self.folder.textChanged.connect(self.changed)
        self.excel.textChanged.connect(self.changed)
        self.delay.valueChanged.connect(self.changed)
        self.frequencies.textChanged.connect(self.changed)

    def image_toggled(self, checked):
        if checked:
            self.video_enabled.setChecked(False)
        self.changed.emit()

    def video_toggled(self, checked):
        if checked:
            self.enabled.setChecked(False)
        self.delay.setEnabled(not checked)
        self.frequency_settings.setEnabled(not checked)
        self.changed.emit()

    def choose_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择全息图 Excel', self.excel.text(), 'Excel (*.xlsx *.xlsm)')
        if path:
            self.excel.setText(path)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择全息图文件夹', self.folder.text())
        if folder:
            self.folder.setText(folder)

    def config_values(self):
        return {'hologram_enabled': self.enabled.isChecked(),
                'hologram_video_enabled': self.video_enabled.isChecked(),
                'hologram_folder': self.folder.text().strip(),
                'hologram_excel': self.excel.text().strip(),
                'hologram_delay_ms': self.delay.value(),
                'hologram_frequencies': self.frequencies.text().strip()}

    def apply_config(self, config):
        self.enabled.setChecked(bool(config.get('hologram_enabled', False)))
        self.video_enabled.setChecked(bool(config.get('hologram_video_enabled', False)))
        self.folder.setText(str(config.get('hologram_folder', '')))
        self.excel.setText(str(config.get('hologram_excel', '')))
        self.delay.setValue(float(config.get('hologram_delay_ms', 0)))
        self.frequencies.setText(str(config.get('hologram_frequencies', '10')))


class ImageCacheSession:
    """GUI loads/refills between lines; the motion worker waits without blocking UI."""
    def __init__(self):
        self.ready = threading.Event()
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self.column = None
        self.error = None

    def request(self, column):
        with self._lock:
            self.column = column
            self.ready.clear()

    def mark_ready(self, column):
        with self._lock:
            if self.column == column and not self.cancelled.is_set():
                self.ready.set()

    def fail(self, message):
        with self._lock:
            self.error = str(message)
            self.ready.set()

    def raise_if_failed(self):
        with self._lock:
            error = self.error
        if error:
            raise RuntimeError('全息图缓存：' + error)

    def cancel(self):
        self.cancelled.set()
        self.ready.set()

    def wait_ready(self, stopped, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stopped() or self.cancelled.is_set():
                return False
            self.raise_if_failed()
            if self.ready.wait(0.01):
                self.raise_if_failed()
                return not (stopped() or self.cancelled.is_set())
        raise TimeoutError('全息图缓存补载超时，扫描已停止。')


class HologramPlayer(QWidget):
    message = pyqtSignal(str)
    dismissed = pyqtSignal()

    def __init__(self, paths, offsets, screen, timeline, sequences=None):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setGeometry(screen.geometry())
        self.paths = [Path(path).resolve() for path in paths]
        self.offsets = offsets
        self.sequences = sequences
        self.timeline = timeline
        self.cache_columns = 5
        self.cache_session = ImageCacheSession() if sequences is not None else None
        self.frames = [None] * len(paths)
        self.current_frame = None
        self.resident_columns = set()
        self._pixmaps = {}
        QImageReader.setAllocationLimit(0)
        try:
            if sequences is None:
                self._load_indices(range(len(paths)))
            else:
                self._load_columns(sorted(sequences)[:self.cache_columns])
        except Exception:
            self.release_cache()
            self.deleteLater()
            raise
        self.index = -1
        self.generation = 0
        self.started = 0.0
        self.running = False
        self.samples = []
        self.records = []
        self.clock_origin = time.monotonic()
        self.wall_origin = time.time()
        self.position_provider = lambda: ({}, {})
        self.report_path = None
        self._finished = False
        self.missed = 0
        self._armed = False
        self._has_started = False
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(1)
        self.timer.timeout.connect(self.tick)

    def _load_indices(self, indices):
        for index in indices:
            if self.cache_session is not None and self.cache_session.cancelled.is_set():
                raise RuntimeError('图片缓存加载已停止')
            path = self.paths[index]
            if path not in self._pixmaps:
                reader = QImageReader(str(path))
                image = reader.read()
                if image.isNull():
                    raise ValueError(f'全息图解码失败：{path}：{reader.errorString()}')
                self._pixmaps[path] = QPixmap.fromImage(image)
            self.frames[index] = self._pixmaps[path]

    def _load_columns(self, columns):
        for column in columns:
            if column not in self.resident_columns:
                self._load_indices(self.sequences[column][0])
                self.resident_columns.add(column)

    def release_cache(self):
        # Frame metadata and playback records contain no decoded image buffers.
        self.frames[:] = [None] * len(self.frames)
        self._pixmaps.clear()
        self.resident_columns.clear()

    def complete_column(self, column):
        """Release finished columns before refilling: never cache six columns."""
        session = self.cache_session
        if session is None or self._finished or session.cancelled.is_set():
            return
        try:
            self.running = False
            for old in tuple(self.resident_columns):
                if old <= column:
                    for index in self.sequences[old][0]:
                        self.frames[index] = None
                    self.resident_columns.remove(old)
            upcoming = [c for c in sorted(self.sequences) if c > column][:self.cache_columns]
            needed = {self.paths[index] for resident in upcoming
                      for index in self.sequences[resident][0]}
            for path in tuple(self._pixmaps):
                if path not in needed:
                    del self._pixmaps[path]
            self.message.emit(f'全息图第{column}列打印完成，已释放该列缓存。')
            self._load_columns(upcoming)
            names = '、'.join(str(c) for c in sorted(self.resident_columns)) or '无（仅保留当前显示帧）'
            self.message.emit(f'全息图缓存就绪：列 {names}，共{len(self._pixmaps)}张不同图片。')
            session.mark_ready(column)
        except Exception as exc:
            session.fail(exc)
            self.message.emit('全息图缓存补载失败：' + str(exc))

    def arm(self):
        self._has_started = True
        self._armed = True
        self.showFullScreen()
        self.repaint()
        self.timer.start()

    def sequence_for(self, generation):
        if self.sequences is None:
            return list(range(len(self.frames))), self.offsets
        return self.sequences[self.timeline.context(generation)['column']]

    def tick(self):
        generation, opened, stamp = self.timeline.snapshot()
        if generation != self.generation:
            if generation > self.generation + 1:
                self.missed += generation - self.generation - 1
            self.generation = generation
            self.running = opened
            if opened:
                self.started = stamp
                self.next_index = 0
                self.active_indices, self.active_offsets = self.sequence_for(generation)
        if not self.running:
            return
        now = time.monotonic()
        if now < self.started + self.active_offsets[self.next_index]:
            return
        # Never skip images; absolute deadlines avoid accumulated interval drift.
        self.index = self.active_indices[self.next_index]
        self.current_frame = self.frames[self.index]
        if self.current_frame is None:
            self.running = False
            if self.cache_session is not None:
                self.cache_session.fail(f'第{self.index + 1}条图片记录未就绪，停止曝光。')
            return
        deadline = self.started + self.active_offsets[self.next_index]
        context, feedback = self.position_provider()
        context = {**context, **self.timeline.context(generation)}
        self.repaint()
        stamp = time.monotonic()
        late_ms = (stamp - deadline) * 1000
        self.samples.append((self.index, late_ms))
        self.records.append({'generation': self.generation, 'index': self.index,
                             'stamp': stamp, 'late_ms': late_ms,
                             'context': context, 'feedback': feedback})
        self.next_index += 1
        if self.next_index == len(self.active_indices):
            self.running = False

    def finish(self):
        if self._finished:
            return
        self._finished = True
        self.timer.stop()
        self.running = False
        if self.cache_session is not None:
            self.cache_session.cancel()
        self.release_cache()
        if self.samples:
            worst = max(ms for _, ms in self.samples)
            failures = sum(ms >= 16 for _, ms in self.samples)
            self.message.emit(f'全息图软件绘制记录：{len(self.samples)} 次，最大迟到 {worst:.3f} ms，'
                              f'≥16 ms 共 {failures} 次；不含显示器刷新及快门机械延迟。')
        if self.missed:
            self.message.emit(f'全息图警告：界面未及时处理 {self.missed} 次快门状态变化，时序未达标。')
        # Preserve the last displayed pixmap after normal completion or shutter close.

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.current_frame is not None:
            pixmap = self.current_frame
            dpr = self.devicePixelRatioF()
            painter.scale(1 / dpr, 1 / dpr)
            painter.drawPixmap(round((self.width() * dpr - pixmap.width()) / 2),
                               round((self.height() * dpr - pixmap.height()) / 2), pixmap)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        self.running = False
        if self.cache_session is not None:
            self.cache_session.cancel()
        self.release_cache()
        self.current_frame = None
        armed = self._armed
        self._armed = False
        if armed:
            self.dismissed.emit()
        event.accept()
