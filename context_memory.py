"""
上下文记忆模块 — 基于 SQLite 的记录存储与模糊检索

支持会话隔离、FIFO 淘汰、时间过期、摘要注入、模糊匹配。
"""

import sqlite3
import os
import time
import re
import logging
from typing import Optional, List, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger("AiDraw.ContextMemory")


class ContextMemory:
    """管理用户会话中生成/识别的记录"""

    def __init__(self, db_path: str, max_entries: int = 20,
                 max_age_hours: int = 24):
        self.db_path = db_path
        self.max_entries = max_entries
        self.max_age_hours = max_age_hours
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS context_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    record_type TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    result_text TEXT NOT NULL DEFAULT '',
                    result_images TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cm_session
                ON context_memory(session_id, created_at)
            """)
            conn.commit()

    def _get_session_id(self, user_id: str, group_id: str = "") -> str:
        return f"{user_id}:{group_id}" if group_id else user_id

    def add(self, user_id: str, group_id: str, record_type: str,
            prompt: str, result_text: str = "",
            result_images: list = None) -> int:
        """添加一条记录，自动清理超限/过期数据。返回 seq 编号。"""
        session_id = self._get_session_id(user_id, group_id)

        with sqlite3.connect(self.db_path) as conn:
            # 获取下一个 seq
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM context_memory WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            seq = row[0]

            images_json = json.dumps(result_images or [])
            now = time.time()

            conn.execute(
                """INSERT INTO context_memory
                   (session_id, seq, record_type, prompt, result_text,
                    result_images, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, seq, record_type, prompt, result_text,
                 images_json, now)
            )

            # 清理过期
            cutoff = now - self.max_age_hours * 3600
            conn.execute(
                "DELETE FROM context_memory WHERE session_id = ? AND created_at < ?",
                (session_id, cutoff)
            )

            # 清理超量（保留最新的 max_entries 条）
            row = conn.execute(
                "SELECT COUNT(*) FROM context_memory WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if row[0] > self.max_entries:
                excess = row[0] - self.max_entries
                conn.execute(
                    """DELETE FROM context_memory WHERE id IN (
                        SELECT id FROM context_memory
                        WHERE session_id = ?
                        ORDER BY created_at ASC LIMIT ?
                    )""",
                    (session_id, excess)
                )

            conn.commit()

        logger.info(f"ContextMemory: added #{seq} [{record_type}] for {session_id}")
        return seq

    def get_summary(self, user_id: str, group_id: str) -> str:
        """生成会话摘要，用于注入 LLM 上下文。"""
        session_id = self._get_session_id(user_id, group_id)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """SELECT seq, record_type, prompt, result_text, created_at
                   FROM context_memory
                   WHERE session_id = ?
                   ORDER BY seq ASC""",
                (session_id,)
            ).fetchall()

        if not rows:
            return ""

        cutoff = time.time() - self.max_age_hours * 3600
        lines = ["[上下文记忆 - 本会话已生成/识别 {} 条记录]".format(len(rows))]

        for seq, rtype, prompt, result_text, created_at in rows:
            if created_at < cutoff:
                continue
            ago = self._format_ago(time.time() - created_at)
            icon = "" if rtype == "draw" else ""
            label = "生成" if rtype == "draw" else "识别"

            if rtype == "draw":
                desc = prompt[:80]
                lines.append(f"#{seq} {icon} {label}: \"{desc}\"  ({ago})")
            else:
                desc = result_text[:120] if result_text else prompt[:80]
                lines.append(f"#{seq} {icon} {label}结果: \"{desc}\"  ({ago})")

        return "\n".join(lines)

    def search(self, user_id: str, group_id: str,
               query: str) -> Optional[dict]:
        """模糊搜索记录。匹配 seq 编号或 prompt/result 内容。"""
        session_id = self._get_session_id(user_id, group_id)

        m = re.search(r'#(\d+)', query)
        seq_num = int(m.group(1)) if m else None

        with sqlite3.connect(self.db_path) as conn:
            if seq_num is not None:
                row = conn.execute(
                    """SELECT seq, record_type, prompt, result_text,
                              result_images, created_at
                       FROM context_memory
                       WHERE session_id = ? AND seq = ?""",
                    (session_id, seq_num)
                ).fetchone()
                if row:
                    return self._row_to_dict(row)

            # 关键词模糊匹配
            for keyword in self._extract_keywords(query):
                row = conn.execute(
                    """SELECT seq, record_type, prompt, result_text,
                              result_images, created_at
                       FROM context_memory
                       WHERE session_id = ?
                         AND (prompt LIKE ? OR result_text LIKE ?)
                       ORDER BY created_at DESC LIMIT 1""",
                    (session_id, f"%{keyword}%", f"%{keyword}%")
                ).fetchone()
                if row:
                    return self._row_to_dict(row)

        return None

    def get_record(self, user_id: str, group_id: str,
                   seq: int) -> Optional[dict]:
        """按 seq 编号精确获取记录。"""
        session_id = self._get_session_id(user_id, group_id)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT seq, record_type, prompt, result_text,
                          result_images, created_at
                   FROM context_memory
                   WHERE session_id = ? AND seq = ?""",
                (session_id, seq)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def _row_to_dict(self, row) -> dict:
        return {
            "seq": row[0],
            "record_type": row[1],
            "prompt": row[2],
            "result_text": row[3],
            "result_images": json.loads(row[4]),
            "created_at": row[5],
        }

    def _extract_keywords(self, query: str) -> List[str]:
        """从查询中提取有意义的搜索词。"""
        # 去掉常见口语词和标点
        noise = {"的", "是", "我", "你", "他", "她", "它", "们", "了", "吗",
                 "呢", "吧", "啊", "呀", "在", "有", "和", "与", "这", "那",
                 "一个", "一张", "那张", "那张图", "图", "图片", "里面", "什么"}
        words = re.findall(r'[\u4e00-\u9fff\w]+', query)
        keywords = [w for w in words if len(w) >= 2 and w not in noise]
        return keywords[:5]

    def _format_ago(self, seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}秒前"
        if seconds < 3600:
            return f"{int(seconds / 60)}分钟前"
        if seconds < 86400:
            return f"{int(seconds / 3600)}小时前"
        return f"{int(seconds / 86400)}天前"


