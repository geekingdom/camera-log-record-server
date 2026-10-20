"""实时日志 WebSocket 帧的公开结构回归测试。"""

import pytest
from camera_logs.common.log_frames import DataFrame, GapFrame, StatusFrame, SubscribeFrame
from pydantic import ValidationError


def test_data_frame_has_strict_wire_shape_and_byte_range_metadata():
    """数据帧必须拒绝额外字段，并公开半开范围、原始字节和不透明游标说明。"""
    frame = DataFrame(
        type="data", fileId="file-1", sessionId="session-1", runId="run-1", offset=3,
        endOffset=7, data="bG9nCg==", size=4, cursor="run-1:4",
    )

    assert frame.model_dump(mode="json")["type"] == "data"
    schema = DataFrame.model_json_schema()
    assert "半开字节区间" in schema["properties"]["offset"]["description"]
    assert "原始字节" in schema["properties"]["size"]["description"]
    assert "不透明" in schema["properties"]["cursor"]["description"]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DataFrame.model_validate({**frame.model_dump(), "unexpected": True})


@pytest.mark.parametrize("frame", [
    {"type": "gap", "message": "实时缓冲已过期，请通过小时归档补读"},
    {"type": "status", "status": "PENDING"},
])
def test_non_data_frames_are_strictly_discriminated(frame):
    """gap/status 也以 Literal 固定帧类型，不能接受数据帧或未知额外字段。"""
    model = GapFrame if frame["type"] == "gap" else StatusFrame
    assert model.model_validate(frame).model_dump(mode="json") == frame
    with pytest.raises(ValidationError):
        model.model_validate({**frame, "type": "data"})


def test_subscribe_frame_preserves_cookie_and_empty_cursor_defaults_but_forbids_unknown_input():
    """订阅首帧允许省略令牌走 Cookie，并把未提供游标规范为当前订阅起点。"""
    subscribe = SubscribeFrame.model_validate({})

    assert subscribe.token is None
    assert subscribe.cursor == ""
    assert SubscribeFrame.model_validate({"token": "service-token", "cursor": "run:8"}).cursor == "run:8"
    assert SubscribeFrame.model_validate({"token": None, "cursor": None}).cursor is None
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SubscribeFrame.model_validate({"cursor": "", "unexpected": True})
