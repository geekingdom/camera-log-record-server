"""多路并行传输测试：确认不同任务的归档不会发生数据串路。"""

from __future__ import annotations

import asyncio
import tarfile

from camera_logs.collection.collector import Collector


class Stream:
    def __init__(self) -> None:
        self.values: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def read(self, _size=65536):
        return (await self.values.get()) or b""

    async def write(self, _data):
        return None

    async def close(self):
        await self.values.put(None)


def test_parallel_streams_archive_each_route_without_cross_contamination(tmp_path):
    async def scenario():
        streams = [Stream(), Stream()]
        archives = [[], []]
        collectors = [
            Collector(
                {"id": f"task-{index}", "runId": f"run-{index}", "initialCommands": []},
                tmp_path,
                connection_factory=lambda _task, stream=stream: stream,
                on_archive=lambda archive, index=index: archives[index].append(archive),
            )
            for index, stream in enumerate(streams)
        ]
        await asyncio.gather(*(collector.start() for collector in collectors))
        await streams[0].values.put(b"a-1\na-2\n")
        await streams[1].values.put(b"b-1\nb-2\n")
        await asyncio.gather(*(stream.close() for stream in streams))
        await asyncio.gather(*(collector.wait_closed() for collector in collectors))
        return archives

    archives = asyncio.run(scenario())
    contents = []
    for route in archives:
        with tarfile.open(route[0].path, "r:gz") as bundle:
            log = next(member for member in bundle.getmembers() if member.name.endswith(".log"))
            contents.append(bundle.extractfile(log).read())
    assert b"a-" not in contents[1] and b"b-" not in contents[0]
    assert b"a-1\n" in contents[0] and b"a-2\n" in contents[0]
    assert b"b-1\n" in contents[1] and b"b-2\n" in contents[1]
