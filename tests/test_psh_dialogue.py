"""PSH 密文提取与流式模式观察使用合成数据，真实密文和口令不得入库。"""

import base64

import pytest
from camera_logs.collection.psh_dialogue import PshDialogue, extract_challenge

SOURCE = base64.b64encode(bytes(range(256)) + b"test").decode()


@pytest.mark.parametrize("marker", ["", "enc_string: "])
def test_extract_exact_source_after_qr_without_decoding_payload(marker):
    raw = ("# debug\r\n" + "\u2588" * 30 + "\r\n" + marker + SOURCE + "\r\nPassword: ").encode()
    assert extract_challenge(raw) == SOURCE
    # 网络切片不会使缺失提示符的半个密文被提前提交。
    for end in range(raw.index(b"Password:")):
        assert extract_challenge(raw[:end]) is None


def test_extract_ignores_ansi_qr_and_normal_log_lines():
    raw = b"# debug\r\nnot-a-challenge\r\n\x1b[32m" + SOURCE.encode() + b"\x1b[0m\r\nPassword: periodic log\n"
    assert extract_challenge(raw) == SOURCE
    assert extract_challenge(b"# debug\nhelp\nPassword: ") is None
    assert extract_challenge(b"x" * 33 + b"\nPassword: ") is None


def test_mode_observation_survives_packet_splits_and_is_session_local():
    first, second = PshDialogue({}, None), PshDialogue({}, None)
    first.feed(b"BusyBox built-in shell (a")
    assert first.mode == "UNKNOWN"
    first.feed(b"sh)\r\n# ")
    assert first.mode == "ASH" and second.mode == "UNKNOWN"
    first.feed(b"\r\nBusyBox Protect Shell (psh)\r\n# ")
    assert first.mode == "PSH"


async def test_serial_password_typing_stops_before_sending_more_bytes(tmp_path):
    from camera_logs.collection.collector import Collector
    from camera_logs.collection.psh_dialogue import PshSwitchError

    sent = []
    collector = Collector({"id": "serial", "protocol": "TELNET_SERIAL", "pshSerialCharacterInterval": .001},
                          tmp_path, connection_factory=lambda _: None)

    class Connection:
        async def write(self, data):
            sent.append(data)
            collector._accepting_commands = False

    collector._connection = Connection()
    with pytest.raises(PshSwitchError):
        await collector._write_debug(b"synthetic-password\r")
    assert sent == [b"s"]


async def test_new_dialogue_does_not_inherit_previous_run_debug_failure():
    """新会话不读取旧运行失败标记，仍可独立执行一次完整 debug 握手。"""
    dialogue = PshDialogue({}, lambda *_args: "synthetic-password")
    writes = []

    async def write(data):
        writes.append(data)
        if data == b"debug\n":
            dialogue.feed(("\n" + SOURCE + "\nPassword:").encode())
        elif data == b"synthetic-password\n":
            dialogue.feed(b"BusyBox built-in shell (ash)\n# ")

    dialogue.mode = "PSH"
    await dialogue.ensure_ash(write, "\n", .1)
    assert writes == [b"debug\n", b"synthetic-password\n"]
