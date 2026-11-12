"""Replay raw SocketCAN frames through the production OTA adapter, without CAN."""
import threading

import pytest

from factory_hmi.core.motor_ota_transport import protocol, write_firmware


@pytest.mark.parametrize('selfcheck', ['ff0201', 'ff020200bcf53ec2'])
@pytest.mark.parametrize('wire_format', ['classic', 'fd', 'mixed'])
def test_ota_accepts_classic_and_fd_extended_replies(monkeypatch, tmp_path, wire_format, selfcheck):
    p = protocol()
    sockets = []

    class Bus:
        inject_noise = True
        def __init__(self, *args):
            self.queue = []
            self.commands = []
            self.receiver = p.IsoTpReceiver()
            self.closed = False
            self.replies = 0
            sockets.append(self)

        def setsockopt(self, *args): pass
        def bind(self, interface): assert interface == ('fixture-can',)
        def setblocking(self, value): assert value is False
        def close(self): self.closed = True

        def enqueue(self, payload):
            self.replies += 1
            classic = wire_format == 'classic' or wire_format == 'mixed' and self.replies % 2
            # Wrong target / RTR / error / standard frames must never acknowledge.
            for wrong in ((0x80001004, 0xC0001002, 0xA0001002, 0x1002) if self.inject_noise else ()):
                self.queue.append(p._CAN_FRAME.pack(wrong, 8, p.encode_sf(b'\xfe\x31')))
            self.queue.append(b'bad')
            self.queue.append(p._CAN_FRAME.pack(0x80001002, 7, payload))
            self.queue.append(p._CAN_FRAME.pack(0x80001002, 8, payload) if classic
                              else p._CANFD_FRAME.pack(0x80001002, 8, 1, payload))

        def send(self, raw):
            can_id, length, flags, data = p._CANFD_FRAME.unpack(raw)
            assert can_id == 0x80001001 and length == 8 and flags == 1
            command, flow = self.receiver.feed(data[:length])
            if flow:
                self.enqueue(flow)
            if command:
                self.commands.append(command)
                response = (b'\xff\x00\x00\x08' if command[0] == p.CMD_CONNECT
                            else bytes.fromhex(selfcheck) if command[0] == p.CMD_USER else b'\xff')
                first, following = p.encode_message(response)
                for part in first + following:self.enqueue(part.ljust(8, b"\x00"))
            return len(raw)

        def recv(self, limit):
            assert limit == 72
            if self.queue:
                return self.queue.pop(0)
            raise BlockingIOError()

    monkeypatch.setattr(p.socket, 'socket', Bus)
    target = {'interface': 'fixture-can', 'motor_id': 1, 'ota_request_id': 0x1001,
              'ota_response_id': 0x1002}
    progress = []
    receipt = write_firmware(target, b'x' * 600, threading.Event(), lambda *args: progress.append(args))
    assert receipt['bytes_written'] == 600 and receipt['selfcheck_response'] == selfcheck
    assert progress == [(256, 600), (512, 600), (600, 600)]
    assert len(sockets) == 1 and sockets[0].closed
    codes = [c[0] for c in sockets[0].commands]
    assert codes.count(p.CMD_CONNECT) == 1 and codes[-1] == p.CMD_PROGRAM_RESET
    # Compare every reconstructed wire command with the very same vendor API
    # used by deploy_motorlro_firmware.py in the successful ID 2 run.
    Bus.inject_noise = False
    firmware = tmp_path / 'synthetic-never-flash.bin'
    firmware.write_bytes(b'x' * 600)
    with p.LinuxFlasher('fixture-can') as flasher:
        flasher.flash(firmware, 0x1001)
    assert sockets[1].closed
    assert sockets[0].commands == sockets[1].commands


def test_connect_timeout_reports_stage_without_erase_or_job_retry(monkeypatch):
    p = protocol()
    commands = []

    class SilentAdapter:
        def __init__(self, interface): self.closed = False
        def open(self): pass
        def close(self): self.closed = True
        def send_frame(self, can_id, data): commands.append(data)
        def recv_frame(self): return None

    with pytest.raises(p.ConnectError) as caught:
        write_firmware({'interface': 'fixture-can', 'motor_id': 1, 'ota_request_id': 0x1001,
                        'ota_response_id': 0x1002}, b'x', threading.Event(), lambda *args: None,
                       adapter_factory=SilentAdapter)
    assert len(commands) == p.FlashTimeouts().connect_retries
    assert all(frame == p.encode_sf(p.build_connect()) for frame in commands)
    assert '升级握手' in str(caught.value) and '未发送擦除' in str(caught.value)
    assert caught.value.ota_diagnostics['stage'] == 'connect'
    assert caught.value.ota_diagnostics['bytes_written'] == 0
