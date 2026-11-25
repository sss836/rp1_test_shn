#!/usr/bin/env python3
"""Build the pinned RP Hand SDK with explicit HMI safety feedback.

Usage: python tools/build_dexhand_sdk.py /path/to/clean/roboparty_dexhand
The supplied checkout is read only. All deliverables stay under hmi/dist/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

REVISION = "3d2a5abb8f7299aea9ad7371ea94f37db87f79d6"


def replace(root, path, old, new):
    file = root / path
    text = file.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"Pinned SDK patch context changed: {path}")
    file.write_text(text.replace(old, new))


def patch(root):
    replace(root, "include/rphand/types.hpp", "    bool feedback_valid = false;",
            "    std::array<bool, 6> emergency_stopped{};\n    std::array<bool, 6> stopped{};\n    bool feedback_valid = false;")
    replace(root, "src/rphand/session.cpp", "            snapshot_.emergency_stopped[i] = running == 6;",
            "            snapshot_.emergency_stopped[i] = running == 6;\n"
            "            snapshot_.state.emergency_stopped[i] = snapshot_.emergency_stopped[i];\n"
            "            snapshot_.state.stopped[i] = snapshot_.stopped[i];")
    replace(root, "src/rphand/session.cpp",
            "    if (revoke) { int rc = control(0, 0, 8); if (rc) return rc; interval(); }",
            "    // HMI: a position command must never revoke an emergency stop.\n    if (revoke) return 9;")
    replace(root, "src/hand.cpp", '        for (int fault : value.state.fault_codes)',
            '        for (bool stopped : value.emergency_stopped) if (stopped) return fail("emergency stop remains active");\n        for (int fault : value.state.fault_codes)')
    replace(root, "src/hand.cpp", "        } guard{this};\n        if (cancelled(token)) return false;",
            "        } guard{this};\n        if (cancelled(token) || !healthy(snapshot())) return false;")
    replace(root, "python/bindings.cpp", "PYBIND11_MODULE(_core, m) {",
            'PYBIND11_MODULE(_core, m) {\n    m.attr("HMI_SAFETY_API") = 1;')
    replace(root, "python/bindings.cpp", '        .def_readonly("feedback_valid", &rphand::HandState::feedback_valid);',
            '        .def_property_readonly("emergency_stopped", [](const rphand::HandState& s) { return snapshot(s.emergency_stopped); })\n'
            '        .def_property_readonly("stopped", [](const rphand::HandState& s) { return snapshot(s.stopped); })\n'
            '        .def_readonly("feedback_valid", &rphand::HandState::feedback_valid);')
    replace(root, "python/bindings.cpp", '    hand.def("disconnect",',
            '    hand.def("prepare_disconnect", [](PythonHand& s) { return s.hand->prepare_disconnect(); }, py::call_guard<py::gil_scoped_release>());\n    hand.def("disconnect",')
    replace(root, "python/rphand/__init__.py", "from ._core import Backend, ConnectionOptions, Hand, HandState",
            "from ._core import Backend, ConnectionOptions, Hand, HandState, HMI_SAFETY_API")
    replace(root, "pyproject.toml", 'version = "0.1.0"', 'version = "0.1.0+rp1hmi.1"')
    replace(root, "tests/session.cpp", "        protocol_frames();", '''        {
            Device d;
            check(d.initial() == 0, "HMI simulated initialization");
            Bytes status(64); status[0] = 0xda; status[1] = 31;
            for (int i = 0; i < 6; ++i) status[2 + 2*i] = 0xd5;
            d.feedback(status);
            check(d.hand.snapshot().state.emergency_stopped[0], "HMI public emergency feedback");
            check(d.hand.snapshot().state.stopped[0], "HMI public stationary feedback");
            d.clear();
            check(d.hand.move_motors(0) != 0 && d.frames.empty(), "HMI never revokes emergency stop to move");
        }
        protocol_frames();''')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "dist" / "device-tools-20261125")
    args = parser.parse_args()
    rev = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(args.source), "status", "--porcelain"], text=True).strip()
    if rev != REVISION or dirty:
        raise RuntimeError("Supply a clean checkout at " + REVISION)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "sdk-source"
    shutil.copytree(args.source, target, ignore=shutil.ignore_patterns(".git"))
    patch(target)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", str(target), "--wheel-dir", str(args.output)], check=True)
    files = {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(target.rglob("*")) if p.is_file()}
    (args.output / "sdk-source-manifest.json").write_text(json.dumps({"upstream": REVISION, "hmi_safety_api": 1, "files": files}, indent=2))
    wheels = list(args.output.glob("rphand-*.whl"))
    (args.output / "SDK-SHA256SUMS").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in wheels))


if __name__ == "__main__":
    main()
