"""串口持续 syslog 穿插在密文、密码提示和 ASH 横幅内部时仍可识别完整响应。"""

import base64

import pytest
from camera_logs.collection.psh_dialogue import extract_challenge
from camera_logs.collection.psh_response import PshResponse

SOURCE = base64.b64encode(bytes(range(256)) + b"test")
LOG = b"Sep  8 14:27:09 dsp.crit bscdsp: periodic output\r\n"


@pytest.mark.parametrize("chunk_size", [1, 7, 47, 65536])
def test_syslog_in_middle_of_control_tokens_is_removed_only_from_observer(chunk_size):
    raw = (b"# debug\r\n" + SOURCE[:107] + LOG + SOURCE[107:] + b"\r\nPass" + LOG
           + b"word: " + LOG + b"BusyBox built-in shell (a" + LOG + b"sh)\r\n# ")
    expected = b"# debug\r\n" + SOURCE + b"\r\nPassword: BusyBox built-in shell (ash)\r\n# "
    observer = PshResponse()
    for offset in range(0, len(raw), chunk_size):
        observer.feed(raw[offset:offset+chunk_size])
    assert observer.data == expected
    assert extract_challenge(observer.data) == SOURCE.decode()
    assert LOG in raw


def test_large_background_print_volume_does_not_evict_control_response():
    observer = PshResponse()
    observer.feed(SOURCE + b"\r\n")
    for _ in range(1000):
        observer.feed(LOG * 10)
    observer.feed(b"Password: ")
    assert extract_challenge(observer.data) == SOURCE.decode()
    assert len(observer.data) < 1024


def test_unknown_text_is_not_silently_removed_or_guessed_as_ciphertext():
    observer = PshResponse()
    observer.feed(SOURCE[:100] + b"unrecognized log\n" + SOURCE[100:] + b"\nPassword: ")
    assert b"unrecognized log" in observer.data
    assert extract_challenge(observer.data) is None
