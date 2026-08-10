import datetime
import os
import re
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
from tu_mi_queue.ui_lock import tu_mi_ui
from tu_mi_queue.log_setup import setup_logging

logger = logging.getLogger(__name__)
today = datetime.datetime.now()
# 主流程和监控线程并发写同一份日志，格式里带线程标签列，控制台上还会给这一列上色
setup_logging(log_file=f"app_{today:%Y-%m-%d}.log")

with open("config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# 配置分段的快捷引用。代码里不写死任何坐标、标题、超时和阈值，一律从这里取，
# 这样换机器、换分辨率、游戏或荼蘼改版时只需要改 config.yaml
G = config["global_settings"]
POINTS = G["points"]
REGIONS = G["regions"]
IMAGES = G["images"]
TITLES = G["titles"]
POPUP = G["tu_mi_popup"]
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

def find_visible_hwnd_by_pid(pid, timeout=TIMEOUTS["find_window"], interval=TIMEOUTS["poll_interval"],
                             title_keyword=None):
    """通过进程pid轮询查找其可见顶层窗口，返回hwnd，超时返回None。

    title_keyword不为空时只认标题含该关键字的窗口。荼蘼必须传这个参数：它弹出模态对话框时，
    对话框也是这个进程下的可见顶层窗口，而且通常排在枚举结果的最前面，不筛标题就会把
    弹框当成主窗口——截图截到的是弹框、置前置的是弹框、后面按坐标的点击全部落空。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found_hwnd = []
        def _enum_handler(hwnd, _):
            title = win32gui.GetWindowText(hwnd)
            if win32gui.IsWindowVisible(hwnd) and title:
                if title_keyword and title_keyword not in title:
                    return
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid == pid:
                    found_hwnd.append(hwnd)
        win32gui.EnumWindows(_enum_handler, None)
        if found_hwnd:
            return found_hwnd[0]
        time.sleep(interval)
    return None

def _attach_thread_input(from_thread, to_thread, attach):
    """AttachThreadInput的安全包装，返回是否真的接上了。

    两个坑：
    1）pywin32里这个函数成功时返回None、失败时抛异常，所以不能拿返回值当"成功"判断
       （原来就是这么写的，结果是接上了却永远不会断开，输入队列一直挂着）；
    2）它失败的原因五花八门，而且大多不是我们能控制的：前台窗口属于提权进程或UAC安全桌面、
       那个线程没有消息队列、线程刚好退出了——都会报 (87, '参数错误')。
    接不上不是致命问题，后面的BringWindowToTop/SetForegroundWindow照样可以试，所以这里只记日志。"""
    if not from_thread or not to_thread or from_thread == to_thread:
        return False
    try:
        win32process.AttachThreadInput(from_thread, to_thread, attach)
        return True
    except Exception as e:
        logger.info("AttachThreadInput(%s -> %s, attach=%s) 失败，跳过这一步继续置前: %s",
                    from_thread, to_thread, attach, e)
        return False

def _raise_window_by_topmost(hwnd):
    """SetForegroundWindow被系统拒绝时的退路：把窗口临时设成置顶再取消置顶。
    这样虽然拿不到键盘焦点，但能把它抬到Z序最前、不被别的窗口盖住——
    而我们真正需要的是"鼠标点到那个坐标时点中的是荼蘼"，靠的正是Z序而不是焦点。"""
    try:
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        return True
    except Exception as e:
        logger.warning("用置顶方式抬升窗口也失败（hwnd=%s）: %s", hwnd, e)
        return False

def force_window_foreground(hwnd):
    """将窗口强制置于最前并激活，返回它最终是不是前台窗口。
    Windows有前台锁定限制：非当前前台线程直接调用SetForegroundWindow经常被系统静默忽略，
    窗口API层面显示visible=True，但实际停在原位、被其他窗口盖住，用户看不到——
    通过AttachThreadInput临时把自己的输入线程"接"到目标窗口线程上，可以绕过该限制。
    整套动作里的每一步都可能被系统拒绝，所以逐步降级：接线程 -> 常规置前 -> 临时置顶抬升，
    最后以"当前前台窗口到底是不是它"为准，而不是以"哪个API没抛异常"为准。"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception as e:
        logger.warning("显示/还原窗口失败（hwnd=%s）: %s", hwnd, e)

    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
    target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    cur_thread = win32api.GetCurrentThreadId()

    attached_fg = _attach_thread_input(fg_thread, target_thread, True)
    attached_cur = _attach_thread_input(cur_thread, target_thread, True)
    try:
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            logger.info("常规置前被系统拒绝（hwnd=%s）: %s，改用临时置顶的方式抬升", hwnd, e)
            _raise_window_by_topmost(hwnd)
    finally:
        # 一定要断开，否则输入队列一直挂在目标线程上，会影响后续的鼠标键盘行为
        if attached_fg:
            _attach_thread_input(fg_thread, target_thread, False)
        if attached_cur:
            _attach_thread_input(cur_thread, target_thread, False)

    if win32gui.GetForegroundWindow() != hwnd:
        # 前台切换有一点延迟，给它一次确认的机会，还不行就再用置顶兜一次
        time.sleep(TIMEOUTS["foreground_settle"])
        if win32gui.GetForegroundWindow() != hwnd:
            _raise_window_by_topmost(hwnd)

    ok = win32gui.GetForegroundWindow() == hwnd
    if ok:
        logger.info("已将窗口置于前台，标题: %s", win32gui.GetWindowText(hwnd))
    else:
        logger.warning("窗口未能拿到前台焦点（hwnd=%s，标题: %s），已尽力抬到最上层；"
                       "若后续按坐标的点击落到了别的窗口，多半就是这里没成功",
                       hwnd, win32gui.GetWindowText(hwnd))
    return ok

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

def bring_hwnd_to_front(hwnd):
    """把指定窗口移到屏幕左上角、修正透明度异常并强制置于前台。
    移到(0,0)是硬性要求：所有点位都是按窗口左上角贴屏幕左上角标定的"""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        win32gui.MoveWindow(hwnd, 0, 0, right - left, bottom - top, True)
    except Exception as e:
        logger.warning("移动窗口位置失败（hwnd=%s）: %s", hwnd, e)
    force_window_opaque(hwnd)
    return force_window_foreground(hwnd)

def bring_window_to_front_by_pid(pid, timeout=TIMEOUTS["bring_to_front"], title_keyword=None):
    """按pid定位窗口（不依赖截图），移到左上角、修正透明度异常并强制置于前台。
    title_keyword的作用见find_visible_hwnd_by_pid：有弹框的进程必须靠标题挑出主窗口，
    否则会把弹框拖到(0,0)并置前，主窗口反而还在后面。"""
    hwnd = find_visible_hwnd_by_pid(pid, timeout=timeout, title_keyword=title_keyword)
    if not hwnd:
        logger.warning("等待%s秒仍未找到pid=%s的可见窗口，跳过置前", timeout, pid)
        return False
    return bring_hwnd_to_front(hwnd)

def find_hwnd_by_pid_and_title(pid, title_keyword):
    """按pid+窗口标题关键字找顶层窗口，返回(hwnd, 当前是否可见)；找不到返回(None, False)。

    和find_visible_hwnd_by_pid的区别：这个连"隐藏"的窗口也认。软件最小化到托盘时，
    主窗口并不是被最小化，而是被ShowWindow(SW_HIDE)藏了起来——IsWindowVisible返回False，
    但窗口对象一直都在，直接SW_SHOW就能把它请回来，根本不用去屏幕上戳托盘图标。
    荼蘼进程下有二十多个顶层窗口（WinForms的各种tooltip/隐藏容器），只有主窗口带标题，
    所以用标题关键字就能准确挑出它。"""
    matched = []

    def _enum_handler(hwnd, _):
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid != pid:
            return
        title = win32gui.GetWindowText(hwnd)
        if title and title_keyword in title:
            matched.append((hwnd, bool(win32gui.IsWindowVisible(hwnd))))

    win32gui.EnumWindows(_enum_handler, None)
    if not matched:
        return None, False
    # 可见的优先（主窗口已经显示出来时，不要挑到某个同名的隐藏残留窗口）
    matched.sort(key=lambda item: not item[1])
    return matched[0]

def show_tu_mi_window(pid, timeout=TIMEOUTS["find_window"], interval=TIMEOUTS["poll_interval"]):
    """把荼蘼主窗口显示出来并尽量置前，返回的是"窗口拿到了没"，不是"置前成功了没"。
    已经显示着就直接置前，被藏到托盘就先唤出来再置前。
    这条路取代了"在屏幕上找托盘图标点一下"——托盘图标可能被折叠进"显示隐藏的图标"里、
    可能在另一块屏幕上、不同缩放比例下还可能认不出来，而窗口句柄一直是稳的。

    置前失败（系统前台锁定、AttachThreadInput被拒等）不算这一步失败：窗口明明已经在了，
    这时候退回去满屏找托盘图标只会白等一场、最后报"荼蘼没找到"。置前那点问题后面还有
    好几次机会补救（每次操作荼蘼之前都会再置前一次）。"""
    deadline = time.time() + timeout
    while True:
        hwnd, visible = find_hwnd_by_pid_and_title(pid, TITLES["tu_mi"])
        if hwnd:
            if not visible:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                logger.info("荼蘼主窗口处于隐藏状态（多半是最小化到了托盘），已直接唤出（hwnd=%s）", hwnd)
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            if not bring_hwnd_to_front(hwnd):
                logger.warning("荼蘼主窗口（hwnd=%s）已显示出来，但没能确认置于最前，继续后续流程", hwnd)
            return True
        if time.time() >= deadline:
            logger.warning("等待%s秒仍未找到pid=%s下标题含\"%s\"的窗口", timeout, pid, TITLES["tu_mi"])
            return False
        time.sleep(interval)

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

def list_tu_mi_popups(pid):
    """列出荼蘼当前弹出的所有对话框，返回[(hwnd, 标题)]。

    判据是"本进程下可见且有标题、标题里又不含'荼蘼'的顶层窗口"。荼蘼进程下有二十多个
    顶层窗口，但除主窗口外全是WinForms的无标题隐藏容器/tooltip，所以带标题的非主窗口
    必然是弹出来的对话框。用这条通用判据而不是给每种弹框配一条标题正则：荼蘼的弹框种类
    没有穷举清单（"错误/插件版本"、"此窗口已存在于中控"、时区警告……），配漏一种就会像
    2026-08-09 那样整条流程被一个没人认识的框挡住几个小时。"""
    popups = []

    def _enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or TITLES["tu_mi"] in title:
            return
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid == pid:
            popups.append((hwnd, title))

    win32gui.EnumWindows(_enum_handler, None)
    return popups


def _popup_buttons_for(title):
    """按标题挑该弹框应该点的按钮候选：命中overrides用它的，否则用默认的一组"""
    for rule in POPUP.get("overrides") or []:
        if re.search(rule["title_re"], title):
            return rule["buttons"]
    return POPUP["buttons"]


def _popup_message(dlg):
    """尽量读出弹框正文，只用于日志——遇到没见过的弹框时，光有标题（往往就俩字"错误"）
    根本认不出是什么，正文才说得清"""
    try:
        texts = [t.strip() for t in dlg.children_texts() if t and t.strip()]
        return " / ".join(texts[:4])
    except Exception:
        return ""


def _dismiss_one_popup(hwnd, title):
    """关掉单个弹框，返回是否关掉了"""
    buttons = _popup_buttons_for(title)
    message = ""
    try:
        app = Application(backend="uia").connect(handle=hwnd)
        dlg = app.window(handle=hwnd)
        message = _popup_message(dlg)
        for button in buttons:
            try:
                dlg.child_window(title=button, control_type="Button").click()
                logger.warning("已关闭荼蘼弹框「%s」（点击\"%s\"）：%s", title, button, message)
                return True
            except Exception:
                continue
        logger.warning("荼蘼弹框「%s」上找不到%s中的任何按钮：%s", title, buttons, message)
    except Exception as e:
        logger.warning("连接荼蘼弹框「%s」失败: %s", title, e)

    if not POPUP.get("close_if_no_button", True):
        return False
    # 兜底：直接发关闭消息。对是/否这类对话框，Windows本来就把标题栏的X置灰、WM_CLOSE不生效，
    # 所以这条兜底不会替我们误答"是"，最坏情况就是没关掉、下一轮再试
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(TIMEOUTS["poll_interval"])
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            logger.warning("已用WM_CLOSE关闭荼蘼弹框「%s」：%s", title, message)
            return True
        logger.warning("荼蘼弹框「%s」按钮点不到、WM_CLOSE也没关掉，可能需要人工处理：%s", title, message)
    except Exception as e:
        logger.warning("向荼蘼弹框「%s」发送WM_CLOSE失败: %s", title, e)
    return False


def dismiss_tu_mi_popups():
    """关掉荼蘼当前所有弹框，返回是否至少关掉一个。

    弹框都是模态的：只要挂着一个，整个荼蘼就点不动，按坐标的点击会落到弹框上，
    截图识别到的也是弹框而不是面板——必须在每次操作/识别荼蘼之前先清干净。
    单次检查、不等待，由调用方（监控轮询、扫描空闲行前、点完"开始"之后）按自己的节奏重复调用；
    一次点掉所有弹框，避免多个角色同时出问题时堆叠了好几个框。"""
    pid = find_pid_by_keyword(PROC["tu_mi"])
    if not pid:
        return False
    dismissed = False
    for hwnd, title in list_tu_mi_popups(pid):
        if _dismiss_one_popup(hwnd, title):
            dismissed = True
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
        bring_window_to_front_by_pid(tu_mi_pid, timeout=DELAYS["software_init"],
                                     title_keyword=TITLES["tu_mi"])
    return tu_mi_pid

def open_script_window():
    """把荼蘼准备到"日常"页并置于前台，返回主窗口hwnd（实在找不到窗口时返回None）。

    唤出主界面有两条路，优先走窗口句柄那条：
    1）按pid+标题直接找主窗口，藏起来就SW_SHOW唤出、最小化就还原，然后置前——不依赖屏幕内容；
    2）实在找不到窗口才退回到"在屏幕上认托盘图标点一下"。托盘这条路很脆：图标可能被折叠进
       "显示隐藏的图标"的溢出面板里根本不在屏幕上、可能在另一块屏幕上、缩放比例变了还可能认不出来，
       所以只当兜底，不再当主路径。"""
    tu_mi_pid = ensure_script_software_open()
    shown = show_tu_mi_window(tu_mi_pid) if tu_mi_pid else False
    if not shown:
        logger.warning("未能直接唤出荼蘼主窗口，退回到点击托盘图标的方式（若图标被折叠在"
                       "\"显示隐藏的图标\"里，请把它拖到托盘常显区域）")
        wait_image_and_click(IMAGES['tu_mi_logo'], max_retries=RETRIES["tu_mi_logo"])
        time.sleep(DELAYS["after_tu_mi_logo"])
        if tu_mi_pid:
            bring_window_to_front_by_pid(tu_mi_pid, timeout=TIMEOUTS["tu_mi_front"],
                                         title_keyword=TITLES["tu_mi"])

    wait_image_and_click(IMAGES['tu_mi_main'])
    pyautogui.click(POINTS["tu_mi_daily_menu"]["x"], POINTS["tu_mi_daily_menu"]["y"])
    logger.info("已切到荼蘼\"日常\"页，荼蘼准备就绪")
    hwnd, _ = find_hwnd_by_pid_and_title(tu_mi_pid, TITLES["tu_mi"]) if tu_mi_pid else (None, False)
    return hwnd

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

def _dedupe_boxes(boxes):
    """模板匹配同一个控件时通常会命中一小簇几乎重合的框（彼此只差一两个像素），
    按中心点距离去重，每一簇只留第一个。不去重的话"页面上有几个按钮"根本没法数，
    日志里也会刷出十几个看着像不同按钮、其实是同一个的坐标。"""
    kept = []
    for box in boxes:
        center_x, center_y = box.left + box.width / 2, box.top + box.height / 2
        if any(abs(center_x - (k.left + k.width / 2)) < k.width / 2 and
               abs(center_y - (k.top + k.height / 2)) < k.height / 2 for k in kept):
            continue
        kept.append(box)
    return kept


def locate_row_button(row_image, button_image, confidence=MATCH["confidence"]):
    """在"一行一条记录、左边是标识右边是操作按钮"的列表里，找出row_image所在那一行的
    按钮中心坐标，行没找到或那一行上没有按钮都返回None。

    渠道服账号管理页面就是这种列表：一个账号一行，行尾是"登录"按钮。原来的做法是
    "在屏幕上找到登录按钮就点"，只有一个渠道服账号时碰巧总是对的，页面上一旦多出一个账号
    就会登错人。这里改成先按账号图片定位到行，再在同一行里挑按钮：按钮和账号在纵向上必然
    居中对齐，横向上按钮在账号右侧——只靠这两个位置关系配对，不写死任何像素偏移，
    所以窗口大小、是否全屏、页面滚到第几屏都不影响。

    两次查找共用同一张截图：分别截两次的话，中间页面要是滚动了一下，
    就会拿A屏的行去配B屏的按钮，配出来的坐标是错的。"""
    screen = pyautogui.screenshot()
    try:
        row_box = pyautogui.locate(row_image, screen, confidence=confidence)
    except pyautogui.ImageNotFoundException:
        return None
    try:
        buttons = _dedupe_boxes(pyautogui.locateAll(button_image, screen, confidence=confidence))
    except pyautogui.ImageNotFoundException:
        buttons = []

    # 按钮中心落在"行图片的纵向范围±容差"之内就算同一行。容差要远小于行高：
    # 大了会把相邻行的按钮认进来（那才是真正会登错账号的情况），小了则容不下
    # 截图时上下多框/少框的那几个像素
    tolerance = MATCH["row_align_tolerance_px"]
    top = row_box.top - tolerance
    bottom = row_box.top + row_box.height + tolerance
    same_row = [b for b in buttons
                if top <= b.top + b.height / 2 <= bottom and b.left >= row_box.left]
    logger.info("已定位到图片 %s 所在的行（top=%s, height=%s），页面上共%d个 %s 按钮，其中%d个与该行对齐",
                row_image, row_box.top, row_box.height, len(buttons), button_image, len(same_row))
    if not same_row:
        return None
    # 同一行里可能还有别的按钮，取最靠近行标识的那个
    return pyautogui.center(min(same_row, key=lambda b: b.left))


def click_channel_account_login(role):
    """在渠道服账号管理页面里点该角色对应账号那一行的"登录"按钮，找不到抛异常。

    账号不在首屏时逐页往下翻着找。这个页面是idv-login弹出的Chrome窗口，翻页用PageDown。"""
    account_image = role.get("account_image")
    login_image = IMAGES["channel_login"]
    max_pagedowns = RETRIES["channel_login"]

    if not account_image:
        # 兼容还没配账号图片的老配置：退回"看见登录按钮就点"，页面上只有一个账号时结果一样
        logger.warning("角色 %s 未配置渠道服账号图片，退回直接点击页面上的登录按钮"
                       "（页面上有多个账号时会登错账号，建议用角色管理助手补截一张）", role["name"])
        for _ in range(G["account_list_scroll"]["channel_pagedown_count"]):
            pyautogui.press('pagedown')
        wait_image_and_click(login_image, max_retries=max_pagedowns)
        return

    for page in range(max_pagedowns + 1):   # 第0次先在当前可见范围找，之后每翻一页找一次
        pos = locate_row_button(account_image, login_image)
        if pos:
            pyautogui.click(pos)
            logger.info("已点击角色 %s 在渠道服账号管理页面所在行的登录按钮，坐标: %s", role["name"], pos)
            return
        if page < max_pagedowns:
            logger.info("渠道服账号管理页面第%d屏未找到角色 %s 对应的登录按钮（账号图片: %s），"
                        "向下翻一页继续找", page + 1, role["name"], account_image)
            pyautogui.press('pagedown')
            time.sleep(DELAYS["after_page_switch"])
    raise Exception(f"在渠道服账号管理页面翻找{max_pagedowns + 1}屏，"
                    f"仍未找到角色 {role['name']} 对应的登录按钮")


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


def wait_image_and_click_in_scrollable_list(image_path, scroll_cfg, confidence=MATCH["confidence"],
                                            open_list_fn=None):
    """在可滚动列表中查找并点击图片，找不到抛异常（交由上层按失败重试处理）。
    和wait_image_and_click的区别：那个只在当前屏原地重试，角色不在首屏时永远找不到；
    这个会逐屏向下翻找，整轮扫完仍没找到就滚回顶部再来一轮（应对列表还没渲染完的情况）。

    open_list_fn：每一轮开始前调用，负责把列表（重新）展开。列表是靠一次固定坐标的点击展开的，
    这一下要是没生效（界面还没渲染完、点击被吞掉），列表压根没打开，后面滚多少屏都是白滚，
    整轮整轮地失败——所以每轮都重新展开一次，而不是只在第一轮之前点那一下。
    注意下拉框的点击是"开/关"切换：万一列表本来就是开着的，这一下会把它关上，
    下一轮再点回来，所以 max_rounds 要留出富余（配置里给了4轮）。"""
    max_rounds = scroll_cfg["max_rounds"]
    for round_index in range(max_rounds):
        if open_list_fn:
            open_list_fn(round_index)
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


def save_failure_screenshot(role_name):
    """setup失败时截一张全屏存到 screenshots/<日期>/setup_fail_<角色>_<时分秒>.png。
    这类失败几乎都是"界面不是我以为的样子"，日志只能看到"图片没找到"，
    有这张图才能一眼看出当时到底卡在哪个界面（列表没展开？弹了公告？还在加载？）"""
    try:
        moment = datetime.datetime.now()
        day_dir = os.path.join(config["storage_settings"]["screenshot_dir"], f"{moment:%Y-%m-%d}")
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, f"setup_fail_{role_name}_{moment:%H%M%S}.png")
        pyautogui.screenshot().save(path)
        logger.info("已保存角色 %s 的失败现场截图: %s", role_name, path)
        return path
    except Exception as e:
        logger.warning("保存角色 %s 的失败现场截图失败: %s", role_name, e)
        return None


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
        save_failure_screenshot(role_name)   # 关窗口之前先留证据，否则事后完全看不出当时界面卡在哪
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
        click_channel_account_login(role)
        idv_channel_title = TITLES["idv_channel_account"]
        channel_windows = pwc.getWindowsWithTitle(idv_channel_title)
        if len(channel_windows) > 0:
            channel_windows[0].minimize()
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
    ok = bring_window_to_front_by_pid(pid, timeout=TIMEOUTS["tu_mi_front"],
                                      title_keyword=TITLES["tu_mi"])
    if not ok:
        logger.warning("操作荼蘼前置前窗口失败（pid=%s），后续点击可能落到别的窗口上", pid)
    return ok


def find_tu_mi_hwnd():
    """定位当前唯一一个荼蘼实例的主窗口hwnd，找不到返回None。
    按标题筛，不能只按pid拿"第一个可见窗口"：荼蘼弹出模态框时那个框也是本进程的可见窗口"""
    pid = find_pid_by_keyword(PROC["tu_mi"])
    if not pid:
        return None
    return find_visible_hwnd_by_pid(pid, timeout=TIMEOUTS["find_tu_mi_window"],
                                    title_keyword=TITLES["tu_mi"])


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

    scroll_cfg = G["role_list_scroll"]

    def open_role_list(round_index):
        """展开角色选择下拉框，等列表渲染出来，再把鼠标从下拉框上挪开。
        点完之后光标就停在下拉框/列表项上，鼠标指针本身和悬停高亮会盖住那一条，
        截图匹配自然认不出来；往右上挪开一点，列表就恢复成截图时的样子了。
        每一轮滚动查找前都重新展开一次，避免这一下被吞掉之后白滚一整轮。"""
        x = points["select_role"]["x"]
        y = points["select_role"]["y"]
        pyautogui.click(x, y)
        time.sleep(scroll_cfg["open_wait_sec"])
        offset = scroll_cfg["open_offset"]
        pyautogui.moveTo(x + offset["x"], y + offset["y"])
        logger.info("第%d轮：已点击角色选择下拉框并把鼠标移开到(%d, %d)，即将在角色列表中滚动查找角色: %s",
                    round_index + 1, x + offset["x"], y + offset["y"], role['login_role_image'])

    wait_image_and_click_in_scrollable_list(role['login_role_image'], scroll_cfg,
                                            open_list_fn=open_role_list)
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

    register_script_in_tu_mi(role, row_index)

    return window_pid


def register_script_in_tu_mi(role, row_index):
    """在荼蘼第row_index行选好该角色要跑的方案并点"开始"。

    整段（置前 -> 刷新 -> 展开方案下拉列表 -> 认出方案 -> 点中 -> 点开始）都在荼蘼界面锁里做，
    是原子的：展开的下拉列表是个很脆弱的临时状态，监控线程点掉错误弹窗、或者别处把窗口
    置前/截图，都会让它收起来，紧接着的认图就会失败。

    单次失败（最典型的就是列表被盖住/被收起来）不直接判角色失败——那要整个重新登录一次，
    代价太大——先原地把整段重做一遍，重试次数用完了才把异常抛给调度器。"""
    attempts = RETRIES["tu_mi_script_select"]
    with tu_mi_ui(f"角色{role['name']}在荼蘼第{row_index}行注册方案"):
        for attempt in range(1, attempts + 1):
            try:
                _register_script_once(role, row_index)
                return
            except Exception as e:
                if attempt >= attempts:
                    raise
                logger.warning("第%d/%d次在荼蘼第%d行为角色 %s 选择方案失败，原地重做整段: %s",
                               attempt, attempts, row_index, role['name'], e)
                time.sleep(DELAYS["after_page_switch"])


def _register_script_once(role, row_index):
    """注册方案的单次尝试，调用方负责持有荼蘼界面锁并按需重试"""
    points = POINTS
    images = IMAGES

    # 荼蘼的弹框是模态的，挂着一个后面所有点击都会落到弹框上（或者干脆被吞掉），
    # 所以整段操作开始之前先把弹框清干净，再置前
    dismiss_tu_mi_popups()
    # 先把荼蘼从游戏窗口后面拉到最前，否则下面这些按坐标的点击会全部落到盖在上面的游戏客户端上
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

    # 点"开始"是最容易弹框的一步（典型的是"此窗口已存在于中控，请直接选择对应窗口开始！"，
    # 意思是这一次的开始没生效）。弹框先关掉，再把这次尝试判失败让外层重做整段——
    # 重做的第一步正好是点刷新，也就是弹框自己给出的处理办法，而且不用重新登录角色
    time.sleep(DELAYS["after_page_switch"])
    if dismiss_tu_mi_popups():
        raise RuntimeError("点击\"开始\"后荼蘼弹出了对话框，本次开始未生效（详见上一条弹框日志）")


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
                           dismiss_popups_fn=dismiss_tu_mi_popups)
    monitor.start()

    scheduler = Scheduler(config, queue_state, role_lookup, setup_role_for_queue,
                           find_tu_mi_hwnd, kill_process_tree,
                           dismiss_popups_fn=dismiss_tu_mi_popups,
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