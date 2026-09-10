"""设备目录身份清洗契约，防止型号特殊字符破坏日志路径。"""

from camera_logs.tasks.resource_binding import _safe_component


def test_safe_component_removes_path_and_control_characters():
    """斜杠、冒号和控制字符必须被替换且结果保持可读。"""
    value = _safe_component("iDS/2CD:VX3\n-IS")
    assert value == "iDS_2CD_VX3_IS"


def test_safe_component_bounds_long_identity():
    """过长设备型号不能导致无限增长的目录路径。"""
    assert len(_safe_component("x" * 1000)) == 96
