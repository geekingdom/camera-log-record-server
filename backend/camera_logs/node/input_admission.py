"""按管理员配置的日志输入吞吐限制新任务，既有连接不因达到阈值而中断。"""

import math
import time

MIB = 1024 * 1024
DEFAULT_INPUT_RATE_LIMIT_MIB = 50
MAX_INPUT_RATE_LIMIT_MIB = 100_000


class InputRateMeter:
    """逐运行累计增量，任务退出不会抵消其它任务流量，引用最多保留一个周期。"""

    def __init__(self):
        self.previous = {}
        self.tick = time.monotonic()

    def sample(self, runtimes, tick=None):
        """包含上周期退出运行的尾部字节，替换运行按新计数器独立计算。"""
        current = {id(runtime): runtime for runtime in runtimes}
        combined = {key: runtime for key, (runtime, _) in self.previous.items()} | current
        values = {key: max(0, runtime.input_bytes) for key, runtime in combined.items()}
        delta = sum(max(0, value - self.previous.get(key, (None, 0))[1]) for key, value in values.items())
        timestamp = time.monotonic() if tick is None else tick
        rate = delta / max(.01, timestamp - self.tick)
        self.previous = {key: (runtime, values[key]) for key, runtime in current.items()}
        self.tick = timestamp
        return rate


def input_rate_limit(config):
    """旧登记和未登记节点保持不启用，新增登记由请求模型写入默认阈值。"""
    value = config.get("inputRateLimitMiB", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def input_rate_blocked(node, config=None):
    """等于阈值即拒绝新任务；启用限制但测量缺失时不能把未知吞吐当成零。"""
    limit = input_rate_limit(node if config is None else config)
    if not limit:
        return False
    rate = node.get("inputBytesPerSecond")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(rate) or rate < 0:
        return True
    return rate >= limit * MIB
