# -*- coding: utf-8 -*-
"""data.json 共享写锁：fetch_data.py 与 fetch_spot.py 共用，防止并发「读-改-写」丢失更新。

设计要点：
- 锁文件 data.lock，O_CREAT|O_EXCL 原子创建（无第三方依赖）
- 只保护「读-合并-写回」临界区（毫秒级），网络抓取在锁外 → 并发时几乎不等待
- 崩溃残留：锁文件 mtime 超过 STALE_SECONDS 视为残留，强制抢占
"""
import errno
import os
import time

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.lock")
STALE_SECONDS = 300      # 5 分钟无活动视为崩溃残留
RETRY_TIMES = 10         # 每 2 秒重试一次，最长等 ~20 秒
RETRY_INTERVAL = 2.0


class DataLockTimeoutError(RuntimeError):
    pass


class DataLock(object):
    def __init__(self, path=LOCK_PATH):
        self.path = path
        self.acquired = False

    def acquire(self):
        for i in range(RETRY_TIMES):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                os.close(fd)
                self.acquired = True
                return True
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                # 锁存在：检查是否为崩溃残留
                try:
                    if time.time() - os.path.getmtime(self.path) > STALE_SECONDS:
                        try:
                            os.remove(self.path)
                            continue  # 抢占后重试创建
                        except OSError:
                            pass
                except OSError:
                    pass
                if i == RETRY_TIMES - 1:
                    return False
                time.sleep(RETRY_INTERVAL)
        return False

    def release(self):
        if self.acquired:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise DataLockTimeoutError(
                "data.json 正被其他更新进程占用，等待 %d 秒后仍未获得写锁，请稍后重试" % (RETRY_TIMES * RETRY_INTERVAL))
        return self

    def __exit__(self, *exc):
        self.release()
