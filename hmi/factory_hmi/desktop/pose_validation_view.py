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
    panel.motion_rates = label("", "poseMuted")
    panel.motion_rates.hide()
    layout.addWidget(panel.motion_rates)

    identity, row = card()
    panel.module_value, panel.serial_value, panel.link_value = (label("—", "poseValue") for _ in range(3))
    for title, value, stretch in (("当前模块", panel.module_value, 2), ("样机编号", panel.serial_value, 1),
                                  ("控制状态", panel.link_value, 2)):
        column = QVBoxLayout(); column.setSpacing(5)
        column.addWidget(label(title, "poseMuted")); column.addWidget(value)
        row.addLayout(column, stretch)
    tip = label("先连接、扫描并使能，再开始验证。\n自动发送零位目标并等待实测停稳，不修改电机零偏。", "poseMuted")
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
    panel.contact_detail = label("", "poseMuted")
    panel.contact_detail.setWordWrap(True)
    col.addWidget(panel.contact_detail)
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


def search_attempt_tooltip(attempts):
    reasons = {"actual_travel": "实测到达行程边界", "command_travel": "指令到达行程边界",
               "feedback_stall": "关节停滞，增载证据不足",
               "contact": "检出止挡"}
    return "\n".join(
        f"第 {row['attempt']} 次 · {reasons.get(row.get('end_reason'), '搜索结束')}\n"
        f"搜索终点 {number(row.get('search_end_actual_deg'))}°  ·  "
        f"目标 {number(row.get('search_end_target_deg'))}°\n"
        f"终点力矩 {number(row.get('search_end_torque_nm'), signed=False)} N·m  ·  "
        f"判定门槛 {number(row.get('threshold_nm'), signed=False)} N·m"
        for row in attempts)


def render_pose_status(panel, s, *, online, pending, serial):
    active = bool(s.get("active")); phase = s.get("phase", "idle")
    rows = s.get("checks") or reference_rows(s.get("limb"))
    results = s.get("results", [])
    total = len(rows); done = len(results)
    current = s.get("current_check") or {}
    candidate = s.get("current_candidate")
    attempt, attempt_limit = current.get("search_attempt"), current.get("search_attempt_limit")
    retrying = attempt is not None and attempt > 1
    extending = bool(current.get("search_extension_active"))
    panel.module_value.setText(dict(MODULES).get(s.get("limb"), "未应用模块"))
    panel.module_value.setToolTip(panel.digest.text())
    panel.serial_value.setText(serial or "未填写")
    rates = s.get("motion_rates") or {}
    if rates:
        panel.motion_rates.setText(
            f"目标速度：搜索 {rates['search_deg_s']:g}°/s  ·  近限位 {rates['near_limit_deg_s']:g}°/s"
            f"  ·  回程 {rates['return_deg_s']:g}°/s")
    panel.motion_rates.setVisible(bool(rates))
    link = ("状态连接待确认" if not online else "未连接电机" if not s.get("discovered") else
            "已连接 · 已使能" if s.get("motors_enabled") else "已连接 · 未使能")
    disabled = s.get("motor_disable") or (s.get("disable") or {}).get("details") or {}
    unconfirmed = [row for row in disabled.get("motors", [])
                   if row.get("requires_confirmation") and row.get("state") != "confirmed"
                   or row.get("state") == "request_failed"]
    if unconfirmed:
        link = "失能未确认 · " + "、".join(f"{row['bus']}#{row['motor_id']}" for row in unconfirmed)
    panel.link_value.setText(link)
    fault = s.get("fault") if phase == "aborted" else None
    panel.fault_banner.setText("检查中止：" + fault if fault else "")
    panel.fault_banner.setVisible(bool(fault))
    panel.progress_text.setText("正在启动…" if pending else f"检查中 · {min(done+1,total)} / {total} 项" if active
                               else "已中止" if phase == "aborted" else "检查完成" if phase == "finished" else "等待开始")
    transfer = s.get("last_transfer") or {}
    completion = s.get("completion") or {}
    if active and done == total > 0:
        panel.progress_text.setText("检查项已记录 · 回零收尾中")
    if active and phase == "homing":
        panel.progress_text.setText("自动回零 · 等待反馈停稳" if transfer.get("waiting_for_settle") else
                                   f"自动回零 · {round(100*transfer.get('command_progress', 0))}%")
    panel.progress.setValue(round(done * 100 / total) if total else 0)
    stage = (3 if phase == "finished" else 2 if phase == "returning" or done == total > 0 else
             0 if phase in {"idle", "waiting_feedback", "gain_transition", "homing"} and not done else 1)
    for index, step in enumerate(panel.steps):
        selected = index == stage and (active or phase == "finished")
        step.setStyleSheet("background: #e4efff; color: #155ccc; font-weight: 600;" if selected else
                           "background: transparent; color: #61748d;")
    if current:
        panel.current_value.setText(f"{joint_label(current['joint'])} · {'上限' if current['sign'] > 0 else '下限'}")
        if active and attempt is not None:
            title = "复搜" if retrying else "首轮搜索"
            panel.current_value.setText(f"{panel.current_value.text()} · {title} {attempt}/{attempt_limit}")
        elif active and extending and phase == "search":
            panel.current_value.setText(f"{panel.current_value.text()} · 延伸搜索")
        kind = "估算力矩" if current.get("torque_is_estimated") else "力矩"
        panel.measurement.setText(f"实测角度 {number(current.get('actual_deg'))}°    ·    {kind} {number(current.get('torque_nm'), signed=False)} N·m")
        state = ("正在按实测位移退让" if phase == "retreat" else
                 "推进已停止，正在完成接触判定" if current.get("search_hold") else
                 "基线已冻结，观察角度和力矩" if current.get("baseline_frozen") else "随实际运动更新负载基线")
        panel.contact_detail.setText(
            f"负载基线 {number(current.get('baseline_nm'), signed=False)} N·m  ·  "
            f"接触门槛 {number(current.get('contact_threshold_nm'), signed=False)} N·m\n{state}"
            if "baseline_nm" in current else "正在采集搜索负载基线")
        if active and retrying and phase == "baseline":
            panel.contact_detail.setText("退让已确认，正在重新采集复搜负载基线")
        elif active and attempt == 1 and attempt_limit == 2 and phase == "retreat" and not candidate:
            panel.contact_detail.setText("首轮未检出，确认退让后低速复搜")
    else:
        panel.contact_detail.clear()
        panel.current_value.setText("从当前姿态平滑回零" if phase == "homing" else
                                    "返回零点" if phase == "returning" else
                                    "回零收尾中止" if phase == "aborted" and transfer and done == total > 0 else
                                    "回零或转场中止" if phase == "aborted" and transfer else
                                    "等待开始" if not done else "检查已结束" if not active else "转场中")
        panel.measurement.setText("实测角度 —    ·    力矩 —")
        if transfer and (active or phase == "aborted"):
            actual, goal = transfer.get("actual_deg", []), transfer.get("goal_deg", [])
            if actual and len(actual) == len(goal):
                error = max(abs(a-b) for a,b in zip(actual, goal))
                tolerance = transfer['arrival_tolerance_deg']
                condition = ("角差仅记录，等待实测停稳" if tolerance is None
                             else f"到位范围 ±{tolerance:g}°")
                panel.measurement.setText(f"最大目标角差 {error:.2f}°    ·    {condition}")
    if phase in {"arm_hanging", "arm_settling", "arm_holding"}:
        panel.progress_text.setText("肘部缓慢下垂收尾" if phase == "arm_hanging" else
                                   "下垂姿态 · 等待实测停稳" if phase == "arm_settling" else "下垂收尾需处理 · 保持目标")
        panel.current_value.setText(panel.progress_text.text())
        actual, target = completion.get("actual_joint_deg", []), completion.get("target_joint_deg", [])
        if len(actual) > 3 and len(target) > 3:
            panel.measurement.setText(f"肘部实测 {actual[3]:+.2f}°  ·  目标 {target[3]:+.2f}°"
                                      f"  ·  已停稳 {completion.get('stable_seconds', 0):.1f} s")
        panel.contact_detail.setText("下垂轨迹完成并连续停稳后，再逐台确认失能。")
        if completion.get("alarm"):
            panel.fault_banner.setText(completion["alarm"])
            panel.fault_banner.show()
    blocking = (completion.get("blocking") if phase == "arm_settling"
                else current.get("settle_blocking") if current else transfer.get("settle_blocking"))
    if active and blocking:
        reason = {"speed": "速度", "span": "角度波动", "tolerance": "目标角差"}.get(blocking["reason"], blocking["reason"])
        message = f"等待 {joint_label(blocking['joint'])} 停稳 · {reason} {blocking['value']:.3g}{blocking['unit']}"
        if phase == "arm_settling":
            message += f" · 已等 {completion.get('waiting_seconds', 0):.1f} s"
        panel.contact_detail.setText(message)
    if unconfirmed and (disabled.get("state") in {"unconfirmed", "request_failed"}
                        or (s.get("disable") or {}).get("request") == "failed"):
        panel.progress_text.setText("检查已记录 · 电机失能未全部确认")
        if completion.get("normal_completed"):
            panel.current_value.setText("肘部缓降完成 · 失能待确认")
    limited = sum(row.get("outcome") == "fixture_limited" for row in results)
    errors = sum(row.get("outcome") != "within_reference" for row in results) - limited
    panel.counts.setText(f"{done} 已记录    {errors} 异常    {max(0,total-done-(1 if active and current else 0))} 待检查")
    if limited:
        panel.counts.setText(panel.counts.text() + f"    {limited} 工装限行")
    if s.get("record_error"):
        panel.record_path.setText("自动保存失败：" + s["record_error"])
    elif s.get("report_file"):
        panel.record_path.setText("本机报告：" + s["report_file"])
    else:
        panel.record_path.setText("结果自动保存在本机。尚未检查的项目不显示实测值。")
    stamp = json.dumps((rows, results, candidate, (current.get("joint"), current.get("sign"), attempt, extending), phase, active))
    if panel._table_stamp == stamp:
        return
    # Snapshot primitives only; polling must not overwrite the comparison stamp in place.
    panel._table_stamp = stamp
    measured = {(r["joint"], r["sign"]): r for r in results}
    panel.results.setRowCount(total)
    outcomes = {"fixture_limited": "工装 −20° 截止 · 机械下限未测", "within_reference": "参考范围内", "early": "提前阻挡", "late": "超出参考", "not_detected": "未检出"}
    for index, ref in enumerate(rows):
        key = (ref["joint"], ref["sign"]); result = measured.get(key)
        is_current = key == (current.get("joint"), current.get("sign"))
        if not result and candidate and key == (candidate["joint"], candidate["sign"]):
            result = candidate
        outcome = outcomes.get(result.get("outcome"), "候选已记录 · 待退让") if result else (
            "延伸搜索中" if is_current and active and extending and phase == "search" else
            "复搜中" if is_current and active and retrying else
            "搜索中" if is_current and active and phase == "search" else "处理中" if is_current and active else
            "未检查" if phase == "aborted" else "待检查")
        if result and result.get("search_extension") and result.get("outcome") == "not_detected":
            outcome = ("推进停滞 · 止挡待确认" if result.get("search_end_reason") == "feedback_stall"
                       else "行程到界 · 止挡待确认")
        tone = "#157452" if result and result.get("outcome") == "within_reference" else (
            "#b45309" if result else "#2367c4" if is_current and active else "#7a8799")
        result = result or {}
        load = number(result.get("torque_median_nm", result.get("torque_floor_nm")), signed=False)
        if load != "—" and result.get("torque_is_estimated"):
            load += "（估算）"
        values = [f"{joint_label(ref['joint'])} / {'上限' if ref['sign'] > 0 else '下限'}",
                  number(ref["reference_deg"]), number(result.get("actual_deg")),
                  number(result.get("deviation_deg")), load, outcome]
        result_tip = search_attempt_tooltip(result.get("search_attempts") or [])
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
            item.setToolTip(ref["joint"] if column == 0 else result_tip if column == 5 else
                            "Iq × 实测 Kt 换算的估算力矩" if column == 4 and result.get("torque_is_estimated") else "")
