"""Offline playback and frame-by-frame inspection of continuous limit checks."""
from __future__ import annotations

import math
from zipfile import BadZipFile

from PySide6.QtCore import QElapsedTimer, QIODevice, QSaveFile, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHeaderView,
                              QHBoxLayout, QLabel, QMessageBox, QSlider, QTableWidget,
                              QTableWidgetItem, QVBoxLayout, QWidget)

from factory_hmi.core.continuous_limit_replay import PHASES, SUPPORTED, load_limit_replay
from .limit_block_page import add_button


class ReplayCanvas(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.pixmap = QPixmap()
        self.setMinimumHeight(230)

    def paintEvent(self, event):
        if not self.pixmap.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            picture = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width()-picture.width())//2, (self.height()-picture.height())//2, picture)
            painter.end()


class ContinuousLimitPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tabPage")
        self.replay = None
        self.shown_picture = None
        self.loaded_limb = None
        self.second = self.anchor = 0.
        self.result_marker = None
        self.elapsed = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        heading = QLabel("连续限位检查")
        heading.setObjectName("pageHeading")
        layout.addWidget(heading)
        self.intro = QLabel("本地模拟回放。逐项显示模拟止挡角和量产参考角。")
        self.intro.setObjectName("pageDescription")
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("模块"))
        self.limb = QComboBox()
        for key, label in SUPPORTED:
            self.limb.addItem("腰髋 · 腰部两轴" if key == "waist_hip" else label, key)
        row.addWidget(self.limb, 1)
        self.play = add_button(row, "播放模拟", self._toggle, primary=True)
        self.restart = add_button(row, "回到起点", self._restart)
        self.open_video = add_button(row, "打开视频", self._open)
        self.export_npz = add_button(row, "导出 NPZ", self._export)
        layout.addLayout(row)

        self.canvas = ReplayCanvas(self)
        self.placeholder = QLabel("播放模拟后显示动作。")
        self.placeholder.setWordWrap(True)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.placeholder)
        layout.addWidget(self.canvas, 1)
        self.canvas.hide()

        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setAccessibleName("轨迹时间")
        layout.addWidget(self.timeline)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["关节 / 方向", "量产参考 (°)", "模拟止挡 (°)", "差值 (°)", "回放状态"])
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(155)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        self.limb.currentIndexChanged.connect(self._select)
        self.timeline.valueChanged.connect(self._seek)
        self.table.cellDoubleClicked.connect(self._jump)
        self._controls()

    def showEvent(self, event):
        super().showEvent(event)
        if self.loaded_limb != self.limb.currentData():
            self._select()

    def hideEvent(self, event):
        self.shutdown()
        super().hideEvent(event)

    def shutdown(self):
        self.timer.stop()
        self.play.setText("播放模拟")

    def _select(self):
        self.shutdown()
        self.canvas.hide()
        self.placeholder.show()
        self.loaded_limb = self.limb.currentData()
        self.shown_picture = None
        if self.replay is not None:
            self.replay.pictures.close()
        self.replay = None
        self.second = 0.
        self.result_marker = None
        self.table.setRowCount(0)
        try:
            self.replay = load_limit_replay(self.loaded_limb)
        except (OSError, ValueError, KeyError, TypeError, BadZipFile) as exc:
            self.placeholder.setText("当前模块的本地轨迹无法载入。")
            self.status.setText(str(exc))
            self._controls()
            return
        if self.loaded_limb == "waist_hip":
            self.intro.setText("腰侧摆和腰旋转各检查上下限。髋轴固定。导出的是腰部两轴模拟 NPZ。")
        else:
            hold = "髋部大幅侧摆时保持屈膝。" if "leg" in self.loaded_limb else "肩部摆动时保持屈肘。"
            self.intro.setText(f"{hold} 本地模拟回放逐项显示止挡角与量产参考角。")
        self.placeholder.setText("播放模拟后显示动作。")
        with QSignalBlocker(self.timeline):
            self.timeline.setRange(0, round(self.replay.duration * 1000))
            self.timeline.setValue(0)
        for i, e in enumerate(self.replay.events):
            self.table.insertRow(i)
            for col, value in enumerate((e.label, f"{e.reference_deg:+.2f}", "—", "—", "等待回放")):
                self.table.setItem(i, col, QTableWidgetItem(value))
        self._controls()
        self._refresh()

    def _controls(self):
        for widget in (self.play, self.restart, self.open_video, self.export_npz, self.timeline):
            widget.setEnabled(self.replay is not None)

    def _toggle(self):
        if self.timer.isActive():
            self.shutdown()
            return
        if self.second >= self.replay.duration:
            self._set_second(0.)
        self.anchor = self.second
        self.elapsed.start()
        self.timer.start()
        self.play.setText("暂停")

    def _tick(self):
        second = self.anchor + self.elapsed.elapsed() / 1000 * self.replay.speed
        self._set_second(second)
        if second >= self.replay.duration:
            self.shutdown()

    def _set_second(self, t):
        self.second = min(self.replay.duration, max(0., t))
        with QSignalBlocker(self.timeline):
            self.timeline.setValue(round(self.second * 1000))
        self._refresh()

    def _seek(self, milliseconds):
        self._set_second(milliseconds / 1000)
        if self.timer.isActive():
            self.anchor = self.second
            self.elapsed.restart()

    def _jump(self, row, _):
        self._seek(round(float(self.replay.time[self.replay.events[row].start]) * 1000))

    def _restart(self):
        self.shutdown()
        self._seek(0)

    def _refresh(self):
        replay = self.replay
        frame = replay.frame_at(self.second)
        active = next((i for i, e in enumerate(replay.events) if e.start <= frame <= e.end), None)
        count = sum(frame >= e.contact for e in replay.events)
        phase = PHASES[str(replay.phases[frame])]
        detail = f"{self.second:.1f} / {replay.duration:.1f} s · {phase} · 已回放 {count}/{len(replay.events)} 项"
        if active is not None:
            e = replay.events[active]
            angle = math.degrees(replay.position[frame, e.axis])
            detail += f" · {e.label} · {angle:+.2f}°"
        self.status.setText(detail)
        picture = replay.picture_at(self.second)
        if picture != self.shown_picture:
            self.shown_picture = picture
            jpeg = replay.pictures.read(f"{picture:06d}.jpg")
            if self.canvas.pixmap.loadFromData(jpeg, "JPG"):
                self.placeholder.hide()
                self.canvas.show()
                self.canvas.update()
            else:
                self.canvas.hide()
                self.placeholder.setText("无法显示动作预览，可用系统播放器打开 MP4。")
                self.placeholder.show()
        marker = (active, count)
        if marker == self.result_marker:
            return
        self.result_marker = marker
        for i, e in enumerate(replay.events):
            reached = i < count
            self.table.item(i, 2).setText(f"{e.simulated_deg:+.2f}" if reached else "—")
            self.table.item(i, 3).setText(f"{e.simulated_deg-e.reference_deg:+.2f}" if reached else "—")
            result = ("模型边界" if e.model_boundary else "模拟检出") if reached else "回放中" if i == active else "等待回放"
            self.table.item(i, 4).setText(result)
        if active is not None:
            self.table.selectRow(active)
        else:
            self.table.clearSelection()

    def _open(self):
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.replay.video))):
            QMessageBox.warning(self, "视频无法打开", str(self.replay.video))

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存当前模块 NPZ", self.replay.npz_name, "NPZ (*.npz)")
        if not path:
            return
        file = QSaveFile(path)
        if (not file.open(QIODevice.OpenModeFlag.WriteOnly)
                or file.write(self.replay.npz_bytes) != len(self.replay.npz_bytes) or not file.commit()):
            QMessageBox.warning(self, "NPZ 保存失败", file.errorString())
