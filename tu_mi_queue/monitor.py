import datetime
import logging
import threading

import pyautogui
import win32gui

from tu_mi_queue.models import TaskStatus, TU_MI_INITIALIZING
from tu_mi_queue.ui_lock import tu_mi_ui

logger = logging.getLogger(__name__)

# 记录哪些状态模板文件已经报过"找不到"的警告，避免每一轮轮询都重复刷屏
_missing_template_warned = set()


def capture_tu_mi_screenshot(hwnd):
    """截取荼蘼窗口区域的截图（PIL Image）"""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return pyautogui.screenshot(region=(left, top, right - left, bottom - top))


def read_row_status(screenshot, row_cfg, status_templates, row_index, color_tolerance, confidence):
    """读取截图中某一行的运行态颜色和荼蘼原始状态文字。
    行颜色（绿=运行中/白=空闲）用于判断行是否空闲、以及运行完成；PENDING->RUNNING的判断
    需要颜色和文字两个信号同时满足才切换，文字识别不到时统一返回"未知"，不能单凭"未知"
    就当作"已经离开未启动/初始化"（否则会在颜色还是白色、根本还没登录时就被误判成运行中）。

    y方向每一行都要重新算(top_y + row_index*height)，因为不同行本来就在不同高度；
    x方向所有行的横向布局完全一样(导航按钮、状态点、文字都在固定x位置)，只有一个全局基准
    content_left_x（每行内容区左边缘、导航按钮右侧那条线，相对窗口客户区最左边缘的像素距离），
    status_color_point.x / status_text_region.x1,x2 都是相对这条线量的，要先加上这个基准
    才是相对窗口左边缘（也就是截图row_image里x=0）的真实像素位置。"""
    row_top = row_cfg["top_y"] + row_index * row_cfg["height"]
    row_height = row_cfg["height"]
    row_image = screenshot.crop((0, row_top, screenshot.width, row_top + row_height))
    content_left_x = row_cfg.get("content_left_x", 0)

    color_point = row_cfg["status_color_point"]
    try:
        rgb = row_image.getpixel((content_left_x + color_point["x"], color_point["y"]))
    except Exception:
        rgb = None
    is_running_color = _is_green(rgb, color_tolerance)
    is_abnormal_color = _is_red(rgb, color_tolerance)

    text_region = row_cfg["status_text_region"]
    text_crop = row_image.crop((
        content_left_x + text_region["x1"], text_region["y1"],
        content_left_x + text_region["x2"], text_region["y2"],
    ))
    raw_status, match_detail = _match_status_text(text_crop, status_templates, confidence)

    return {
        "is_running_color": is_running_color,
        "is_abnormal_color": is_abnormal_color,
        "raw_status": raw_status,
        "color_rgb": rgb,
        "text_match_detail": match_detail,
    }


def scan_rows(screenshot, row_cfg, max_rows, status_templates, color_tolerance, confidence):
    """扫描截图中所有可见行的状态，返回 {行号: 状态信息}"""
    return {i: read_row_status(screenshot, row_cfg, status_templates, i, color_tolerance, confidence)
            for i in range(max_rows)}


def find_topmost_empty_row(row_statuses):
    """按行号从上到下找第一个"空闲"（非绿色运行中）的行，找不到返回None"""
    for row_index in sorted(row_statuses):
        if not row_statuses[row_index]["is_running_color"]:
            return row_index
    return None


def _is_green(rgb, tolerance):
    if not rgb:
        return False
    r, g, b = rgb[:3]
    return g > r + tolerance and g > b + tolerance


def _is_red(rgb, tolerance):
    """行背景变红(粉色)=游戏客户端异常退出（配合荼蘼弹出的错误框一起出现）。
    已用真实截图校准：异常行在status_color_point采到的是RGB(255,192,203)，
    正常运行的绿色行是RGB(144,238,144)，容差40能正确区分两者。"""
    if not rgb:
        return False
    r, g, b = rgb[:3]
    return r > g + tolerance and r > b + tolerance


def _match_status_text(text_image, status_templates, confidence):
    """依次尝试每个状态模板，返回(识别到的状态名或"未知", 每个模板尝试结果的明细列表)，
    明细列表供调用方写进日志，方便排查具体是哪个模板没匹配上、还是文件根本不存在。"""
    detail = []
    for name, template_path in status_templates.items():
        try:
            if pyautogui.locate(template_path, text_image, confidence=confidence):
                detail.append(f"{name}:匹配成功")
                return name, detail
            detail.append(f"{name}:未匹配")
        except FileNotFoundError:
            detail.append(f"{name}:模板文件不存在({template_path})")
            if template_path not in _missing_template_warned:
                _missing_template_warned.add(template_path)
                logger.warning(
                    "状态模板图片不存在: %s（对应状态\"%s\"），荼蘼原始状态文字暂时无法识别；"
                    "不影响状态机判断（判断依据是行颜色+文字确认），只是该角色的荼蘼原始状态会一直显示'未知'",
                    template_path, name)
        except Exception as e:
            detail.append(f"{name}:识别异常({e})")
    return "未知", detail


class TuMiMonitor:
    """定时截图荼蘼窗口，识别每行状态，驱动队列状态机的PENDING->RUNNING->DONE流转和超时判定。
    PENDING->RUNNING需要行背景色变绿、且荼蘼原始状态文字确认已离开"未启动"/"初始化"两个信号
    同时成立才切换；RUNNING->DONE只看行颜色变白。这样既符合"文字离开初始化才算运行中"的
    原始需求，又不会在颜色还没变、文字识别失败返回"未知"时被误判成已经开始运行。"""

    def __init__(self, config, queue_state, state_store, find_tu_mi_hwnd_fn, close_window_fn,
                 dismiss_popups_fn=None):
        monitor_cfg = config["monitor_settings"]
        queue_cfg = config["queue_settings"]
        self.queue_state = queue_state
        self.state_store = state_store
        self.find_tu_mi_hwnd_fn = find_tu_mi_hwnd_fn
        self.close_window_fn = close_window_fn
        # 游戏异常退出时荼蘼会弹出模态错误框，弹出后整个荼蘼不可操作；每轮轮询先检查并点掉它，
        # 不传则跳过检测（保持向后兼容，不强制要求调用方提供）
        self.dismiss_popups_fn = dismiss_popups_fn
        self.interval = monitor_cfg["screenshot_interval_sec"]
        self.max_rows = monitor_cfg["max_visible_rows"]
        self.row_cfg = monitor_cfg["row"]
        self.status_templates = monitor_cfg.get("status_templates", {})
        self.color_tolerance = monitor_cfg["color_tolerance"]
        self.confidence = config["global_settings"]["image_match"]["confidence"]
        self.stop_join_timeout = monitor_cfg["stop_join_timeout_sec"]
        # 等荼蘼界面锁的上限：主流程正在注册脚本时监控就得让路，等超过这个时间就跳过本轮
        self.ui_lock_timeout = monitor_cfg["ui_lock_timeout_sec"]
        self.pending_timeout = queue_cfg["pending_timeout_sec"]
        self.max_retries = queue_cfg["max_retries"]
        self._stop_event = threading.Event()
        self._thread = None
        self._poll_count = 0

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tu-mi-monitor")
        self._thread.start()
        logger.info("荼蘼状态监控线程已启动，轮询间隔%s秒", self.interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.stop_join_timeout)
        logger.info("荼蘼状态监控线程已停止")

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("荼蘼状态轮询失败，跳过本轮")
            self._stop_event.wait(self.interval)

    def poll_once(self):
        self._poll_count += 1
        # 点弹窗、截图都是在动/在读荼蘼界面，必须和"注册脚本"那种连续操作互斥：
        # 一次插进去的点击就能把人家展开着的方案下拉列表收掉，导致角色整个重新登录。
        # 这里带超时地拿锁，拿不到就跳过本轮——监控只是周期性看一眼状态，绝不能把自己卡死
        with tu_mi_ui(f"[第{self._poll_count}轮] 截图识别荼蘼状态",
                      timeout=self.ui_lock_timeout) as acquired:
            if not acquired:
                return
            # 弹框要在截图之前关掉：它是浮在面板上的，不关掉就会连它一起截进来，
            # 被盖住的那几行会被读成空闲，主流程紧接着就往正在跑的行上塞新角色
            self._dismiss_popups_if_needed()

            hwnd = self.find_tu_mi_hwnd_fn()
            if not hwnd:
                logger.warning("[第%d轮] 未找到荼蘼窗口，跳过本轮状态识别", self._poll_count)
                return

            screenshot = capture_tu_mi_screenshot(hwnd)

        today = datetime.date.today()
        moment = datetime.datetime.now()
        screenshot_path = self.state_store.save_screenshot(today, screenshot, moment)

        row_statuses = scan_rows(screenshot, self.row_cfg, self.max_rows, self.status_templates,
                                  self.color_tolerance, self.confidence)

        with self.queue_state.lock:
            active = list(self.queue_state.active.items())
            logger.info("[第%d轮] 截图已保存: %s，当前活跃任务%d个，全部行扫描结果: %s",
                        self._poll_count, screenshot_path, len(active),
                        {i: ("绿" if v["is_running_color"] else "白", v["raw_status"])
                         for i, v in sorted(row_statuses.items())})

            if not active:
                logger.info("[第%d轮] 当前没有处于待运行/运行中的任务，跳过逐任务比对", self._poll_count)

            for role_name, task in active:
                info = row_statuses.get(task.row_index)
                if info is None:
                    logger.warning("角色 %s 分配的行号%s超出扫描范围(max_visible_rows=%d)，本轮跳过",
                                    role_name, task.row_index, self.max_rows)
                    continue
                logger.info("角色 %s: 当前状态=%s, 所在行=第%d行, 本轮识别 颜色=%s(RGB=%s) 原始状态文字=%s "
                            "[逐模板匹配情况: %s]",
                            role_name, task.status, task.row_index,
                            "绿/运行中" if info["is_running_color"] else "白/空闲",
                            info["color_rgb"], info["raw_status"], "; ".join(info["text_match_detail"]))
                self._apply_task_update(task, info, screenshot_path)

            self._check_pending_timeouts(moment, screenshot_path)
            logger.info("[第%d轮] 本轮处理完毕，队列状态: %s",
                        self._poll_count, self.queue_state.summary_line())

        self.state_store.save(today, self.queue_state.snapshot())

    def _dismiss_popups_if_needed(self):
        if not self.dismiss_popups_fn:
            return
        try:
            if self.dismiss_popups_fn():
                logger.warning("[第%d轮] 截图前检测到荼蘼弹框并已关闭（弹框内容见上一条日志）",
                                self._poll_count)
        except Exception:
            logger.exception("[第%d轮] 检查/关闭荼蘼弹框失败", self._poll_count)

    def _apply_task_update(self, task, info, screenshot_path):
        is_running_color = info["is_running_color"]
        is_abnormal_color = info["is_abnormal_color"]
        raw_status = info["raw_status"]

        # 荼蘼原始状态文字只用于展示，识别到了就更新展示值，识别不到("未知")不覆盖旧值
        if raw_status != "未知" and raw_status != task.tu_mi_raw_status:
            task.tu_mi_raw_status = raw_status

        # 行变红=游戏客户端异常退出，不管当前是待运行还是运行中都直接按失败处理，
        # 不用等PENDING的30分钟超时——异常退出的信号比超时更明确、更该立刻响应
        if is_abnormal_color and task.status in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
            logger.warning("角色 %s 荼蘼第%d行变红，判定为游戏客户端异常退出",
                            task.role_name, task.row_index)
            self._handle_abnormal_exit(task, raw_status, screenshot_path)
            return

        if task.status == TaskStatus.PENDING.value:
            # 运行中的判断：行颜色变绿 + 荼蘼原始状态不是"初始化"。"未知"（没识别出具体是什么状态）
            # 也算"不是初始化"，不需要额外配一张"运行中"模板去正向确认——颜色变绿本身已经是
            # 足够强的信号了，文字这里只用来排除"还卡在初始化阶段"这一种情况。
            if is_running_color and raw_status != TU_MI_INITIALIZING:
                task.transition(TaskStatus.RUNNING, tu_mi_raw_status=raw_status, screenshot_path=screenshot_path)
                logger.info("角色 %s 荼蘼行颜色变绿且不再是初始化（当前识别: %s），标记为运行中",
                            task.role_name, raw_status)
            elif is_running_color:
                logger.info("角色 %s 行颜色已变绿，但荼蘼原始状态仍是\"初始化\"，暂不切换到运行中，等待下一轮确认",
                            task.role_name)
        elif task.status == TaskStatus.RUNNING.value:
            if not is_running_color:
                task.transition(TaskStatus.DONE, tu_mi_raw_status=raw_status, screenshot_path=screenshot_path)
                self.queue_state.mark_done(task.role_name)
                logger.info("角色 %s 荼蘼行颜色变白，标记为运行完成", task.role_name)

    def _handle_abnormal_exit(self, task, raw_status, screenshot_path):
        """游戏客户端异常退出（荼蘼行变红）后的处理，重试/终态失败逻辑与_check_pending_timeouts
        的超时分支保持一致，只是触发条件换成了行变红而不是等待超时。"""
        if task.window_pid and self.close_window_fn:
            try:
                self.close_window_fn(task.window_pid)
                logger.info("已关闭角色 %s 异常退出的游戏窗口（pid=%s）", task.role_name, task.window_pid)
            except Exception:
                logger.exception("关闭角色 %s 异常退出的游戏窗口失败（pid=%s）", task.role_name, task.window_pid)

        task.retry_count += 1
        if task.retry_count <= self.max_retries:
            task.transition(TaskStatus.QUEUED, tu_mi_raw_status=raw_status, screenshot_path=screenshot_path)
            self.queue_state.requeue(task)
            logger.info("角色 %s 异常退出，第%d/%d次重试，重新进入等待队列队尾",
                        task.role_name, task.retry_count, self.max_retries)
        else:
            task.transition(TaskStatus.FAILED, tu_mi_raw_status=raw_status, screenshot_path=screenshot_path)
            self.queue_state.mark_failed(task.role_name)
            logger.error("角色 %s 异常退出且连续失败%d次（超过上限%d），标记为失败/待处理，需人工介入",
                         task.role_name, task.retry_count, self.max_retries)

    def _check_pending_timeouts(self, moment, screenshot_path):
        for role_name, task in list(self.queue_state.active.items()):
            if task.status != TaskStatus.PENDING.value or not task.pending_started_at:
                continue
            started = datetime.datetime.fromisoformat(task.pending_started_at)
            elapsed = (moment - started).total_seconds()
            if elapsed < self.pending_timeout:
                continue

            logger.warning("角色 %s 待运行已超过%d秒（超时阈值%d秒），判定为失败",
                            role_name, int(elapsed), self.pending_timeout)
            if task.window_pid and self.close_window_fn:
                try:
                    self.close_window_fn(task.window_pid)
                    logger.info("已关闭角色 %s 对应的游戏窗口（pid=%s）", role_name, task.window_pid)
                except Exception:
                    logger.exception("关闭角色 %s 对应游戏窗口失败（pid=%s）", role_name, task.window_pid)

            task.retry_count += 1
            if task.retry_count <= self.max_retries:
                task.transition(TaskStatus.QUEUED, screenshot_path=screenshot_path)
                self.queue_state.requeue(task)
                logger.info("角色 %s 第%d/%d次重试，重新进入等待队列队尾",
                            role_name, task.retry_count, self.max_retries)
            else:
                task.transition(TaskStatus.FAILED, screenshot_path=screenshot_path)
                self.queue_state.mark_failed(role_name)
                logger.error("角色 %s 连续失败%d次（超过上限%d），标记为失败/待处理，需人工介入",
                             role_name, task.retry_count, self.max_retries)
