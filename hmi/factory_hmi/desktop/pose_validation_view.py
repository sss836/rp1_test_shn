"""Presentation for feedback-driven pose checks; never emits motor commands."""
from functools import lru_cache
import json
import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from factory_hmi.core.limit_block_check import COMMON, SIDE, MODULES, NAMES
from factory_hmi.limit_replay_assets import RESOURCE_ROOT, read_manifest, verified_asset
from scripts.rp1_limb_common import limb_joint_names


def joint_label(name):
    return NAMES.get(name.removesuffix("_joint").removeprefix("left_").removeprefix("right_"), name)


@lru_cache(maxsize=16)
def reference_rows(limb):
    if limb not in dict(MODULES):
        return ()
    names = limb_joint_names(limb)
    if limb == "waist_hip":
        names = names[:2]
    rows = {}
    for name in names:
        key = name.removesuffix("_joint").removeprefix("left_").removeprefix("right_")
        bounds = SIDE.get(name.split("_")[0], {}).get(key, COMMON.get(key))
        if bounds is None:
            continue
        for sign, angle in zip((-1, 1), bounds):
            rows[(name, sign)] = dict(joint=name, sign=sign, reference_deg=angle)
    try:
        asset = read_manifest(RESOURCE_ROOT)[limb]["route"]
        _, raw = verified_asset(RESOURCE_ROOT, asset)
        order = [(row["joint"], row["direction"]) for row in json.loads(raw)["windows"]]
        if set(order) == set(rows) and len(order) == len(rows):
            return tuple(rows[key] for key in order)
    except (KeyError, ValueError, OSError):
        pass  # Display references even if an asset is unavailable; start still validates it.
    return tuple(rows.values())


def label(text="", name=""):
    item = QLabel(text)
    item.setObjectName(name)
    item.setTextFormat(Qt.TextFormat.PlainText)
    return item


def card(layout_type=QHBoxLayout):
    frame = QFrame()
    frame.setObjectName("poseCard")
    layout = layout_type(frame)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(14)
    return frame, layout


def build_pose_layout(panel):
    panel.setObjectName("poseValidation")
    panel.setStyleSheet("""
        QWidget#poseValidation { background: #f4f7fb; color: #17324b; }
        #poseValidation QLabel { background: transparent; border: none; }
        #poseValidation QFrame#poseCard { background: white; border: 1px solid #d9e2ef; border-radius: 10px; }
        #poseValidation QLabel#poseEyebrow { color: #687c95; font-size: 11px; font-weight: 600; }
        #poseValidation QLabel#poseTitle { font-size: 28px; font-weight: 700; color: #152e47; }
        #poseValidation QLabel#poseMuted { color: #63768c; font-size: 12px; }
        #poseValidation QLabel#poseValue { color: #15324e; font-size: 17px; font-weight: 600; }
        #poseValidation QLabel#poseSection { font-size: 16px; font-weight: 600; }
        #poseValidation QLabel#poseBadge { background: #edf4ff; color: #245eb2; border-radius: 7px; padding: 8px 12px; }
        #poseValidation QLabel#poseFault { background: #fff1ef; color: #ac302a; border: 1px solid #f1c9c5; border-radius: 7px; padding: 10px 12px; }
        #poseValidation QLabel#poseStep { border-radius: 8px; padding: 10px 8px; font-size: 13px; }
        #poseValidation QPushButton { min-height: 36px; padding: 0 16px; border: 1px solid #bdcde1; border-radius: 7px; background: white; color: #1d4268; font-weight: 600; }
        #poseValidation QPushButton#poseStart { background: #2169db; color: white; border-color: #2169db; }
        #poseValidation QPushButton#poseAbort { border-color: #dc7777; color: #bd3535; }
        #poseValidation QPushButton:disabled, #poseValidation QPushButton#poseStart:disabled, #poseValidation QPushButton#poseAbort:disabled { background: #edf1f6; color: #8b9bad; border-color: #dbe3ed; }
        #poseValidation QProgressBar { border: none; background: #dfe7f2; border-radius: 4px; min-height: 8px; max-height: 8px; }
        #poseValidation QProgressBar::chunk { background: #2877e8; border-radius: 4px; }
        #poseValidation QTableWidget { background: white; alternate-background-color: #f7f9fd; border: none; gridline-color: #e4eaf2; selection-background-color: #e9f2ff; selection-color: #193c61; }
        #poseValidation QHeaderView::section { background: #edf2f8; color: #294560; border: none; border-bottom: 1px solid #dfe6ef; padding: 10px 6px; font-weight: 600; }
    """)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(18, 14, 18, 16)
    layout.setSpacing(13)
    layout.addWidget(label("MOTION VALIDATION", "poseEyebrow"))
    heading = QHBoxLayout()
    heading.addWidget(label("姿态验证", "poseTitle"))
    heading.addStretch()
    heading.addWidget(label("本机检查 · 实测反馈", "poseBadge"))
    layout.addLayout(heading)
    layout.addWidget(label("自动回零  ·  按顺序搜索  ·  实测记录限位", "poseMuted"))

    identity, row = card()
    panel.module_value, panel.serial_value, panel.link_value = (label("—", "poseValue") for _ in range(3))
    for title, value, stretch in (("当前模块", panel.module_value, 2), ("样机编号", panel.serial_value, 1),
                                  ("控制状态", panel.link_value, 2)):
        column = QVBoxLayout(); column.setSpacing(5)
        column.addWidget(label(title, "poseMuted")); column.addWidget(value)
        row.addLayout(column, stretch)
    tip = label("先连接、扫描并使能，再开始验证。\n自动移动到逻辑零点，不修改电机零偏。", "poseMuted")
    tip.setWordWrap(True); row.addWidget(tip, 3)
    layout.addWidget(identity)

    actions, row = card()
    for attr, title, name, action in (("start", "开始姿态验证", "poseStart", panel._start),
                                    ("abort", "中止并失能", "poseAbort", panel._abort),
                                    ("export", "导出报告", "poseExport", panel._export)):
        button = QPushButton(title); button.setObjectName(name); button.clicked.connect(action)
        setattr(panel, attr, button); row.addWidget(button)
    row.addStretch()
    panel.progress_text = label("等待开始", "poseValue"); row.addWidget(panel.progress_text)
    layout.addWidget(actions)
    panel.fault_banner = label("", "poseFault")
    panel.fault_banner.setWordWrap(True)
    panel.fault_banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    panel.fault_banner.hide(); layout.addWidget(panel.fault_banner)
    steps = QHBoxLayout()
    panel.steps = []
    for text in ("1   自动回零", "2   限位搜索", "3   回零收尾", "4   保存报告"):
        step = label(text, "poseStep"); step.setAlignment(Qt.AlignmentFlag.AlignCenter)
        steps.addWidget(step, 1); panel.steps.append(step)
    layout.addLayout(steps)
    panel.progress = QProgressBar(); panel.progress.setRange(0, 100); panel.progress.setValue(0)
    panel.progress.setTextVisible(False); layout.addWidget(panel.progress)

    summaries = QHBoxLayout()
    current, col = card(QVBoxLayout)
    col.setSpacing(8)
    col.addWidget(label("当前检查", "poseSection"))
    panel.current_value = label("等待开始", "poseValue")
    panel.measurement = label("实测角度 —    ·    力矩 —", "poseMuted")
    col.addWidget(panel.current_value); col.addWidget(panel.measurement)
    col.addWidget(label("角度持续停滞且力矩上升，记录实测止挡角。", "poseMuted"))
    summaries.addWidget(current, 3)
    summary, col = card(QVBoxLayout)
    col.setSpacing(8)
    col.addWidget(label("检查结果", "poseSection"))
    panel.counts = label("0 已记录    0 异常    0 待检查", "poseValue")
    col.addWidget(panel.counts)
    col.addWidget(label("量产参考角用于比较，不替代实测结果。", "poseMuted"))
    summaries.addWidget(summary, 2)
    layout.addLayout(summaries)

    results_card, col = card(QVBoxLayout)
    title = QHBoxLayout(); title.addWidget(label("限位检查记录", "poseSection")); title.addStretch()
    title.addWidget(label("角度单位 °  ·  力矩单位 N·m", "poseMuted")); col.addLayout(title)
    panel.results = QTableWidget(0, 6)
    panel.results.setHorizontalHeaderLabels(["关节 / 方向", "参考角", "实测角", "偏差", "阻挡力矩", "结果"])
    panel.results.setAlternatingRowColors(True)
    panel.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    panel.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    panel.results.verticalHeader().hide(); panel.results.verticalHeader().setDefaultSectionSize(37)
    panel.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    panel.results.horizontalHeader().setFixedHeight(40)
    panel.results.setMinimumHeight(265)
    col.addWidget(panel.results, 1); layout.addWidget(results_card, 1)
    panel.state = label("", "poseMuted"); panel.state.setWordWrap(True); layout.addWidget(panel.state)
    panel.record_path = label("结果自动保存在本机。尚未检查的项目不显示实测值。", "poseMuted")
    panel.record_path.setWordWrap(True); panel.record_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(panel.record_path)
    panel.digest = label(); panel.digest.hide()  # Full identity stays available in tooltips/reports.
    panel._table_stamp = None


def number(value, *, signed=True):
    if value is None or not math.isfinite(value):
        return "—"
    return format(value, "+.2f" if signed else ".2f")


def render_pose_status(panel, s, *, online, pending, serial):
    active = bool(s.get("active")); phase = s.get("phase", "idle")
    rows = s.get("checks") or reference_rows(s.get("limb"))
    results = s.get("results", [])
    total = len(rows); done = len(results)
    current = s.get("current_check") or {}
    candidate = s.get("current_candidate")
    panel.module_value.setText(dict(MODULES).get(s.get("limb"), "未应用模块"))
    panel.module_value.setToolTip(panel.digest.text())
    panel.serial_value.setText(serial or "未填写")
    link = ("状态连接待确认" if not online else "未连接电机" if not s.get("discovered") else
            "已连接 · 已使能" if s.get("motors_enabled") else "已连接 · 未使能")
    panel.link_value.setText(link)
    fault = s.get("fault") if phase == "aborted" else None
    panel.fault_banner.setText("检查中止：" + fault if fault else "")
    panel.fault_banner.setVisible(bool(fault))
    panel.progress_text.setText("正在启动…" if pending else f"检查中 · {min(done+1,total)} / {total} 项" if active
                               else "已中止" if phase == "aborted" else "检查完成" if phase == "finished" else "等待开始")
    transfer = s.get("last_transfer") or {}
    if active and phase == "homing":
        panel.progress_text.setText(f"自动回零 · {round(100*transfer.get('command_progress', 0))}%")
    panel.progress.setValue(round(done * 100 / total) if total else 0)
    stage = (3 if phase == "finished" else 2 if phase == "returning" or done == total > 0 else
             0 if phase in {"idle", "waiting_feedback", "gain_transition", "homing"} and not done else 1)
    for index, step in enumerate(panel.steps):
        selected = index == stage and (active or phase == "finished")
        step.setStyleSheet("background: #e4efff; color: #155ccc; font-weight: 600;" if selected else
                           "background: transparent; color: #61748d;")
    if current:
        panel.current_value.setText(f"{joint_label(current['joint'])} · {'上限' if current['sign'] > 0 else '下限'}")
        kind = "估算力矩" if current.get("torque_is_estimated") else "力矩"
        panel.measurement.setText(f"实测角度 {number(current.get('actual_deg'))}°    ·    {kind} {number(current.get('torque_nm'), signed=False)} N·m")
    else:
        panel.current_value.setText("从当前姿态平滑回零" if phase == "homing" else
                                    "返回零点" if phase == "returning" else "回零或转场中止" if phase == "aborted" and transfer else
                                    "等待开始" if not done else "检查已结束" if not active else "转场中")
        panel.measurement.setText("实测角度 —    ·    力矩 —")
        if transfer and (active or phase == "aborted"):
            actual, goal = transfer.get("actual_deg", []), transfer.get("goal_deg", [])
            if actual and len(actual) == len(goal):
                error = max(abs(a-b) for a,b in zip(actual, goal))
                panel.measurement.setText(f"最大到位偏差 {error:.2f}°    ·    到位范围 ±{transfer['arrival_tolerance_deg']:g}°")
    errors = sum(row.get("outcome") != "within_reference" for row in results)
    panel.counts.setText(f"{done} 已记录    {errors} 异常    {max(0,total-done-(1 if active and current else 0))} 待检查")
    if s.get("record_error"):
        panel.record_path.setText("自动保存失败：" + s["record_error"])
    elif s.get("report_file"):
        panel.record_path.setText("本机报告：" + s["report_file"])
    else:
        panel.record_path.setText("结果自动保存在本机。尚未检查的项目不显示实测值。")
    stamp = json.dumps((rows, results, candidate, (current.get("joint"), current.get("sign")), phase, active))
    if panel._table_stamp == stamp:
        return
    # Snapshot primitives only; polling must not overwrite the comparison stamp in place.
    panel._table_stamp = stamp
    measured = {(r["joint"], r["sign"]): r for r in results}
    panel.results.setRowCount(total)
    outcomes = {"within_reference": "参考范围内", "early": "提前阻挡", "late": "超出参考", "not_detected": "未检出"}
    for index, ref in enumerate(rows):
        key = (ref["joint"], ref["sign"]); result = measured.get(key)
        is_current = key == (current.get("joint"), current.get("sign"))
        if not result and candidate and key == (candidate["joint"], candidate["sign"]):
            result = candidate
        outcome = outcomes.get(result.get("outcome"), "候选已记录 · 待退让") if result else (
            "搜索中" if is_current and active and phase == "search" else "处理中" if is_current and active else
            "未检查" if phase == "aborted" else "待检查")
        tone = "#157452" if result and result.get("outcome") == "within_reference" else (
            "#b45309" if result else "#2367c4" if is_current and active else "#7a8799")
        result = result or {}
        load = number(result.get("torque_floor_nm"), signed=False)
        if load != "—" and result.get("torque_is_estimated"):
            load += "（估算）"
        values = [f"{joint_label(ref['joint'])} / {'上限' if ref['sign'] > 0 else '下限'}",
                  number(ref["reference_deg"]), number(result.get("actual_deg")),
                  number(result.get("deviation_deg")), load, outcome]
        for column, value in enumerate(values):
            item = panel.results.item(index, column)
            if item is None:
                item = QTableWidgetItem(); panel.results.setItem(index, column, item)
            item.setText(value)
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if column == 0 else Qt.AlignmentFlag.AlignCenter))
            item.setForeground(QColor(tone if column == 5 else "#203e5d"))
            item.setBackground(QColor("#eaf7f0" if result.get("outcome") == "within_reference" else
                                      "#fff3df" if result else "#e9f2ff" if is_current and active else "#f7f9fd")
                               if column == 5 else QColor("#ffffff" if index % 2 == 0 else "#f7f9fd"))
            item.setToolTip(ref["joint"] if column == 0 else "Iq × 实测 Kt 换算的估算力矩" if column == 4 and result.get("torque_is_estimated") else "")
