"""闲鱼助手 - 跨线程任务队列

Playwright 线程亲和：浏览器操作只能在属主线程（监听线程）执行。
其他线程（如 FastAPI）通过本队列把任务推给监听线程执行。
"""
import re
import threading
import time
from collections import deque


class TaskQueue:
    def __init__(self):
        self._q = deque()
        self._lock = threading.Lock()

    def push(self, fn):
        with self._lock:
            self._q.append(fn)

    def drain(self):
        """取出并清空当前所有任务；由属主线程调用。"""
        with self._lock:
            items = list(self._q)
            self._q.clear()
        return items

    def size(self):
        with self._lock:
            return len(self._q)


task_queue = TaskQueue()

# ---------- 最近自己发送的消息（echo 过滤） ----------
# 收到的推送可能把"自己刚发出的消息"回显（WS 推送/DOM 会话列表预览），
# 若不识别会把自家消息当买家消息：重复入库、误触发消息音/自动回复循环。
# 发送文本含换行，而回显文本会把换行折叠为空格 → 不能精确匹配，需归一化+时间窗。
OWN_SENT_MAX = 200
OWN_SENT_WINDOW_SEC = 90   # WS 回显时间窗（秒）
_own_sent_log = []         # [(ts, norm_text)] FIFO


def _norm_sent(s):
    return re.sub(r"\s+", "", s or "")[:200].lower()


def note_own_sent(text):
    """登记一条自己发送的消息（发送成功后调用）"""
    n = _norm_sent(text)
    if not n:
        return
    with task_queue._lock:
        _own_sent_log.append((time.time(), n))
        if len(_own_sent_log) > OWN_SENT_MAX:
            _own_sent_log.pop(0)   # FIFO 丢最旧（不得整体清空，否则漏过滤 echo）


def is_own_recent(text, window_sec=OWN_SENT_WINDOW_SEC):
    """收到的消息是否是自己最近发出的回显（容忍换行/空白差异）。
    window_sec=None/0 表示不限时间（DOM 会话列表预览长期存在，用永久窗）。"""
    n = _norm_sent(text)
    if not n:
        return False
    now = time.time()
    with task_queue._lock:
        log = list(_own_sent_log)
    for ts, on in reversed(log):
        if window_sec and now - ts > window_sec:
            break   # log 按时间升序，越靠后越新；超过窗口即可停
        if n == on:
            return True
        # 前缀宽松匹配（回显可能被截断/压缩），长度太短不判，避免误伤
        if len(on) >= 14 and on[:14] in n[:60]:
            return True
        if len(n) >= 14 and n[:14] in on[:60]:
            return True
    return False
