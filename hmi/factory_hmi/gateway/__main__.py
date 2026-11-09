"""Start the RP1 factory aging-test control gateway."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.lease import DEFAULT_LEASE_TIMEOUT_S, validate_lease_timeout


class PreparedShutdownServer(uvicorn.Server):
    """Signals request preparation; repeated Ctrl-C never escalates to kill.

    Uvicorn's lifespan.shutdown.failed is only a log, not an exit veto. Keep
    its should_exit false until our resource shutdown actually succeeds.
    """
    def __init__(self, config, coordinator):
        super().__init__(config)
        self.coordinator = coordinator
        self._prepare_requested = False
        self._prepare_waiting = False

    def handle_exit(self, sig, frame):
        # Signal handlers only set a flag; no locks or hardware calls here.
        self._prepare_requested = True

    async def on_tick(self, counter: int) -> bool:
        if self._prepare_requested:
            self._prepare_requested = False
            self.coordinator.request()
            self._prepare_waiting = True
        if self._prepare_waiting:
            state = self.coordinator.snapshot()
            if state["ready"]:
                self.should_exit = True
            elif state["state"] == "blocked":
                self._prepare_waiting = False
                logging.getLogger(__name__).error(
                    "Exit refused: %s. Resolve the blocker, then request shutdown again.", state["detail"])
        return await super().on_tick(counter)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lease-timeout", type=validate_lease_timeout,
                        default=os.environ.get("RP1_FACTORY_LEASE_TIMEOUT_S", str(DEFAULT_LEASE_TIMEOUT_S)))
    parser.add_argument("--status-rate", type=float, default=10.0)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--log-level", default="info")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be in [1, 65535]")
    if args.status_rate <= 0.0:
        raise SystemExit("--status-rate must be positive")
    app = create_app(
        lease_timeout_s=float(args.lease_timeout),
        status_rate_hz=float(args.status_rate),
        data_root=args.data_root,
    )
    server = PreparedShutdownServer(uvicorn.Config(
        app, host=args.host, port=args.port, log_level=args.log_level,
    ), app.state.runtime.shutdown)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
