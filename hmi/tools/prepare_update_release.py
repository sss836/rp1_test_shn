#!/usr/bin/env python3
"""Prepare a release offline; never generate, publish or deploy a signing key."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import sys

prepare_release = runpy.run_path(str(Path(__file__).resolve().parents[1] / "factory_hmi/updates.py"))["prepare_release"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--private-key", required=True, type=Path,
                        help="维护人员已有的 Ed25519 私钥；不生成或复制私钥")
    parser.add_argument("--os-id", required=True)
    parser.add_argument("--os-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--notes", default="")
    parser.add_argument("--release-id")
    args = parser.parse_args()
    try:
        release = prepare_release(args.package, private_key_path=args.private_key,
                                  os_id=args.os_id, os_version=args.os_version,
                                  output=args.output, notes=args.notes,
                                  release_id=args.release_id)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"release_id": release["manifest"]["release_id"],
                      "release_path": str(args.output.resolve()),
                      "published": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
