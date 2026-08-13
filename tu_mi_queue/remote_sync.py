"""把队列状态同步到服务器的展示页面（craft_book 的"挂机运行情况"）。

设计前提是**绝不能拖累本机挂机流程**：挂机全程靠 PyAutoGUI 抢全局鼠标，主流程或监控线程
被网络卡住几秒，轻则监控轮次错拍，重则拿着荼蘼界面锁不放把调度器一起堵死。所以：

- `push()` 只做两件事：把已经序列化好的快照放进单槽位、叫醒后台线程，然后立刻返回；
- 真正的 HTTP 请求全在守护线程里做，带超时、失败只写日志，异常一律不外抛；
- 槽位只留最新一份快照。网络慢的时候旧快照直接被覆盖丢弃——状态是全量上报的，
  发最新那份就够了，积压重发反而会让页面上的数据越来越滞后；
- 服务器不通、密码没配、URL 写错，全都只是"页面上看不到"，本机该跑照跑。
"""

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

SYNC_PATH = "/api/clx-status/sync"
OVERVIEW_PATH = "/api/clx-status/overview"


class RemoteSyncNotConfigured(Exception):
    """上报没配好（没启用/没填地址/环境变量里没密码）。带一句能直接给人看的原因。"""


def resolve_endpoint(config):
    """从 config.yaml 的 remote_sync 段解析出 (上报地址, 密码, 机器名, 该段配置)。

    没配好就抛 RemoteSyncNotConfigured，由调用方决定是"只打条日志继续挂机"（挂机流程）
    还是"打印原因后退出"（命令行工具）。
    """
    settings = (config or {}).get("remote_sync") or {}
    if not settings.get("enabled"):
        raise RemoteSyncNotConfigured("config.yaml 里 remote_sync.enabled 不是 true，没有开启上报")

    base_url = (settings.get("base_url") or "").strip()
    if not base_url:
        raise RemoteSyncNotConfigured("config.yaml 里 remote_sync.base_url 是空的，不知道该往哪传")

    token_env = settings.get("token_env", "CLX_SYNC_TOKEN")
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise RemoteSyncNotConfigured(
            "环境变量 %s 是空的（它应该等于网站的写入密码）。"
            "设置方法见 README 的「在别的设备上看运行情况」；"
            "改完环境变量要把命令行窗口全部关掉重开才生效" % token_env)

    machine = (settings.get("machine_name") or "").strip() or socket.gethostname()
    return base_url.rstrip("/") + SYNC_PATH, token, machine, settings


def build_payload(machine, date_str, task_dicts, running=True, client_time=None):
    """组装一次上报的请求体。task_dicts 是 RoleTask.to_dict() 出来的普通 dict 列表。"""
    return {
        "machine": machine,
        "date": date_str,
        "running": running,
        # 客户端本地时间，只用于展示"那台机器自己认为现在几点"；判不判超时由服务器按自己的时钟算
        "client_time": client_time or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": task_dicts,
    }


def post_snapshot(url, token, payload, timeout_sec=5):
    """同步发一份快照，成功返回服务器的响应 dict，失败按 urllib 的异常往外抛。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "X-Write-Token": token},
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


class RemoteSync:
    """后台上报线程。用法：push() 推快照，收尾时 close() 发最后一份并退出。"""

    def __init__(self, url, token, machine_name, timeout_sec=5,
                 max_retries=2, retry_backoff_sec=3):
        self.url = url
        self.token = token
        self.machine_name = machine_name
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec

        self._lock = threading.Lock()
        self._pending = None          # 待发送的最新快照，只留一份
        self._stopping = False
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="remote-sync")
        self._thread.start()
        logger.info("状态上报线程已启动，目标: %s（机器名 %s）", self.url, self.machine_name)

    @classmethod
    def from_config(cls, config):
        """按 config.yaml 的 remote_sync 段创建；没配好时只打一条警告并返回 None，
        调用方按"不上报"处理——上报配不配得好，都不该影响挂机能不能跑。"""
        try:
            url, token, machine, settings = resolve_endpoint(config)
        except RemoteSyncNotConfigured as e:
            logger.warning("本次不上报运行状态（本机挂机不受影响）：%s", e)
            return None

        try:
            return cls(
                url=url,
                token=token,
                machine_name=machine,
                timeout_sec=settings.get("timeout_sec", 5),
                max_retries=settings.get("max_retries", 2),
                retry_backoff_sec=settings.get("retry_backoff_sec", 3),
            )
        except Exception:
            logger.exception("状态上报线程启动失败，本次不上报运行状态；本机挂机不受影响")
            return None

    def push(self, date_str, task_dicts, running=True):
        """入队一份快照。task_dicts 必须是调用方已经转好的普通 dict 列表——
        RoleTask 是主流程和监控线程共享的可变对象，直接把它交给后台线程会读到半更新的状态。"""
        try:
            payload = build_payload(self.machine_name, date_str, task_dicts, running)
            with self._lock:
                if self._stopping:
                    return
                self._pending = payload
            self._wake.set()
        except Exception:
            # 上报是附加功能，出任何问题都不能反过来影响调用它的落盘/监控流程
            logger.exception("准备上报数据失败，跳过本次上报")

    def close(self, date_str=None, task_dicts=None, timeout_sec=15):
        """收尾：可选地推最后一份"本轮已结束"的快照，然后等后台线程把队列里的发完。

        等待有上限，超时就不等了——线程是 daemon，进程该退还是能退，
        绝不能因为服务器没响应就让整个挂机程序卡在最后一步不结束。
        """
        if date_str is not None and task_dicts is not None:
            self.push(date_str, task_dicts, running=False)
        with self._lock:
            self._stopping = True
        self._wake.set()
        self._thread.join(timeout=timeout_sec)
        if self._thread.is_alive():
            logger.warning("状态上报线程在%d秒内未结束，不再等待（不影响程序退出）", timeout_sec)
        else:
            logger.info("状态上报线程已结束")

    def _run_loop(self):
        while True:
            with self._lock:
                payload, self._pending = self._pending, None
                if payload is None:
                    if self._stopping:
                        return
                    # 清标记必须和"取快照"在同一把锁里，否则可能清掉刚 push 进来那次的唤醒
                    self._wake.clear()
            if payload is None:
                self._wake.wait()
                continue
            self._send_with_retry(payload)

    def _send_with_retry(self, payload):
        for attempt in range(self.max_retries + 1):
            if self._send_once(payload, attempt):
                return
            with self._lock:
                has_newer = self._pending is not None
            if has_newer:
                logger.info("已有更新的状态快照待发送，放弃重发这一份")
                return
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff_sec)
        logger.warning("状态上报连续%d次失败，本次快照丢弃，等下一轮监控再报", self.max_retries + 1)

    def _send_once(self, payload, attempt):
        """返回 True 表示这一份不用再重试了：要么发成功了，要么失败原因（比如密码错）重试也没用。"""
        try:
            post_snapshot(self.url, self.token, payload, self.timeout_sec)
            logger.info("状态已上报服务器（%d个角色）", len(payload["tasks"]))
            return True
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # 密码错了重发多少次都一样，直接放弃这一份
                logger.error("状态上报被拒绝（401）：环境变量里的上报密码和服务器 WRITE_PASSWORD 不一致")
                return True
            logger.warning("状态上报失败（第%d次，HTTP %s）", attempt + 1, e.code)
        except Exception as e:
            logger.warning("状态上报失败（第%d次）：%s", attempt + 1, e)
        return False
