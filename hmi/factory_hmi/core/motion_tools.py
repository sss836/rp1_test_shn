"""Offline audit and explicit derivative trajectory generation; never controls CAN."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np

from .config import load_station_config
from .manual_control import joint_positions_to_motor, motor_positions_to_joint
from .motion_completion import MotionParameters, configuration_sha256, motion_readiness
from .trajectory import import_trajectory, file_sha256
from factory_hmi.models import TrajectoryData


def extend_j5(config, source, output: Path, minimum: float, maximum: float, fps: float = 50.) -> dict:
    if config.limb not in {'left_short_arm', 'right_short_arm'} or len(config.entries) != 5:
        raise ValueError('只支持左右 5DOF；不处理 7DOF')
    if not (np.isfinite(minimum) and np.isfinite(maximum) and minimum < 0 < maximum):
        raise ValueError('必须显式指定跨零的双向 J5 目标')
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError('fps 必须为正有限值')
    params = MotionParameters.load(config)
    params.check_trajectory(config, source)
    joint = motor_positions_to_joint(config, source.motor_pos[-1])
    rows, times = [row.copy() for row in source.motor_pos], list(source.timestamps)
    for target_j5 in (minimum, maximum, joint[4]):
        initial = motor_positions_to_joint(config, rows[-1])
        target = initial.copy(); target[4] = target_j5
        target_motor = joint_positions_to_motor(config, target)
        params.check_target(config, target_motor)
        distance = np.abs(target_motor - rows[-1])
        duration = max(float(np.max(1.875 * distance / params.values['max_velocity_rad_s'])),
            float(np.max(np.sqrt(5.774 * distance / params.values['max_acceleration_rad_s2']))), 1/fps)
        # Uniform samples keep derivative checks and the stored fps consistent.
        steps = max(2, int(np.ceil(duration*fps)))
        duration = steps/fps
        origin, started = rows[-1].copy(), times[-1]
        for index in range(1, steps+1):
            x=index/steps; blend=x**3*(10+x*(-15+6*x))
            rows.append(origin+(target_motor-origin)*blend)
            times.append(started+index/fps)
    values, timestamps = np.asarray(rows), np.asarray(times)
    derivative = TrajectoryData(output, config.limb, values, timestamps, fps, '', {})
    params.check_trajectory(config, derivative)
    # Exclusive creation: source, existing output, standard bundles and custom
    # trajectories cannot be overwritten by this tool.
    if output.resolve() == source.path.resolve():
        raise ValueError('必须生成新文件，禁止覆盖源轨迹')
    evidence_path = output.with_suffix('.provenance.json')
    if output.exists() or evidence_path.exists():
        raise FileExistsError('输出或来源证据已存在，拒绝覆盖')
    with output.open('xb') as stream:
        np.savez(stream, motor_pos=values, time=timestamps, fps=[fps], limb=[config.limb])
    evidence = dict(source_sha256=source.sha256, generated_sha256=file_sha256(output),
        configuration_sha256=params.configuration_sha256, parameter_revision=params.parameter_revision,
        limb=config.limb, original_frames=source.frame_count, generated_frames=len(rows),
        j5_joint_minimum_rad=minimum, j5_joint_maximum_rad=maximum,
        other_joint_ranges_preserved=True, physical_effect_verified=False)
    with evidence_path.open('x', encoding='utf-8') as stream:
        json.dump(evidence, stream, indent=2, ensure_ascii=False)
    return evidence


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['hash', 'audit', 'extend-j5'])
    parser.add_argument('--limb', required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--minimum-j5-rad', type=float)
    parser.add_argument('--maximum-j5-rad', type=float)
    parser.add_argument('--fps', type=float, default=50.)
    parser.add_argument('--interfaces', nargs='+', help='effective ordered CAN bus bindings')
    args=parser.parse_args()
    base=load_station_config(args.limb, args.config)
    bindings = ([dict(index=i, interface=name, mode=base.entries[0].interface_type) for i,name in enumerate(args.interfaces)]
                if args.interfaces else None)
    config=load_station_config(args.limb, args.config, bus_bindings=bindings)
    result=dict(limb=config.limb, configuration_sha256=configuration_sha256(config),
                motion_readiness=motion_readiness(config))
    if args.operation != 'hash':
        if args.input is None:
            parser.error('--input required')
        trajectory=import_trajectory(args.input, limb=config.limb, motor_count=len(config.entries))
        joints=np.vstack([motor_positions_to_joint(config, row) for row in trajectory.motor_pos])
        result.update(frames=trajectory.frame_count, duration_s=trajectory.duration_s,
            trajectory_sha256=trajectory.sha256, motor_min_rad=np.min(trajectory.motor_pos, axis=0).tolist(),
            motor_max_rad=np.max(trajectory.motor_pos, axis=0).tolist(),
            joint_min_rad=np.min(joints, axis=0).tolist(), joint_max_rad=np.max(joints, axis=0).tolist(),
            mapping=[dict(joint_name=e.joint_name, motor_id=e.motor_id, sign=e.sign,
                          zero_offset=e.zero_offset, interface=e.interface) for e in config.entries],
            actual_feedback_verified=False)
        if args.operation == 'extend-j5':
            if args.output is None or args.minimum_j5_rad is None or args.maximum_j5_rad is None:
                parser.error('--output --minimum-j5-rad --maximum-j5-rad required')
            result['generated']=extend_j5(config, trajectory, args.output,
                args.minimum_j5_rad, args.maximum_j5_rad, args.fps)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
