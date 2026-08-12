"""单实例进程锁：保证同一时间只有一轮挂机在跑。

两轮并行是有破坏性的，不只是"乱一点"：
  - 登录/选角色/在荼蘼注册脚本整套流程靠 PyAutoGUI 控制全局唯一的鼠标，两个进程会互相插指针；
  - `tu_mi_queue/ui_lock.py` 的界面锁是进程内的 RLock，跨进程根本不生效；
  - 空闲行是靠截图扫出来的，另一个进程刚点完"开始"、荼蘼那行还没变绿的窗口期里会被扫成空闲，
    于是往正在跑的行上塞新角色；
  - 每日状态 JSON 是整份覆盖写的，两个进程会互相抹掉对方的记录；
  - 最致命的是新进程启动时会调 close_clx_windows_and_wait() 无差别关掉所有一梦江湖窗口，
    正在挂机的角色会被一起杀掉。

所以宁可拒绝启动也不能放行。锁文件里记 pid + 进程启动时间：只看 pid 存不存在是不够的，
系统会把退出进程的 pid 回收后分配给别的进程，得比对启动时间才能确认"还是当初那个进程"。
"""

import datetime
import json
import logging
import os

import psutil

logger = logging.getLogger(__name__)

# 比对进程启动时间时允许的误差（秒）。psutil 读到的 create_time 精度和写入时可能有微小出入，
# 卡得太死会把自己的锁误判成残留锁
_CREATE_TIME_TOLERANCE_SEC = 1.0


class AlreadyRunningError(RuntimeError):
    """已经有一轮挂机在跑，本次不该启动"""

    def __init__(self, holder):
        self.pid = holder.get("pid")
        self.started_at = holder.get("started_at", "未知时间")
        super().__init__(f"已有挂机进程在运行（pid={self.pid}，启动于{self.started_at}）")


class ProcessLock:
    """基于锁文件的单实例锁。acquire 失败抛 AlreadyRunningError，release 只清理自己写的那份。"""

    def __init__(self, lock_path):
        self.lock_path = lock_path
        self._acquired = False

    def read_holder(self):
        """返回当前持锁进程的信息 dict（pid/started_at），无人持锁时返回 None。

        锁文件不存在、内容坏了、或者记录的进程已经不在了，都算无人持锁——
        崩溃/断电时 release 是来不及执行的，残留锁文件必须能自动失效，
        否则一次异常退出就会让程序从此永远拒绝启动。
        """
        if not os.path.exists(self.lock_path):
            return None
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                holder = json.load(f)
            pid = int(holder["pid"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.warning("进程锁文件 %s 无法解析（%s），按无人持锁处理", self.lock_path, e)
            return None

        try:
            proc = psutil.Process(pid)
            create_time = holder.get("create_time")
            if create_time and abs(proc.create_time() - float(create_time)) > _CREATE_TIME_TOLERANCE_SEC:
                # pid 对上了但启动时间对不上：原进程早退出了，这个 pid 被系统回收给了别的进程
                return None
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
            return None
        return holder

    def acquire(self):
        holder = self.read_holder()
        if holder:
            raise AlreadyRunningError(holder)
        if os.path.exists(self.lock_path):
            logger.warning("发现上次运行残留的进程锁 %s（记录的进程已不存在），覆盖后继续", self.lock_path)

        os.makedirs(os.path.dirname(os.path.abspath(self.lock_path)), exist_ok=True)
        proc = psutil.Process(os.getpid())
        payload = {
            "pid": proc.pid,
            "create_time": proc.create_time(),
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        tmp_path = self.lock_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.lock_path)
        self._acquired = True
        logger.info("已获取单实例进程锁（pid=%s）: %s", proc.pid, self.lock_path)

    def release(self):
        """删掉自己写的锁文件。没拿到过锁就什么都不做，避免把别人的锁误删了。"""
        if not self._acquired:
            return
        self._acquired = False
        try:
            holder = self.read_holder()
            if holder and holder.get("pid") != os.getpid():
                logger.warning("进程锁 %s 现在属于pid=%s，不是本进程，跳过清理",
                               self.lock_path, holder.get("pid"))
                return
            os.remove(self.lock_path)
            logger.info("已释放单实例进程锁: %s", self.lock_path)
        except OSError as e:
            logger.warning("释放进程锁 %s 失败: %s", self.lock_path, e)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
