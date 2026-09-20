"""秒级慢写样本需要可排障的精度，不能把1.1秒夸大到2.048秒。"""

from camera_logs.common.write_metrics import WriteLatency


def test_seconds_histogram_retains_conservative_hundred_millisecond_precision():
    """管理员可配置阈值内最多向上取整100毫秒，边界不得低估真实延迟。"""
    for delay in (1001, 1100, 1101, 1999, 2000, 2001, 59999, 60000):
        upper = WriteLatency._upper_bound(delay)
        assert delay <= upper < delay + 100


def test_long_stall_still_uses_bounded_geometric_buckets():
    """超过最大可配60秒阈值后继续保守表示，不为无限长故障创建线性桶。"""
    assert WriteLatency._upper_bound(60001) == 65536
    assert WriteLatency._upper_bound(100000) == 131072
