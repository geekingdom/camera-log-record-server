"""解析 HTTP API 时间参数，统一无时区输入的上海墙钟语义。"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_api_datetime(value: str) -> datetime:
    """解析 ISO 8601 时间；无偏移量时按上海时间解释并返回 UTC 绝对时刻。"""
    if "T" not in value:
        raise ValueError("时间必须包含日期和时间")
    try:
        parsed = datetime.fromisoformat(value)
    except OverflowError as error:
        raise ValueError("时间超出支持范围") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    try:
        return parsed.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("时间超出支持范围") from error
