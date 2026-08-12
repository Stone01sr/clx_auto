import datetime
import threading
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    """角色任务在队列中的状态"""
    QUEUED = "排队中"
    PENDING = "待运行"
    RUNNING = "运行中"
    DONE = "运行完成"
    FAILED = "失败"


# 荼蘼软件本身展示的原始状态文案，用于判断PENDING->RUNNING、RUNNING->DONE的切换点
TU_MI_NOT_STARTED = "未启动"
TU_MI_INITIALIZING = "初始化"


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclass
class HistoryEntry:
    """一次状态变迁记录"""
    timestamp: str
    from_status: str
    to_status: str
    tu_mi_raw_status: str = ""
    screenshot_path: str = ""

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "tu_mi_raw_status": self.tu_mi_raw_status,
            "screenshot_path": self.screenshot_path,
        }

    @staticmethod
    def from_dict(d):
        return HistoryEntry(
            timestamp=d["timestamp"],
            from_status=d["from_status"],
            to_status=d["to_status"],
            tu_mi_raw_status=d.get("tu_mi_raw_status", ""),
            screenshot_path=d.get("screenshot_path", ""),
        )


@dataclass
class RoleTask:
    """单个角色当天的队列任务"""
    role_name: str
    status: str = TaskStatus.QUEUED.value
    tu_mi_raw_status: str = ""
    row_index: int = -1              # 该任务当前占用的荼蘼行号，未分配为-1
    window_pid: int = None           # 对应的游戏客户端进程pid，便于精确关窗口
    pending_started_at: str = None   # 进入待运行状态的时间戳，用于30分钟超时判断
    retry_count: int = 0             # 程序自动重试次数（超时/异常退出触发），达上限判失败
    rerun_count: int = 0             # 人工重跑次数，和自动重试分开计数，方便在查看页面上区分
    history: list = field(default_factory=list)

    def transition(self, new_status: TaskStatus, tu_mi_raw_status: str = "", screenshot_path: str = ""):
        """记录一次状态迁移并更新当前状态"""
        entry = HistoryEntry(
            timestamp=now_iso(),
            from_status=self.status,
            to_status=new_status.value,
            tu_mi_raw_status=tu_mi_raw_status or self.tu_mi_raw_status,
            screenshot_path=screenshot_path,
        )
        self.history.append(entry)
        self.status = new_status.value
        if tu_mi_raw_status:
            self.tu_mi_raw_status = tu_mi_raw_status

    def reset_for_rerun(self):
        """把当天已经跑过的任务放回"排队中"，供人工重跑用。

        保留 history：查看页面上要能连着看到"失败→重跑→完成"的完整链路，而不是从头一份新记录。
        retry_count 必须归零，否则上一轮攒下的重试次数会让这次一失败就直接判死。
        本来就还在排队中的任务（上一轮没跑到就中断了）不算一次重跑，不计数也不记一条状态变迁。
        """
        if self.status != TaskStatus.QUEUED.value:
            self.rerun_count += 1
            self.transition(TaskStatus.QUEUED)
        self.row_index = -1
        self.window_pid = None
        self.pending_started_at = None
        self.retry_count = 0

    def to_dict(self):
        return {
            "role_name": self.role_name,
            "status": self.status,
            "tu_mi_raw_status": self.tu_mi_raw_status,
            "row_index": self.row_index,
            "window_pid": self.window_pid,
            "pending_started_at": self.pending_started_at,
            "retry_count": self.retry_count,
            "rerun_count": self.rerun_count,
            "history": [h.to_dict() for h in self.history],
        }

    @staticmethod
    def from_dict(d):
        task = RoleTask(
            role_name=d["role_name"],
            status=d.get("status", TaskStatus.QUEUED.value),
            tu_mi_raw_status=d.get("tu_mi_raw_status", ""),
            row_index=d.get("row_index", -1),
            window_pid=d.get("window_pid"),
            pending_started_at=d.get("pending_started_at"),
            retry_count=d.get("retry_count", 0),
            rerun_count=d.get("rerun_count", 0),
        )
        task.history = [HistoryEntry.from_dict(h) for h in d.get("history", [])]
        return task


class QueueState:
    """当天所有角色任务的共享状态容器，setup worker（scheduler）和监控线程（monitor）都会读写它，
    用RLock保护——监控线程整体轮询时会先加一次锁，内部再调用requeue/mark_done等方法，
    这些方法自己也要加锁，所以必须用可重入锁，否则同一线程二次加锁会死锁。"""

    def __init__(self, role_names, max_concurrent, existing_tasks=None):
        """existing_tasks 用于重跑：传入当天已有的 RoleTask（调用方需先 reset_for_rerun 放回排队中），
        没传或某个角色没有对应记录时，按全新任务处理。"""
        self.lock = threading.RLock()
        self.max_concurrent = max_concurrent
        self.role_order = list(role_names)
        existing = {task.role_name: task for task in (existing_tasks or [])}
        self.tasks = {
            name: existing.get(name) or RoleTask(role_name=name) for name in role_names
        }
        self.queue_order = list(role_names)
        self.row_map = {}

    @property
    def active(self):
        """当前占用并发槽位的任务（待运行+运行中），role_name -> RoleTask"""
        with self.lock:
            return {
                name: task for name, task in self.tasks.items()
                if task.status in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
            }

    def active_count(self):
        with self.lock:
            return len(self.active)

    def admit_next(self):
        """如果还有空闲并发槽位，从等待队列头部取出下一个任务，标记为待运行并启动计时。
        找不到可入队任务或没有空闲槽位时返回None。"""
        with self.lock:
            if self.active_count() >= self.max_concurrent or not self.queue_order:
                return None
            role_name = self.queue_order.pop(0)
            task = self.tasks[role_name]
            task.transition(TaskStatus.PENDING)
            task.pending_started_at = now_iso()
            return task

    def assign_row(self, task, row_index):
        with self.lock:
            task.row_index = row_index
            self.row_map[row_index] = task.role_name

    def requeue(self, task):
        """任务超时失败但还没到重试上限，清空运行相关信息，重新排到等待队列队尾"""
        with self.lock:
            self.row_map.pop(task.row_index, None)
            task.row_index = -1
            task.window_pid = None
            task.pending_started_at = None
            self.queue_order.append(task.role_name)

    def mark_done(self, role_name):
        with self.lock:
            task = self.tasks[role_name]
            self.row_map.pop(task.row_index, None)

    def mark_failed(self, role_name):
        with self.lock:
            task = self.tasks[role_name]
            self.row_map.pop(task.row_index, None)

    def snapshot(self):
        """按角色原始顺序返回所有任务当前状态，供落盘/展示使用"""
        with self.lock:
            return [self.tasks[name] for name in self.role_order]

    def summary_line(self):
        """一行文本概括当前各状态下都有哪些角色，用于日志里快速查看队列整体情况"""
        with self.lock:
            order = [TaskStatus.QUEUED, TaskStatus.PENDING, TaskStatus.RUNNING,
                     TaskStatus.DONE, TaskStatus.FAILED]
            parts = []
            for status in order:
                names = [t.role_name for t in self.tasks.values() if t.status == status.value]
                parts.append(f"{status.value}[{len(names)}]:{','.join(names) if names else '-'}")
            return " | ".join(parts)
