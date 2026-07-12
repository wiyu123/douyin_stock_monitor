"""
已推送跟踪模块 — SQLite 数据库，视频 ID 主键去重。
直接复用 twitter_stock_monitor/tracker.py 模式。
"""

import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "videos.db")


class VideoTracker:
    """已通知追踪器 — SQLite 持久化，视频 ID 主键，不重复发送。"""

    def __init__(self):
        self._conn = sqlite3.connect(DB_FILE)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS videos ("
            "  video_id TEXT PRIMARY KEY,"
            "  processed_at TEXT NOT NULL,"
            "  summary TEXT"
            ")"
        )
        self._conn.commit()
        cnt = self._conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        logger.info(f"SQLite 已加载 {cnt} 条已处理视频")

    def is_processed(self, video_id: str) -> bool:
        """检查视频是否已处理过"""
        row = self._conn.execute(
            "SELECT 1 FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return row is not None

    def mark_done(self, video_id: str, summary: str = ""):
        """标记视频已处理"""
        ts = datetime.now().isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO videos (video_id, processed_at, summary) VALUES (?, ?, ?)",
            (video_id, ts, summary[:500] if summary else ""),
        )
        self._conn.commit()

        # 保留最近 1000 条记录
        cnt = self._conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        if cnt > 1000:
            self._conn.execute(
                "DELETE FROM videos WHERE video_id IN ("
                "  SELECT video_id FROM videos ORDER BY processed_at ASC LIMIT ?"
                ")",
                (cnt - 1000,),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()
