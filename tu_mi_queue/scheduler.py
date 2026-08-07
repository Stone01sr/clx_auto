import logging
import time

from tu_mi_queue.models import TaskStatus
from tu_mi_queue.monitor import capture_tu_mi_screenshot, scan_rows, find_topmost_empty_row

logger = logging.getLogger(__name__)


class Scheduler:
    """串行的setup worker：只要还有空闲并发槽位，就从等待队列取下一个角色，
    立即占用槽位（待运行），再执行登录、选角色、在荼蘼里找空闲行注册脚本并点开始的完整流程。
    这一整套操作依赖PyAutoGUI控制全局唯一的鼠标，所以本身是串行的；点击"开始"之后
    该角色就交给荼蘼后台挂机，不再占用自动化资源，真正的"并发"体现在荼蘼里同时挂着几个角色。"""

    def __init__(self, config, queue_state, role_lookup, setup_role_fn,
                 find_tu_mi_hwnd_fn, close_window_fn, dismiss_error_popup_fn=None):
        monitor_cfg = config["monitor_settings"]
        queue_cfg = config["queue_settings"]
        self.queue_state = queue_state
        self.role_lookup = role_lookup            # role_name -> config.yaml中该角色的配置dict
        self.setup_role_fn = setup_role_fn         # (role, row_index) -> window_pid
        self.find_tu_mi_hwnd_fn = find_tu_mi_hwnd_fn
        self.close_window_fn = close_window_fn
        # 荼蘼弹出异常退出错误框时会挡住整个面板、可能干扰空闲行扫描，扫描前先点掉它
        self.dismiss_error_popup_fn = dismiss_error_popup_fn
        self.idle_poll_interval = queue_cfg["idle_poll_interval_sec"]
        self.wait_log_interval = queue_cfg["wait_log_interval_sec"]
        self.row_cfg = monitor_cfg["row"]
        self.max_rows = monitor_cfg["max_visible_rows"]
        self.status_templates = monitor_cfg.get("status_templates", {})
        self.color_tolerance = monitor_cfg["color_tolerance"]
        self.confidence = config["global_settings"]["image_match"]["confidence"]
        self.max_retries = queue_cfg["max_retries"]
        self._last_wait_log_at = 0

    def run_until_all_finished(self):
        """主循环：持续尝试把等待队列中的角色送入运行队列，直到当天所有角色都到达终态（运行完成/失败）"""
        logger.info("队列调度开始，共%d个角色，并发上限%d。初始队列状态: %s",
                    len(self.queue_state.role_order), self.queue_state.max_concurrent,
                    self.queue_state.summary_line())
        while not self._all_finished():
            task = self.queue_state.admit_next()
            if task is None:
                self._log_waiting()
                time.sleep(self.idle_poll_interval)
                continue
            logger.info("角色 %s 占用并发槽位，进入待运行。当前队列状态: %s",
                        task.role_name, self.queue_state.summary_line())
            self._process_task(task)
        logger.info("队列调度结束，最终队列状态: %s", self.queue_state.summary_line())

    def _log_waiting(self):
        """并发槽位满或等待队列为空时，调度器只能空转等待；限流打印，避免每隔几秒刷一遍屏"""
        now = time.time()
        if now - self._last_wait_log_at >= self.wait_log_interval:
            self._last_wait_log_at = now
            logger.info("并发槽位已满或等待队列为空，调度器等待中。当前队列状态: %s",
                        self.queue_state.summary_line())

    def _all_finished(self):
        with self.queue_state.lock:
            return all(
                task.status in (TaskStatus.DONE.value, TaskStatus.FAILED.value)
                for task in self.queue_state.tasks.values()
            )

    def _process_task(self, task):
        role = self.role_lookup[task.role_name]
        logger.info("角色 %s 开始执行登录/选角色/注册脚本流程", task.role_name)
        try:
            row_index = self._find_empty_row()
            if row_index is None:
                raise RuntimeError("荼蘼面板没有空闲行，可能行数配置不足或存在未追踪的残留任务")
            self.queue_state.assign_row(task, row_index)
            logger.info("角色 %s 分配到荼蘼第%d行，开始登录", task.role_name, row_index)
            window_pid = self.setup_role_fn(role, row_index)
            task.window_pid = window_pid
            logger.info("角色 %s 已注册到荼蘼第%d行并点击开始（游戏客户端pid=%s）",
                        task.role_name, row_index, window_pid)
        except Exception:
            logger.exception("角色 %s 登录/注册脚本流程失败", task.role_name)
            self._handle_setup_failure(task)

    def _find_empty_row(self):
        hwnd = self.find_tu_mi_hwnd_fn()
        if not hwnd:
            logger.warning("未找到荼蘼窗口，无法扫描空闲行")
            return None
        if self.dismiss_error_popup_fn:
            try:
                if self.dismiss_error_popup_fn():
                    logger.warning("扫描空闲行前检测到荼蘼异常退出错误弹窗，已点击\"确定\"关闭")
            except Exception:
                logger.exception("扫描空闲行前检查/关闭荼蘼错误弹窗失败")
        screenshot = capture_tu_mi_screenshot(hwnd)
        row_statuses = scan_rows(screenshot, self.row_cfg, self.max_rows, self.status_templates,
                                  self.color_tolerance, self.confidence)
        logger.info("荼蘼行扫描结果（颜色/原始状态文字）: %s",
                    {i: ("绿" if v["is_running_color"] else "白", v["raw_status"])
                     for i, v in sorted(row_statuses.items())})
        row_index = find_topmost_empty_row(row_statuses)
        if row_index is None:
            logger.warning("荼蘼面板扫描到的%d行全部处于运行中，没有空闲行可用", self.max_rows)
        return row_index

    def _handle_setup_failure(self, task):
        """setup过程本身抛异常（比如图片一直识别不到），按失败处理：关窗口+计入重试次数，
        逻辑和monitor里30分钟超时失败的处理保持一致（重试<=3次回等待队列队尾，超过则终态失败）。"""
        if task.window_pid and self.close_window_fn:
            try:
                self.close_window_fn(task.window_pid)
                logger.info("已关闭角色 %s 对应的游戏窗口（pid=%s）", task.role_name, task.window_pid)
            except Exception:
                logger.exception("关闭角色 %s 对应游戏窗口失败（pid=%s）", task.role_name, task.window_pid)

        task.retry_count += 1
        if task.retry_count <= self.max_retries:
            task.transition(TaskStatus.QUEUED)
            self.queue_state.requeue(task)
            logger.info("角色 %s 第%d/%d次重试，重新进入等待队列队尾。当前队列状态: %s",
                        task.role_name, task.retry_count, self.max_retries, self.queue_state.summary_line())
        else:
            task.transition(TaskStatus.FAILED)
            self.queue_state.mark_failed(task.role_name)
            logger.error("角色 %s 连续失败%d次（超过上限%d），标记为失败/待处理，需人工介入。当前队列状态: %s",
                         task.role_name, task.retry_count, self.max_retries, self.queue_state.summary_line())
