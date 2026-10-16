"""节点本地SQLite检查点只存读取位置和源身份；正文始终可从原始文件重建。"""

import json
import sqlite3
import time


class Checkpoints:
    """单进程使用事务提交批次位置，重启后不依赖内存或Worker存活。"""
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS positions (revision TEXT PRIMARY KEY, state TEXT NOT NULL)')
        self.db.commit()

    def get(self, revision):
        """不存在的源版本从零开始；旧版本永远不能替代新文件水位。"""
        row = self.db.execute('SELECT state FROM positions WHERE revision=?', (revision,)).fetchone()
        return json.loads(row[0]) if row else {"offset": 0, "chunks": 0}

    def save(self, revision, state):
        """仅由ES全部批次项确认后调用；事务失败允许按稳定ID重放。"""
        state = state | {"updatedAt": time.time()}
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO positions VALUES (?, ?)', (revision, json.dumps(state)))

    def prune(self, days):
        """检查点不含正文，删除过期元数据最多导致幂等重放，不影响日志完整性。"""
        with self.db:
            self.db.execute("DELETE FROM positions WHERE json_extract(state, '$.updatedAt') < ?",
                            (time.time() - (days + 1) * 86400,))

    def close(self):
        """释放持久检查点句柄。"""
        self.db.close()
