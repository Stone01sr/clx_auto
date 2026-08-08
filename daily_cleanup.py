import datetime
import os
import sys
from random import randint

import pyautogui
import subprocess
import time
import psutil
import pywinctl as pwc
import win32api
import win32con
import win32gui
import win32process
import yaml
import logging
from pywinauto import Application, findwindows

from tu_mi_queue.models import QueueState, TaskStatus
from tu_mi_queue.state_store import StateStore
from tu_mi_queue.monitor import TuMiMonitor
from tu_mi_queue.scheduler import Scheduler

logger = logging.getLogger(__name__)
today = datetime.datetime.now()
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),  # 输出到控制台
        logging.FileHandler(f"app_{today:%Y-%m-%d}.log", encoding="utf-8")      # 输出到文件
    ]
)

with open("config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# 配置分段的快捷引用。代码里不写死任何坐标、标题、超时和阈值，一律从这里取，
# 这样换机器、换分辨率、游戏或荼蘼改版时只需要改 config.yaml
G = config["global_settings"]
POINTS = G["points"]
REGIONS = G["regions"]
IMAGES = G["images"]
TITLES = G["titles"]
PROC = G["process_keywords"]
MATCH = G["image_match"]
RETRIES = G["retries"]
DELAYS = G["delays"]
TIMEOUTS = G["timeouts"]


def region_of(name, top=None):
    """把配置里的搜索范围转成 pyautogui 要的 (左, 上, 宽, 高) 元组。
    script_list 的上边界要按角色所在行号算，所以允许调用方传 top 覆盖。"""
    r = REGIONS[name]
    return (r["left"], r["top"] if top is None else top, r["width"], r["height"])

def open_software(software_path):
    """使用指定路径打开软件，返回被启动进程的pid（启动失败返回None）。
    pid用于setup失败时的兜底清理：窗口还没建出来就失败的话，只能靠它把进程收掉"""
    try:
        proc = subprocess.Popen(software_path)
        logger.info("正在启动软件: %s（pid=%s）", software_path, proc.pid)
        return proc.pid
    except Exception as e:
        logger.error("启动软件失败: %s", e)
        return None



def find_pid_by_keyword(keyword):
    """单次遍历查找可执行文件路径中包含keyword的进程，找不到返回None，不等待"""
    keyword = keyword.lower()
    for proc in psutil.process_iter(['pid', 'exe']):
        try:
            exe = proc.info['exe']
            if exe and keyword in exe.lower():
                return proc.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

def find_visible_hwnd_by_pid(pid, timeout=TIMEOUTS["find_window"], interval=TIMEOUTS["poll_interval"]):
    """通过进程pid轮询查找其可见顶层窗口，返回hwnd，超时返回None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found_hwnd = []
        def _enum_handler(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid == pid:
                    found_hwnd.append(hwnd)
        win32gui.EnumWindows(_enum_handler, None)
        if found_hwnd:
            return found_hwnd[0]
        time.sleep(interval)
    return None

def force_window_foreground(hwnd):
    """将窗口强制置于最前并激活。
    Windows有前台锁定限制：非当前前台线程直接调用SetForegroundWindow经常被系统静默忽略，
    窗口API层面显示visible=True，但实际停在原位、被其他窗口盖住，用户看不到——
    通过AttachThreadInput临时把自己的输入线程"接"到目标窗口线程上，可以绕过该限制。"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread, _ = win32process.GetWindowThreadProcessId(fg_hwnd) if fg_hwnd else (0, 0)
        target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
        cur_thread = win32api.GetCurrentThreadId()

        attached_fg = fg_thread and fg_thread != target_thread and win32process.AttachThreadInput(fg_thread, target_thread, True)
        attached_cur = cur_thread != target_thread and win32process.AttachThreadInput(cur_thread, target_thread, True)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached_fg:
                win32process.AttachThreadInput(fg_thread, target_thread, False)
            if attached_cur:
                win32process.AttachThreadInput(cur_thread, target_thread, False)
        return True
    except Exception as e:
        logger.warning("强制置前窗口失败（hwnd=%s）: %s", hwnd, e)
        return False

def force_window_opaque(hwnd):
    """部分软件用WS_EX_LAYERED做窗口淡入动画，通过自动化方式启动时该动画可能卡住，
    导致窗口alpha一直停在0（完全透明）——窗口本身可见、位置正常，但人眼什么都看不到，
    表现为“打开了但没有窗口”。这里直接读取当前layered属性，如果alpha不是255就强制拉满。"""
    try:
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if not (ex_style & win32con.WS_EX_LAYERED):
            return
        _, alpha, flags = win32gui.GetLayeredWindowAttributes(hwnd)
        if alpha < 255:
            win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, win32con.LWA_ALPHA)
            logger.info("检测到窗口透明度异常（alpha=%s），已强制设为不透明", alpha)
    except Exception as e:
        logger.warning("修正窗口透明度失败（hwnd=%s）: %s", hwnd, e)

def bring_window_to_front_by_pid(pid, timeout=TIMEOUTS["bring_to_front"]):
    """按pid定位窗口（不依赖标题/截图），移到左上角、修正透明度异常并强制置于前台"""
    hwnd = find_visible_hwnd_by_pid(pid, timeout=timeout)
    if not hwnd:
        logger.warning("等待%s秒仍未找到pid=%s的可见窗口，跳过置前", timeout, pid)
        return False
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        win32gui.MoveWindow(hwnd, 0, 0, right - left, bottom - top, True)
    except Exception as e:
        logger.warning("移动窗口位置失败（hwnd=%s）: %s", hwnd, e)
    force_window_opaque(hwnd)
    ok = force_window_foreground(hwnd)
    if ok:
        logger.info("已将窗口置于前台，标题: %s", win32gui.GetWindowText(hwnd))
    return ok

def dismiss_timezone_warning(timeout=TIMEOUTS["timezone_warning"], interval=TIMEOUTS["poll_interval"]):
    """荼蘼启动时如果检测到系统时区不是"中国北京"会弹出"警告"对话框；
    按需求点击"否"继续启动软件，不自动修改系统时区。找不到弹窗则直接返回，不影响后续流程。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        windows = findwindows.find_elements(title_re=TITLES["timezone_warning"], backend="uia")
        if windows:
            try:
                app = Application(backend="uia").connect(handle=windows[0].handle)
                dlg = app.window(handle=windows[0].handle)
                dlg.child_window(title=TITLES["timezone_warning_button"], control_type="Button").click()
                logger.info("检测到时区警告弹窗，已点击“否”继续启动")
                return True
            except Exception as e:
                logger.warning("点击时区警告弹窗“否”按钮失败: %s", e)
                return False
        time.sleep(interval)
    return False

def dismiss_tu_mi_error_popup():
    """游戏客户端异常退出时，荼蘼会弹出"<错误> 插件版本:xxx"的模态错误框（提示"创建DISPLAY失败
    <错误信息 = 无效的窗口句柄。>"），弹出后整个荼蘼变得不可操作，必须先点掉"确定"才能恢复。
    单次检查、不等待——由调用方（监控轮询/扫描空闲行前）按自己的节奏重复调用；
    一次性点掉所有匹配的错误框，避免多个角色同时异常退出时堆叠了多个弹窗。"""
    windows = findwindows.find_elements(title_re=TITLES["tu_mi_error_popup"], backend="uia")
    if not windows:
        return False
    dismissed = False
    for w in windows:
        try:
            app = Application(backend="uia").connect(handle=w.handle)
            dlg = app.window(handle=w.handle)
            dlg.child_window(title=TITLES["tu_mi_error_popup_button"], control_type="Button").click()
            dismissed = True
        except Exception as e:
            logger.warning("点击荼蘼异常退出错误弹窗\"确定\"按钮失败: %s", e)
    return dismissed


def ensure_script_software_open():
    """如果荼蘼未运行，则自动打开；返回荼蘼进程pid（找不到则返回None）"""
    tu_mi_pid = find_pid_by_keyword(PROC["tu_mi"])
    if tu_mi_pid:
        logger.info("检测到荼蘼已在运行（pid=%s），跳过启动", tu_mi_pid)
    else:
        script_path = G["script_path"]
        logger.info("未检测到荼蘼进程，正在自动启动: %s", script_path)
        try:
            os.startfile(script_path)
        except Exception as e:
            logger.error("启动荼蘼失败: %s", e)
            return None
        tu_mi_pid = wait_for_process_by_keyword(PROC["tu_mi"])
        if not tu_mi_pid:
            logger.warning("等待超时，未检测到荼蘼进程，继续后续流程")
            return None
        # 部分电脑系统时区不是"中国北京"时，荼蘼启动过程中会弹出警告框；
        # 按需求点击"否"继续启动软件，不修改系统时区
        dismiss_timezone_warning()
        bring_window_to_front_by_pid(tu_mi_pid, timeout=DELAYS["software_init"])
    return tu_mi_pid

def open_script_window():
    tu_mi_pid = ensure_script_software_open()
    wait_image_and_click(IMAGES['tu_mi_logo'])
    time.sleep(DELAYS["after_tu_mi_logo"])
    if tu_mi_pid:
        bring_window_to_front_by_pid(tu_mi_pid, timeout=TIMEOUTS["tu_mi_front"])
    else:
        script_window = pwc.getWindowsWithTitle(TITLES["tu_mi"])[0]
        script_window.moveTo(0, 0)
        script_window.activate()
    wait_image_and_click(IMAGES['tu_mi_main'])
    pyautogui.click(POINTS["tu_mi_daily_menu"]["x"], POINTS["tu_mi_daily_menu"]["y"])
    return pwc.getWindowsWithTitle(TITLES["tu_mi"])[0]

def get_roles():
    """获取所有启用的角色，按优先级排序"""
    roles = config.get("roles", [])
    enabled_roles = [role for role in roles if role.get("enable", True)]
    return enabled_roles

def run_as_admin_powershell(program_path, arguments=None):
    """
    使用PowerShell以管理员权限运行程序
    """
    # 构建PowerShell命令
    ps_command = f'Start-Process "{program_path}" -Verb RunAs'

    if arguments:
        ps_command = f'Start-Process "{program_path}" -ArgumentList "{arguments}" -Verb RunAs'
    # 执行PowerShell命令
    result = subprocess.run(
        ["powershell", "-Command", ps_command],
        shell=True,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    if result.returncode == 0:
        logger.info("程序已使用管理员身份启动：%s", program_path)
    else:
        logger.info("程序可能被用户取消：%s, %s", program_path, result.stderr)

def wait_for_process_by_keyword(keyword, timeout=TIMEOUTS["wait_process"], interval=TIMEOUTS["poll_interval"]):
    """轮询等待可执行文件路径中包含keyword的进程启动，返回其pid，超时返回None"""
    keyword = keyword.lower()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for proc in psutil.process_iter(['pid', 'exe']):
            try:
                exe = proc.info['exe']
                if exe and keyword in exe.lower():
                    return proc.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(interval)
    return None

def minimize_window_by_pid(pid, timeout=TIMEOUTS["find_window"], interval=TIMEOUTS["poll_interval"]):
    """通过进程pid查找其可见顶层窗口并最小化，找不到则返回False，不抛异常。
    idv-login新版本不再有固定的窗口标题，按pid定位比按标题匹配更稳健。"""
    hwnd = find_visible_hwnd_by_pid(pid, timeout=timeout, interval=interval)
    if hwnd:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        logger.info("已最小化idv-login窗口，标题: %s", win32gui.GetWindowText(hwnd))
        return True
    logger.warning("未能找到idv-login的可见窗口（pid=%s），跳过最小化", pid)
    return False

def kill_process_tree(pid):
    """终止指定pid及其所有子进程（idv-login可能会派生子进程，如渠道服账号管理窗口；
    游戏启动器也可能把真正的客户端拉起为子进程）。既用于关idv-login，也用作队列里的关窗口手段"""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(children, timeout=TIMEOUTS["kill_process"])
        parent.terminate()
        parent.wait(timeout=TIMEOUTS["kill_process"])
        logger.info("已关闭进程（pid=%s）及其子进程", pid)
    except psutil.NoSuchProcess:
        logger.info("进程（pid=%s）已不存在，无需关闭", pid)
    except Exception as e:
        logger.error("关闭进程（pid=%s）时发生错误: %s", pid, e)

def wait_image_and_click(image_path, region=None, max_retries=MATCH["default_max_retries"]):
    find_flag = False
    retries = 0
    while not find_flag:
        try:
            if region:
                account_field_pos = pyautogui.locateCenterOnScreen(image_path, region=region, confidence=MATCH["confidence"])
            else:
                account_field_pos = pyautogui.locateCenterOnScreen(image_path, confidence=MATCH["confidence"])
            pyautogui.click(account_field_pos)
            logger.info("已找到图片: %s并点击", image_path)
            find_flag = True
        except pyautogui.ImageNotFoundException:
            retries += 1
            time.sleep(MATCH["retry_interval_sec"])
            if retries > max_retries:
                raise Exception("can not find image: %s after %d retries", image_path, retries)
            logger.info("未找到图片: %s，继续寻找...", image_path)

def scroll_list_and_locate(image_path, scroll_cfg, confidence=MATCH["confidence"]):
    """从当前位置开始，在一个可滚动列表里逐屏向下查找图片。
    找到返回中心坐标；一直滚到底仍找不到返回None。
    滚轮只对鼠标光标所在的控件生效，所以每次滚动都要把光标放到列表区域内（scroll_cfg.point）。"""
    point = scroll_cfg["point"]
    max_scrolls = scroll_cfg["max_scrolls"]
    for scrolled in range(max_scrolls + 1):  # 第0次先在当前可见范围找，之后每滚一屏找一次
        try:
            pos = pyautogui.locateCenterOnScreen(image_path, confidence=confidence)
            logger.info("在角色列表第%d屏找到图片: %s", scrolled + 1, image_path)
            return pos
        except pyautogui.ImageNotFoundException:
            if scrolled >= max_scrolls:
                logger.info("已向下滚动%d次至列表底部，仍未找到图片: %s", max_scrolls, image_path)
                return None
            pyautogui.scroll(scroll_cfg["clicks"], x=point["x"], y=point["y"])
            logger.info("当前屏未找到图片: %s，向下滚动第%d/%d次继续查找",
                        image_path, scrolled + 1, max_scrolls)


def scroll_list_to_top(scroll_cfg):
    """把列表滚回顶部，便于下一轮从头开始查找；多滚几次确保到顶（已经到顶后继续滚不会有副作用）"""
    point = scroll_cfg["point"]
    up_clicks = abs(scroll_cfg["clicks"])
    for _ in range(scroll_cfg["max_scrolls"] + 1):
        pyautogui.scroll(up_clicks, x=point["x"], y=point["y"])


def wait_image_and_click_in_scrollable_list(image_path, scroll_cfg, confidence=MATCH["confidence"]):
    """在可滚动列表中查找并点击图片，找不到抛异常（交由上层按失败重试处理）。
    和wait_image_and_click的区别：那个只在当前屏原地重试，角色不在首屏时永远找不到；
    这个会逐屏向下翻找，整轮扫完仍没找到就滚回顶部再来一轮（应对列表还没渲染完的情况）。"""
    max_rounds = scroll_cfg["max_rounds"]
    for round_index in range(max_rounds):
        pos = scroll_list_and_locate(image_path, scroll_cfg, confidence=confidence)
        if pos:
            pyautogui.click(pos)
            logger.info("已找到图片: %s并点击", image_path)
            return pos
        if round_index < max_rounds - 1:
            logger.info("第%d/%d轮滚动查找未找到图片: %s，滚回列表顶部重试",
                        round_index + 1, max_rounds, image_path)
            scroll_list_to_top(scroll_cfg)
    raise Exception(f"在列表中滚动查找{max_rounds}轮仍未找到图片: {image_path}")


class RoleLaunchContext:
    """记录一次角色setup过程中"我们启动了什么"，失败时据此把这一次开出来的东西全部收干净。
    必须一边启动一边记、而不是等流程跑完再统一收集：失败点可以出现在任何一步（多数是某张
    图片一直识别不到），早期失败时window_pid还没拿到，光靠它清理会漏掉已经打开的
    "一梦江湖"窗口，于是每重试一次就多留一个窗口，最后堆出一屏幕。"""

    def __init__(self):
        self.existing_clx_windows = snapshot_clx_windows()  # setup开始前就存在的游戏窗口，不能碰
        self.launcher_pid = None    # 我们拉起的Launcher.exe进程
        self.idv_pid = None         # 渠道服账号登录用的idv-login进程
        self.window_pid = None      # 本次新开的游戏客户端窗口所属进程

    def cleanup(self, role_name):
        """关掉本次setup新开的游戏窗口和辅助进程，让重试从干净的状态重新开始"""
        closed = close_new_clx_windows(self.existing_clx_windows)
        logger.info("角色 %s setup失败清理：关闭了%d个本次新打开的一梦江湖窗口", role_name, closed)
        # 窗口一个都没关到，说明失败得很早（客户端窗口还没建出来、或标题还不是"一梦江湖"），
        # 这时按启动器pid兜底，避免留下一个看不见的客户端进程占着账号
        if closed == 0 and self.launcher_pid:
            logger.info("角色 %s 未匹配到新的一梦江湖窗口，改按启动器进程清理（pid=%s）",
                        role_name, self.launcher_pid)
            kill_process_tree(self.launcher_pid)
        if self.idv_pid:
            logger.info("角色 %s setup失败清理：关闭残留的idv-login进程（pid=%s）", role_name, self.idv_pid)
            kill_process_tree(self.idv_pid)


def open_clx_and_login(role, ctx):
    is_channel_account = role.get('channel_account', False)
    # 渠道服账号不是官服，不能直接登录，要借助外部脚本idv
    if is_channel_account:
        run_as_admin_powershell(G["idv_login_path"], '--open-ui')
        ctx.idv_pid = wait_for_process_by_keyword(PROC["idv_login"])
        if ctx.idv_pid:
            minimize_window_by_pid(ctx.idv_pid)
        else:
            logger.warning("等待超时，未检测到idv-login进程，继续后续流程")
    # 打开一梦江湖
    ctx.launcher_pid = open_software(G["software_path"])
    time.sleep(DELAYS["after_game_launch"])
    logger.info("糊糊已打开...")
    # 朕知道了
    wait_image_and_click(IMAGES['init_known'])
    if is_channel_account:
        wait_image_and_click(IMAGES['other_account'], max_retries=RETRIES["other_account"])
        wait_image_and_click(IMAGES['logo'], max_retries=RETRIES["logo"])
        time.sleep(DELAYS["after_page_switch"])
        # 渠道服账号排在账号列表靠后的位置，先整页往下翻几次再找
        for _ in range(G["account_list_scroll"]["channel_pagedown_count"]):
            pyautogui.press('pagedown')
        wait_image_and_click(IMAGES['an_login'], max_retries=RETRIES["an_login"])
        idv_channel_title = TITLES["idv_channel_account"]
        an_login_windows = pwc.getWindowsWithTitle(idv_channel_title)
        if len(an_login_windows) > 0:
            an_login_windows[0].minimize()
        time.sleep(DELAYS["after_channel_login"])
        # 渠道服账号登录完成，关闭idv-login，官服账号不依赖该进程，不受影响
        if ctx.idv_pid:
            kill_process_tree(ctx.idv_pid)
            ctx.idv_pid = None  # 已主动关掉，失败清理时不用再关一次
    else:
        # 在账号下拉列表所在范围内找那个倒三角，范围缩小既能提速也避免匹配到屏幕别处
        wait_image_and_click(IMAGES['account_selection'],
                             region=region_of("account_list"),
                             max_retries=RETRIES["account_selection"])
        find_flag = False
        count_down_times = 0
        while not find_flag:
            # 提前截取好账号输入框的图片，保存为 'account_field.png'
            try:
                account_field_pos = pyautogui.locateCenterOnScreen(role["account_image"], confidence=MATCH["confidence"])
                pyautogui.click(account_field_pos)
                logger.info("已找到账号选择框")
                find_flag = True
            except pyautogui.ImageNotFoundException:
                logger.info("未找到账号选择框，继续寻找...")
                scroll_cfg = G["account_list_scroll"]
                if count_down_times == 0:
                    count_times = scroll_cfg["first_press_count"]
                else:
                    count_times = randint(scroll_cfg["next_press_min"], scroll_cfg["next_press_max"])
                # 向下滚动
                for i in range(count_times):
                    pyautogui.press('down')
                count_down_times += 1
                if count_down_times > G["account_list_scroll"]["max_attempts"]:
                    raise Exception("can not find account: %s", role["name"])
        # 点击登录
        wait_image_and_click(IMAGES['login_enter_game'], max_retries=RETRIES["login_enter_game"])
    time.sleep(DELAYS["after_login"])


def bring_tu_mi_to_front():
    """把荼蘼窗口移回左上角并强制置于最前，返回是否成功。
    角色点完"进入游戏"后前台是游戏客户端，会把荼蘼整个盖住；此时再按固定坐标去点
    荼蘼的刷新按钮/方案下拉框，点到的其实是游戏窗口，后面自然就找不到脚本图片了。
    所以每次操作荼蘼之前都先置前一次（顺带把窗口拉回(0,0)，坐标是按这个位置标定的）。"""
    pid = find_pid_by_keyword(PROC["tu_mi"])
    if not pid:
        logger.warning("未找到荼蘼进程，无法在操作前置前荼蘼窗口")
        return False
    ok = bring_window_to_front_by_pid(pid, timeout=TIMEOUTS["tu_mi_front"])
    if not ok:
        logger.warning("操作荼蘼前置前窗口失败（pid=%s），后续点击可能落到别的窗口上", pid)
    return ok


def find_tu_mi_hwnd():
    """定位当前唯一一个荼蘼实例的窗口hwnd，找不到返回None"""
    pid = find_pid_by_keyword(PROC["tu_mi"])
    if not pid:
        return None
    return find_visible_hwnd_by_pid(pid, timeout=TIMEOUTS["find_tu_mi_window"])


def snapshot_clx_windows():
    """获取当前所有"一梦江湖"窗口的 {handle: pid}。一梦江湖的可执行文件是Launcher.exe，
    路径里不含中文关键字，没法像荼蘼那样按exe路径关键字找pid，只能按窗口标题识别；
    队列并发时会同时存在多个"一梦江湖"窗口，登录新角色前先记一次快照，登录后做diff
    才能准确定位"这次新开的是哪一个"，而不是随便抓一个已存在的同名窗口。
    连pid一起记，是为了清理时能认出"新窗口其实属于一个老进程"（见close_new_clx_windows）。"""
    windows = findwindows.find_elements(title_re=f'.*{TITLES["clx"]}.*', backend="uia")
    snapshot = {}
    for w in windows:
        try:
            _, pid = win32process.GetWindowThreadProcessId(w.handle)
        except Exception:
            pid = None
        snapshot[w.handle] = pid
    return snapshot


def close_new_clx_windows(existing_windows):
    """关闭快照（snapshot_clx_windows的结果）之后新出现的"一梦江湖"窗口，返回关闭的窗口数。
    只关新handle，并跳过pid已经出现在快照里的窗口——并发挂机中的其他角色如果弹出新的同名窗口，
    它属于老进程，一旦误杀就把人家正在跑的客户端一起干掉了。"""
    existing_pids = {pid for pid in existing_windows.values() if pid}
    closed = 0
    for handle, pid in snapshot_clx_windows().items():
        if handle in existing_windows:
            continue
        if pid and pid in existing_pids:
            logger.info("一梦江湖窗口(handle=%s)属于本次之前就在运行的客户端(pid=%s)，跳过关闭", handle, pid)
            continue
        try:
            Application(backend="uia").connect(handle=handle).kill()
            closed += 1
            logger.info("已关闭本次新打开的一梦江湖窗口（handle=%s, pid=%s）", handle, pid)
        except Exception as e:
            logger.error("关闭一梦江湖窗口失败（handle=%s, pid=%s）: %s", handle, pid, e)
    return closed


def wait_for_new_clx_window(existing_windows, timeout=TIMEOUTS["new_game_window"], interval=TIMEOUTS["poll_interval"]):
    """轮询等待出现一个handle不在existing_windows（snapshot_clx_windows的结果）里的新
    "一梦江湖"窗口，返回其pid；超时返回None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        windows = findwindows.find_elements(title_re=f'.*{TITLES["clx"]}.*', backend="uia")
        for w in windows:
            if w.handle not in existing_windows:
                _, pid = win32process.GetWindowThreadProcessId(w.handle)
                return pid
        time.sleep(interval)
    return None


def setup_role_for_queue(role, row_index):
    """队列模式下单个角色的完整setup流程：登录、选角色、进入游戏、在荼蘼第row_index行
    注册脚本并点击开始。返回新打开的游戏客户端pid，供后续超时/异常退出时精确关闭该窗口用。

    流程中任何一步失败（登录界面的图片识别不到、荼蘼里选不到脚本等），都要先把这一次
    新开出来的游戏窗口和辅助进程清理掉再把异常抛给调度器：调度器会把角色重新排队重试，
    残留的窗口不关掉，重试几轮就会堆出一屏幕"一梦江湖"，既占内存也会干扰后续图像识别。"""
    ctx = RoleLaunchContext()
    try:
        return _setup_role_steps(role, row_index, ctx)
    except Exception:
        logger.warning("角色 %s 的setup流程失败，开始清理本次新打开的窗口/进程", role['name'])
        try:
            ctx.cleanup(role['name'])
        except Exception:
            logger.exception("角色 %s setup失败后的清理过程本身出错，可能仍有窗口残留", role['name'])
        raise


def _setup_role_steps(role, row_index, ctx):
    """setup的具体步骤，启动了什么都记到ctx里，失败清理由setup_role_for_queue统一负责"""
    points = POINTS
    images = IMAGES

    open_clx_and_login(role, ctx)
    window_pid = wait_for_new_clx_window(ctx.existing_clx_windows)
    ctx.window_pid = window_pid
    if not window_pid:
        logger.warning("未能捕获角色 %s 新打开的游戏客户端pid，失败时可能无法精确关闭该窗口", role['name'])

    # 点开角色选择下拉框。账号下角色较多时目标角色可能不在首屏，需要滚动翻页查找
    pyautogui.click(points["select_role"]["x"], points["select_role"]["y"])
    logger.info('已点击角色选择，即将在角色列表中滚动查找角色: %s', role['login_role_image'])
    wait_image_and_click_in_scrollable_list(role['login_role_image'],
                                            G["role_list_scroll"])
    time.sleep(DELAYS["after_role_selected"])
    # 先确定当前要进入经典服还是梦境服：如找到"梦境私服"标记，点击勾选框取消选中
    try:
        pyautogui.locateCenterOnScreen(images['dream_server'], confidence=MATCH["confidence"])
        logger.info("角色：%s检测到已选中梦境服，即将取消选中...", role['name'])
        wait_image_and_click(images['dream_checkbox'], max_retries=RETRIES["dream_checkbox"])
    except pyautogui.ImageNotFoundException:
        logger.info("角色：%s未选中梦境服，即将踏入经典服", role['name'])
    wait_image_and_click(images['role_enter_game'], max_retries=RETRIES["role_enter_game"])
    time.sleep(DELAYS["after_enter_game"])

    # 登录流程结束、要开始操作荼蘼了：先把荼蘼从游戏窗口后面拉到最前，否则下面这些
    # 按坐标的点击会全部落到盖在上面的游戏客户端上
    bring_tu_mi_to_front()
    time.sleep(DELAYS["after_page_switch"])

    # 在荼蘼里把脚本注册到分配好的行（row_index），不再假设按顺序追加。
    # 刷新按钮用图像识别定位而不是固定坐标：荼蘼窗口被移动或改变大小时坐标就会失效，
    # 认图标则只要按钮还显示在界面上就能找到
    wait_image_and_click(images['tu_mi_refresh'], max_retries=RETRIES["tu_mi_refresh"])
    row_height = config["monitor_settings"]["row"]["height"]
    interval = row_index * row_height
    x = points['script_choose_base']['x']
    y = points['script_choose_base']['y'] + interval
    # 先点击当前方案，再下拉列表，防止目前已选中要查找的方案，导致背景颜色不对找不到
    pyautogui.click(x, y)
    pyautogui.click(x, y + POINTS["script_dropdown_offset_y"])
    pyautogui.click(x, y)
    logger.info("已点击脚本的下拉列表，x：%d，y：%d", x, y)
    time.sleep(DELAYS["after_script_dropdown"])
    y = points['script_choose_base']['y'] + interval
    logger.info("即将在(0, %d, 1500, 1200)区域中寻找要运行的脚本", y)
    script_pos = pyautogui.locateCenterOnScreen(role['script_image'], region=region_of("script_list", top=y),
                                                 grayscale=True, confidence=MATCH["confidence"])
    pyautogui.click(script_pos)
    logger.info("已找到角色要运行的脚本: %s并点击", role['script_image'])
    x = points['script_run_base']['x']
    y = points['script_run_base']['y'] + interval
    pyautogui.click(x, y)
    logger.info("于 x=%d，y=%d 处点击运行脚本", x, y)

    return window_pid


def close_window_with_pywinauto(title, index):
    try:
        # 查找所有匹配的窗口
        windows = findwindows.find_elements(title_re=f".*{title}.*", backend="uia")
        if len(windows) > 0:
            if len(windows) > index:
                # 选择特定索引的窗口
                window = windows[index]
                # 连接到该窗口的应用程序
                app = Application(backend="uia").connect(handle=window.handle)
                # 强制关闭整个应用程序
                app.kill()
                logger.info("已关闭第%d个窗口", index)
        else:
            logger.info("未找到任何匹配的窗口")
    except Exception as e:
        logger.error("通过pywinauto关闭窗口时发生错误: %s", e)

def close_clx_window(index):
    close_window_with_pywinauto(TITLES["clx"], index)

def close_top_window_with_pywinauto(title):
    try:
        # 查找所有匹配的窗口
        windows = findwindows.find_elements(title_re=f".*{title}.*", backend="uia")
        if len(windows) > 0:
            window = windows[len(windows) - 1]
            # 连接到该窗口的应用程序
            app = Application(backend="uia").connect(handle=window.handle)
            # 强制关闭整个应用程序
            app.kill()
            logger.info("已关闭顶层窗口")
        else:
            logger.info("未找到任何匹配的窗口")
    except Exception as e:
        logger.error("通过pywinauto关闭窗口时发生错误: %s", e)

def close_top_clx_window():
    close_top_window_with_pywinauto(TITLES["clx"])

def close_clx_windows_and_wait():
    """检查是否有名为'一梦江湖'的窗口，如果有则关闭它们并等待30分钟"""
    windows = findwindows.find_elements(title_re=f'.*{TITLES["clx"]}.*', backend="uia")
    if len(windows) > 0:
        for window in windows:
            # 连接到该窗口的应用程序
            app = Application(backend="uia").connect(handle=window.handle)
            # 强制关闭整个应用程序
            app.kill()
        logger.info("已找到并关闭了%d个一梦江湖的窗口", len(windows))
        time.sleep(DELAYS["relogin_wait"])
    else:
        logger.info("当前没有打开的一梦江湖")

def main():
    logger.info("当前配置：%s", config)
    # 点击延迟
    pyautogui.PAUSE = DELAYS["global"]
    # 打开荼蘼
    open_script_window()
    # 确保当前没有打开的糊糊窗口
    close_clx_windows_and_wait()

    all_roles = get_roles()
    role_lookup = {role["name"]: role for role in all_roles}
    role_names = [role["name"] for role in all_roles]

    queue_state = QueueState(role_names, config["queue_settings"]["max_concurrent"])
    state_store = StateStore(config["storage_settings"])
    state_store.cleanup_old_data()
    state_store.save(datetime.date.today(), queue_state.snapshot())

    monitor = TuMiMonitor(config, queue_state, state_store, find_tu_mi_hwnd, kill_process_tree,
                           dismiss_error_popup_fn=dismiss_tu_mi_error_popup)
    monitor.start()

    scheduler = Scheduler(config, queue_state, role_lookup, setup_role_for_queue,
                           find_tu_mi_hwnd, kill_process_tree,
                           dismiss_error_popup_fn=dismiss_tu_mi_error_popup,
                           bring_tu_mi_to_front_fn=bring_tu_mi_to_front)
    try:
        scheduler.run_until_all_finished()
    finally:
        monitor.stop()
        state_store.save(datetime.date.today(), queue_state.snapshot())

    tasks = queue_state.snapshot()
    done = [t for t in tasks if t.status == TaskStatus.DONE.value]
    failed = [t for t in tasks if t.status == TaskStatus.FAILED.value]
    logger.info("当天队列运行结束：完成%d个，失败%d个（失败角色：%s）",
                len(done), len(failed), [t.role_name for t in failed])


if __name__ == "__main__":
    main()