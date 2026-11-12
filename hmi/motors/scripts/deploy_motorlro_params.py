#!/usr/bin/env python3
"""
Read or write LeadRobot (LRO) MIT limit parameters, torque coefficient Kt,
CAN timeout, common and model-specific Pn settings, and PnE71 overvoltage level.

Examples:
  python3 deploy_motorlro_params.py --mode read
  python3 deploy_motorlro_params.py --mode write -v

Protocol notes:
  - Query:  ID=motor_id, [0xE0, query_code]
  - Config: ID=motor_id, [0xC0, cfg_code, value_hi, value_lo, ...]
  - Pn write: ID=motor_id, [0xC0, 0x10, pn_hi, pn_lo, value_31..value_0]
  - Pn read: ID=motor_id, [0xC0, 0x11, pn_hi, pn_lo]
  - Multi-byte values are stored high byte first, low byte last.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import re
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import yaml


DEFAULT_CONFIG = Path("./src/inference/config/robot.yaml")
CAN_ID_MASK = 0x7FF
CANFD_INTERFACE_TYPES = {"canfd", "ethercanfd"}

CANDUMP_FRAME_LIMIT = 10
DUMP_SETUP_DELAY_S = 0.05
READ_SETTLE_DELAY_S = 0.15
WRITE_DELAY_S = 0.10
WRITE_SETTLE_DELAY_S = 0.30

HEX_BYTE_RE = re.compile(r"^[0-9A-Fa-f]{2}$")


@dataclass(frozen=True)
class MotorTarget:
    iface: str
    iface_type: str
    motor_id: int
    model: int


# Model enum (lro_motor_driver.hpp):
#   0 = LRO_5550, 1 = LRO_6562, 2 = LRO_8462, 3 = LRO_10062
# Kt values fit the mid-load 48 V PJ2/PJ3 torque tests using Iq current.
# CUR limits are Iq encoding ranges with headroom, assuming report AC currents
# are RMS and amplitude-invariant Park Iq = sqrt(2) * phase RMS at Id = 0.
MODEL_PARAMS: Dict[int, Dict[str, float]] = {
    0: {
        "name": "55",
        "kt": 1.74,
        "kp_max": 500.0,
        "kd_max": 50.0,
        "pos_max": 12.5,
        "spd_max": 20.0,
        "tau_max": 30.0,
        "cur_max": 18.0,
    },
    1: {
        "name": "60",
        "kt": 1.62,
        "kp_max": 500.0,
        "kd_max": 50.0,
        "pos_max": 12.5,
        "spd_max": 40.0,
        "tau_max": 40.0,
        "cur_max": 32.0,
    },
    2: {
        "name": "75",
        "kt": 2.31,
        "kp_max": 500.0,
        "kd_max": 50.0,
        "pos_max": 12.5,
        "spd_max": 35.0,
        "tau_max": 65.0,
        "cur_max": 35.0,
    },
    3: {
        "name": "97",
        "kt": 2.38,
        "kp_max": 500.0,
        "kd_max": 50.0,
        "pos_max": 12.5,
        "spd_max": 16.0,
        "tau_max": 165.0,
        "cur_max": 90.0,
    },
}


LRO_QUERY_HEADER = 0xE0   # 0x07 << 5
LRO_CONFIG_HEADER = 0xC0  # 0x06 << 5
LRO_FB_TYPE_MASK = 0xE0
LRO_FB_TYPE_QUERY = 0xA0  # 0x05 << 5
PN_WRITE_CODE = 0x10
PN_READ_CODE = 0x11
PNE71_PARAMETER_NUMBER = 0x0E71
PNE71_OVERVOLTAGE_LEVEL = 75
PN_SETTINGS = (
    (0x0401, 200),
    (0x0402, 300),
    (0x0403, 300),
)
# Raw values from the PJ3 expected-parameter CSVs; no values supplied for model 0.
MODEL_PN_SETTINGS = {
    1: ((0x0407, 10000), (0x0E05, 56), (0x0EB2, 45), (0x0F09, 65), (0x0F0A, 287)),
    2: ((0x0407, 10000), (0x0E05, 56), (0x0EB2, 39)),
    3: ((0x0407, 5000), (0x0E05, 56), (0x0EB2, 40)),
}

QUERY_CODE = {
    "kt": 0x16,
    "kp": 0x17,
    "kd": 0x18,
    "pos": 0x19,
    "spd": 0x1A,
    "tor": 0x1B,
    "cur": 0x1C,
    "can_timeout": 0x1F,
}

CONFIG_CODE = {
    "kt": 0x04,
    "kp": 0x05,
    "kd": 0x06,
    "pos": 0x07,
    "spd": 0x08,
    "tor": 0x09,
    "cur": 0x0A,
    "can_timeout": 0x0B,
}

SCALE = {
    "kt": 100,
    "kp": 1,
    "kd": 1,
    "pos": 100,
    "spd": 100,
    "tor": 10,
    "cur": 10,
    "can_timeout": 1,
}

IS_SIGNED = {
    "kt": False,
    "kp": False,
    "kd": False,
    "pos": True,
    "spd": True,
    "tor": True,
    "cur": True,
    "can_timeout": False,
}

PARAM_DEFS = [
    ("kt", "KT"),
    ("kp", "KP"),
    ("kd", "KD"),
    ("pos", "POS"),
    ("spd", "SPD"),
    ("tor", "TOR"),
    ("cur", "CUR"),
    ("can_timeout", "CAN_TIMEOUT"),
]

SINGLE_VALUE_KEYS = {"kt", "can_timeout"}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read or write LeadRobot motor Kt, MIT limits, CAN timeout, and Pn settings."
    )
    parser.add_argument(
        "--mode",
        choices=["read", "write"],
        default="read",
        help="Mode: read or write parameters (default: read)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed output for every parameter",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Path to robot.yaml (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--interface", action="append", default=[],
                        help="Only this CAN interface; repeat to select several")
    parser.add_argument("--parallel", action="store_true",
                        help="Process interfaces concurrently, motors within each interface sequentially; "
                             "other interfaces finish even if one fails")
    return parser.parse_args(argv)


def vprint(verbose: bool, *msg: object) -> None:
    if verbose:
        print(*msg)


def require_list(mapping: Dict[str, object], key: str) -> List[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise RuntimeError(f"robot.yaml motors.{key} must be a list")
    return value


def load_config(config_path: Path) -> Dict[str, object]:
    if not config_path.exists():
        raise RuntimeError(f"Could not find {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Failed to parse {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise RuntimeError(f"{config_path} must contain a YAML mapping")
    return config


def build_motor_targets(config: Dict[str, object], target_type: str) -> List[MotorTarget]:
    motors = config.get("motors")
    if not isinstance(motors, dict):
        raise RuntimeError("robot.yaml missing motors section")

    motor_ids = require_list(motors, "motor_id")
    interfaces = require_list(motors, "motor_interface")
    motor_nums = require_list(motors, "motor_num")
    motor_types = require_list(motors, "motor_type")
    motor_models = require_list(motors, "motor_model")

    interface_types = motors.get("motor_interface_type")
    if interface_types is None:
        interface_types = ["can"] * len(interfaces)
    if not isinstance(interface_types, list):
        raise RuntimeError("robot.yaml motors.motor_interface_type must be a list")

    if len(interfaces) != len(motor_nums) or len(interfaces) != len(motor_types):
        raise RuntimeError(
            "robot.yaml motors.motor_interface, motor_num, and motor_type "
            "must have the same length"
        )

    try:
        motor_counts = [int(count) for count in motor_nums]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("robot.yaml motors.motor_num must contain integers") from exc

    expected_motors = sum(motor_counts)
    if len(motor_ids) < expected_motors or len(motor_models) < expected_motors:
        raise RuntimeError(
            "robot.yaml motors.motor_id and motor_model must contain at least "
            f"{expected_motors} entries"
        )

    targets: List[MotorTarget] = []
    offset = 0
    for bus_idx, iface in enumerate(interfaces):
        count = motor_counts[bus_idx]
        bus_type = str(motor_types[bus_idx]).upper()
        iface_type = (
            str(interface_types[bus_idx]).lower()
            if bus_idx < len(interface_types)
            else "can"
        )

        for local_idx in range(count):
            motor_idx = offset + local_idx
            if bus_type == target_type:
                targets.append(
                    MotorTarget(
                        iface=str(iface),
                        iface_type=iface_type,
                        motor_id=int(motor_ids[motor_idx]),
                        model=int(motor_models[motor_idx]),
                    )
                )
        offset += count

    return targets


def is_canfd_interface(iface_type: str) -> bool:
    return iface_type.lower() in CANFD_INTERFACE_TYPES


def proto_label(iface_type: str) -> str:
    return "CANFD" if is_canfd_interface(iface_type) else "CAN"


def can_id_hex(can_id: int) -> str:
    return f"{can_id:03X}"


def cansend_frame_arg(can_id: int, data_hex: str, iface_type: str) -> str:
    separator = "##1" if is_canfd_interface(iface_type) else "#"
    return f"{can_id_hex(can_id)}{separator}{data_hex}"


def send_frame(
    iface: str,
    iface_type: str,
    can_id: int,
    data_hex: str,
    verbose: bool,
) -> None:
    frame_arg = cansend_frame_arg(can_id, data_hex, iface_type)
    cmd = ["cansend", iface, frame_arg]
    vprint(verbose, "    " + " ".join(cmd))

    try:
        subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("'cansend' not found; install can-utils") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"exit code {exc.returncode}"
        raise RuntimeError(f"cansend failed on {iface}: {detail}") from exc


def stop_process(proc: subprocess.Popen[str]) -> Tuple[str, str]:
    if proc.poll() is None:
        proc.terminate()
    try:
        return proc.communicate(timeout=1.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


def capture_response(
    iface: str,
    response_can_id: int,
    send_request: Callable[[], None],
) -> str:
    source = f"{iface},{can_id_hex(response_can_id)}:{CAN_ID_MASK:03X}"
    dump_proc: Optional[subprocess.Popen[str]] = None
    out = ""
    err = ""

    try:
        dump_proc = subprocess.Popen(
            ["candump", "-n", str(CANDUMP_FRAME_LIMIT), source],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(DUMP_SETUP_DELAY_S)
        send_request()
        time.sleep(READ_SETTLE_DELAY_S)
    except FileNotFoundError as exc:
        raise RuntimeError("'candump' not found; install can-utils") from exc
    finally:
        if dump_proc is not None:
            out, err = stop_process(dump_proc)

    if dump_proc is not None and dump_proc.returncode not in (0, -15) and err.strip():
        raise RuntimeError(f"candump failed on {iface}: {err.strip()}")

    return out


def iter_candump_frames(output: str) -> Iterable[Tuple[int, List[int]]]:
    for line in output.splitlines():
        parts = line.strip().split()
        if not parts:
            continue

        dlc_idx = next(
            (idx for idx, part in enumerate(parts) if part.startswith("[") and part.endswith("]")),
            None,
        )
        if dlc_idx is None or dlc_idx == 0:
            continue

        try:
            can_id = int(parts[dlc_idx - 1], 16)
        except ValueError:
            continue

        payload = [
            int(part, 16)
            for part in parts[dlc_idx + 1 :]
            if HEX_BYTE_RE.fullmatch(part)
        ]
        yield can_id, payload


def bytes_to_hex(data: Iterable[int]) -> str:
    return "".join(f"{byte:02X}" for byte in data)


def pack_u16(value: int) -> str:
    return bytes_to_hex(struct.pack(">H", value & 0xFFFF))


def pack_u32(value: int) -> str:
    return bytes_to_hex(struct.pack(">I", value & 0xFFFFFFFF))


def pack_i16(value: int) -> str:
    return bytes_to_hex(struct.pack(">h", value))


def unpack_u16(hi: int, lo: int) -> int:
    return struct.unpack(">H", bytes([hi, lo]))[0]


def unpack_u32(data: Iterable[int]) -> int:
    raw = bytes(data)
    if len(raw) != 4:
        raise ValueError("uint32 payload must contain exactly four bytes")
    return struct.unpack(">I", raw)[0]


def unpack_i16(hi: int, lo: int) -> int:
    return struct.unpack(">h", bytes([hi, lo]))[0]


def build_config_frame(
    key: str,
    min_raw: int,
    max_raw: int,
    signed: bool,
) -> str:
    cfg_code = CONFIG_CODE[key]
    if key in SINGLE_VALUE_KEYS:
        return f"{LRO_CONFIG_HEADER:02X}{cfg_code:02X}{pack_u16(max_raw)}"

    pack_value = pack_i16 if signed else pack_u16
    return (
        f"{LRO_CONFIG_HEADER:02X}{cfg_code:02X}"
        f"{pack_value(min_raw)}{pack_value(max_raw)}"
    )


def build_query_frame(query_code: int) -> str:
    return f"{LRO_QUERY_HEADER:02X}{query_code:02X}"


def build_pn_write_frame(parameter_number: int, value: int) -> str:
    """Build the V109+ protocol's Pn parameter write frame."""
    return (
        f"{LRO_CONFIG_HEADER:02X}{PN_WRITE_CODE:02X}"
        f"{pack_u16(parameter_number)}{pack_u32(value)}"
    )


def build_pn_read_frame(parameter_number: int) -> str:
    return f"{LRO_CONFIG_HEADER:02X}{PN_READ_CODE:02X}{pack_u16(parameter_number)}"


def expected_values(model_params: Dict[str, float], key: str) -> Tuple[float, float]:
    if key == "kt":
        return 0.0, float(model_params["kt"])
    if key == "kp":
        return 0.0, float(model_params["kp_max"])
    if key == "kd":
        return 0.0, float(model_params["kd_max"])
    if key == "pos":
        return -float(model_params["pos_max"]), float(model_params["pos_max"])
    if key == "spd":
        return -float(model_params["spd_max"]), float(model_params["spd_max"])
    if key == "tor":
        return -float(model_params["tau_max"]), float(model_params["tau_max"])
    if key == "cur":
        return -float(model_params["cur_max"]), float(model_params["cur_max"])
    if key == "can_timeout":
        return 0.0, 0.0
    raise RuntimeError(f"Unsupported LRO parameter key: {key}")


def value_to_raw(key: str, value: float) -> int:
    return int(round(value * SCALE[key]))


def close_enough(actual: float, expected: float) -> bool:
    tolerance = max(0.001, abs(expected) * 0.01 + 0.001)
    return abs(actual - expected) < tolerance


def parse_query_response(
    output: str,
    response_can_id: int,
    query_code: int,
    key: str,
) -> Optional[Tuple[float, float]]:
    signed = IS_SIGNED[key]
    scale = SCALE[key]
    min_payload_len = 4 if key in SINGLE_VALUE_KEYS else 6

    for can_id, payload in iter_candump_frames(output):
        if can_id != response_can_id or len(payload) < min_payload_len:
            continue
        if (payload[0] & LRO_FB_TYPE_MASK) != LRO_FB_TYPE_QUERY:
            continue
        if payload[1] != query_code:
            continue

        if key in SINGLE_VALUE_KEYS:
            return 0.0, unpack_u16(payload[2], payload[3]) / scale

        if signed:
            min_raw = unpack_i16(payload[2], payload[3])
            max_raw = unpack_i16(payload[4], payload[5])
        else:
            min_raw = unpack_u16(payload[2], payload[3])
            max_raw = unpack_u16(payload[4], payload[5])

        return min_raw / scale, max_raw / scale

    return None


def query_param(
    target: MotorTarget,
    query_code: int,
    key: str,
    verbose: bool,
) -> Optional[Tuple[float, float]]:
    def send_request() -> None:
        send_frame(
            target.iface,
            target.iface_type,
            target.motor_id,
            build_query_frame(query_code),
            verbose,
        )

    output = capture_response(target.iface, target.motor_id, send_request)
    return parse_query_response(output, target.motor_id, query_code, key)


def write_param(
    target: MotorTarget,
    key: str,
    min_raw: int,
    max_raw: int,
    verbose: bool,
) -> None:
    data_hex = build_config_frame(key, min_raw, max_raw, IS_SIGNED[key])
    send_frame(target.iface, target.iface_type, target.motor_id, data_hex, verbose)


def write_pn_e71_overvoltage(target: MotorTarget, verbose: bool) -> None:
    data_hex = build_pn_write_frame(
        PNE71_PARAMETER_NUMBER,
        PNE71_OVERVOLTAGE_LEVEL,
    )
    vprint(
        verbose,
        f"  [WRITE] PnE71 overvoltage detection level: "
        f"{PNE71_OVERVOLTAGE_LEVEL} V (raw {PNE71_OVERVOLTAGE_LEVEL})",
    )
    send_frame(target.iface, target.iface_type, target.motor_id, data_hex, verbose)


def parse_pn_read_response(
    output: str,
    response_can_id: int,
    parameter_number: int,
) -> Optional[int]:
    for can_id, payload in iter_candump_frames(output):
        if can_id != response_can_id or len(payload) < 7:
            continue
        if payload[:3] != [0xFF, 0xFE, PN_READ_CODE]:
            continue
        if len(payload) >= 7:
            return unpack_u32(payload[3:7])
    return None


def query_pn(target: MotorTarget, parameter_number: int, verbose: bool) -> Optional[int]:
    def send_request() -> None:
        send_frame(
            target.iface,
            target.iface_type,
            target.motor_id,
            build_pn_read_frame(parameter_number),
            verbose,
        )

    output = capture_response(target.iface, target.motor_id, send_request)
    return parse_pn_read_response(output, target.motor_id, parameter_number)


def run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    targets = build_motor_targets(config, "LRO")
    missing = set(args.interface) - {target.iface for target in targets}
    if missing:
        raise RuntimeError(f"Unmatched interfaces: {sorted(missing)}")
    if args.interface:
        targets = [target for target in targets if target.iface in args.interface]
    if not targets:
        print("No LRO motors found in robot.yaml.")
        return 0

    if args.parallel:
        return run_parallel(parallel_commands(args, targets))

    total_ok = 0
    total_fail = 0
    total_skip = 0
    motor_results: List[Tuple[MotorTarget, str, int, int]] = []

    mode_label = "WRITE" if args.mode == "write" else "READ"
    print(f"LeadRobot parameter {mode_label}")
    print("-" * 55)

    for target in targets:
        model_params = MODEL_PARAMS.get(target.model)
        if model_params is None:
            print(
                f"  [SKIP] Motor {target.motor_id} on {target.iface}: "
                f"unknown model {target.model}"
            )
            total_skip += 1
            continue

        model_name = str(model_params["name"])
        pn_settings = PN_SETTINGS + MODEL_PN_SETTINGS.get(target.model, ())
        param_defs = [
            param_def
            for param_def in PARAM_DEFS
            if param_def[0] != "kt" or "kt" in model_params
            if param_def[0] != "cur" or "cur_max" in model_params
        ]
        label = proto_label(target.iface_type)
        motor_ok = 0
        motor_fail = 0
        fail_details: List[str] = []

        vprint(
            args.verbose,
            f"\nMotor ID {target.motor_id} on {target.iface} [{label}] "
            f"(Model {model_name})",
        )
        if "kt" not in model_params:
            vprint(
                args.verbose,
                f"  [SKIP] KT: no calibrated model-wide value for Model {model_name}",
            )
        if "cur_max" not in model_params:
            print(f"  [SKIP] CUR: no confirmed maximum current for Model {model_name}")

        if args.mode == "write":
            for key, param_label in param_defs:
                exp_min, exp_max = expected_values(model_params, key)
                min_raw = value_to_raw(key, exp_min)
                max_raw = value_to_raw(key, exp_max)

                if key == "can_timeout":
                    vprint(
                        args.verbose,
                        f"  [WRITE] {param_label}: {exp_max:.0f} ms (0=disabled)",
                    )
                elif key in SINGLE_VALUE_KEYS:
                    vprint(
                        args.verbose,
                        f"  [WRITE] {param_label}: {exp_max:.4f} "
                        f"(raw {max_raw})",
                    )
                else:
                    vprint(
                        args.verbose,
                        f"  [WRITE] {param_label}: MIN={exp_min:.4f} "
                        f"MAX={exp_max:.4f} (raw {min_raw}, {max_raw})",
                    )

                write_param(target, key, min_raw, max_raw, args.verbose)
                time.sleep(WRITE_DELAY_S)

            # The protocol exposes Pn writes separately from the MIT parameter
            # commands. Read it back using the V109+ Pn read command below.
            for parameter_number, value in pn_settings:
                vprint(args.verbose, f"  [WRITE] Pn{parameter_number:03X}: {value}")
                send_frame(
                    target.iface,
                    target.iface_type,
                    target.motor_id,
                    build_pn_write_frame(parameter_number, value),
                    args.verbose,
                )
                time.sleep(WRITE_DELAY_S)

            write_pn_e71_overvoltage(target, args.verbose)
            time.sleep(WRITE_DELAY_S)

            vprint(args.verbose, "  [INFO] Config sent; LRO applies params immediately")
            time.sleep(WRITE_SETTLE_DELAY_S)

        action = "[VERIFY]" if args.mode == "write" else "[READ]"
        vprint(args.verbose, f"  {action} Querying parameters")

        for key, param_label in param_defs:
            exp_min, exp_max = expected_values(model_params, key)
            result = query_param(target, QUERY_CODE[key], key, args.verbose)

            if result is None:
                motor_fail += 1
                fail_details.append(f"      {param_label}: no response")
                vprint(args.verbose, f"    {param_label}: no response")
                continue

            read_min, read_max = result
            if args.mode == "read":
                if key == "can_timeout":
                    vprint(args.verbose, f"    {param_label}: {read_max:.0f} ms (0=disabled)")
                elif key in SINGLE_VALUE_KEYS:
                    vprint(
                        args.verbose,
                        f"    {param_label}: {read_max:.4f} "
                        f"(expected {exp_max:.4f})",
                    )
                else:
                    vprint(
                        args.verbose,
                        f"    {param_label}: MIN={read_min:.4f} MAX={read_max:.4f} "
                        f"(expected MAX={exp_max:.4f})",
                    )
                if key == "can_timeout":
                    ok = abs(read_max - exp_max) < 1.0
                else:
                    ok = close_enough(read_min, exp_min) and close_enough(read_max, exp_max)
                if ok:
                    motor_ok += 1
                else:
                    motor_fail += 1
                    if key in SINGLE_VALUE_KEYS or key == "can_timeout":
                        detail = (
                            f"      {param_label}: expected {exp_max:.4f}, "
                            f"got {read_max:.4f}"
                        )
                    else:
                        detail = (
                            f"      {param_label}: expected MIN={exp_min:.4f} "
                            f"MAX={exp_max:.4f}, got MIN={read_min:.4f} "
                            f"MAX={read_max:.4f}"
                        )
                    fail_details.append(detail)
                    vprint(args.verbose, f"    Mismatch {param_label}: {detail.strip()}")
                continue

            if key == "can_timeout":
                ok = abs(read_max - exp_max) < 1.0
            else:
                ok = close_enough(read_min, exp_min) and close_enough(read_max, exp_max)

            if ok:
                if key == "can_timeout":
                    vprint(args.verbose, f"    Confirmed {param_label}: {read_max:.0f} ms")
                elif key in SINGLE_VALUE_KEYS:
                    vprint(
                        args.verbose,
                        f"    Confirmed {param_label}: {read_max:.4f}",
                    )
                else:
                    vprint(
                        args.verbose,
                        f"    Confirmed {param_label}: MIN={read_min:.4f} "
                        f"MAX={read_max:.4f}",
                    )
                motor_ok += 1
            else:
                motor_fail += 1
                if key == "can_timeout":
                    detail = (
                        f"      {param_label}: expected {exp_max:.0f}, "
                        f"got {read_max:.0f}"
                    )
                    vprint(
                        args.verbose,
                        f"    Mismatch {param_label}: expected {exp_max:.0f}, "
                        f"got {read_max:.0f}",
                    )
                elif key in SINGLE_VALUE_KEYS:
                    detail = (
                        f"      {param_label}: expected {exp_max:.4f}, "
                        f"got {read_max:.4f}"
                    )
                    vprint(
                        args.verbose,
                        f"    Mismatch {param_label}: expected {exp_max:.4f}, "
                        f"got {read_max:.4f}",
                    )
                else:
                    detail = (
                        f"      {param_label}: expected MIN={exp_min:.4f} "
                        f"MAX={exp_max:.4f}, got MIN={read_min:.4f} "
                        f"MAX={read_max:.4f}"
                    )
                    vprint(
                        args.verbose,
                        f"    Mismatch {param_label}: expected [{exp_min:.4f}, "
                        f"{exp_max:.4f}], got [{read_min:.4f}, {read_max:.4f}]",
                    )
                fail_details.append(detail)

        for parameter_number, expected in pn_settings:
            pn_label = f"Pn{parameter_number:03X}"
            pn_value = query_pn(target, parameter_number, args.verbose)
            if pn_value == expected:
                motor_ok += 1
                vprint(args.verbose, f"    {pn_label}: {pn_value} (expected {expected})")
            else:
                motor_fail += 1
                detail = (
                    f"{pn_label}: no response"
                    if pn_value is None
                    else f"{pn_label}: expected {expected}, got {pn_value}"
                )
                fail_details.append(f"      {detail}")
                vprint(args.verbose, f"    {detail}")

        pn_value = query_pn(target, PNE71_PARAMETER_NUMBER, args.verbose)
        if pn_value is None:
            motor_fail += 1
            fail_details.append("      PnE71: no response")
            vprint(args.verbose, "    PnE71: no response")
        elif args.mode == "read" and pn_value == PNE71_OVERVOLTAGE_LEVEL:
            vprint(
                args.verbose,
                f"    PnE71 overvoltage detection level: {pn_value} V "
                f"(expected {PNE71_OVERVOLTAGE_LEVEL} V)",
            )
            motor_ok += 1
        elif pn_value == PNE71_OVERVOLTAGE_LEVEL:
            vprint(
                args.verbose,
                f"    Confirmed PnE71 overvoltage detection level: {pn_value} V",
            )
            motor_ok += 1
        else:
            motor_fail += 1
            fail_details.append(
                f"      PnE71: expected {PNE71_OVERVOLTAGE_LEVEL}, got {pn_value}"
            )
            vprint(
                args.verbose,
                f"    Mismatch PnE71: expected {PNE71_OVERVOLTAGE_LEVEL}, "
                f"got {pn_value}",
            )

        status = "OK" if motor_fail == 0 else "FAIL"
        print(
            f"  [{status}] Motor {target.motor_id} ({model_name}) on "
            f"{target.iface} [{label}]: {motor_ok}/{motor_ok + motor_fail} OK"
        )
        for detail in fail_details:
            print(detail)

        total_ok += motor_ok
        total_fail += motor_fail
        motor_results.append((target, model_name, motor_ok, motor_fail))

    print("-" * 55)
    all_total = total_ok + total_fail
    if total_skip:
        print(f"Skipped {total_skip} motor(s) with unknown model.")

    if total_fail == 0:
        print(f"Done. All {all_total} parameters OK.")
    else:
        print(f"Done. {total_ok}/{all_total} OK, {total_fail} FAILED:")
        for target, model_name, _ok, fail in motor_results:
            if fail > 0:
                print(
                    f"  Motor {target.motor_id} ({model_name}) on "
                    f"{target.iface} [{proto_label(target.iface_type)}]: {fail} failure(s)"
                )

    return 1 if total_fail else 0


def run_parallel(commands):
    """Isolate each bus in a child process and prefix its streaming output."""
    output_lock = threading.Lock()

    def worker(interface, command):
        try:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, start_new_session=True) as child:
                for line in child.stdout:
                    if line.strip():
                        with output_lock:
                            print(f"[{interface}] {line.rstrip()}", flush=True)
                return child.wait()
        except OSError as exc:
            with output_lock:
                print(f"[{interface}] FAILED: {exc}", file=sys.stderr, flush=True)
            return 1

    interrupted = False
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        futures = {interface: pool.submit(worker, interface, command)
                   for interface, command in commands.items()}
        while True:
            try:
                results = {interface: future.result() for interface, future in futures.items()}
                break
            except KeyboardInterrupt:
                interrupted = True
                print("Interrupt received; waiting for active interface jobs to finish safely.",
                      file=sys.stderr, flush=True)
    for interface, code in results.items():
        print(f"Interface {interface}: {'OK' if code == 0 else 'FAILED'} (exit={code})")
    return 130 if interrupted else int(any(results.values()))


def parallel_commands(args, targets):
    common = [sys.executable, "-u", str(Path(__file__).resolve()),
              "--config", str(args.config), "--mode", args.mode]
    if args.verbose:
        common += ["--verbose"]
    return {interface: common + ["--interface", interface]
            for interface in dict.fromkeys(t.iface for t in targets)}


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
