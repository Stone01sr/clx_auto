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

def open_software(software_path):
    """使用指定路径打开软件"""
    try:
        subprocess.Popen(software_path)
        logger.info("正在启动软件: %s", software_path)
    except Exception as e:
        logger.error("启动软件失败: %s", e)



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

def find_visible_hwnd_by_pid(pid, timeout=15, interval=1):
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

def bring_window_to_front_by_pid(pid, timeout=20):
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

def dismiss_timezone_warning(timeout=15, interval=1):
    """荼蘼启动时如果检测到系统时区不是"中国北京"会弹出"警告"对话框；
    按需求点击"否"继续启动软件，不自动修改系统时区。找不到弹窗则直接返回，不影响后续流程。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        windows = findwindows.find_elements(title_re=".*警告.*", backend="uia")
        if windows:
            try:
                app = Application(backend="uia").connect(handle=windows[0].handle)
                dlg = app.window(handle=windows[0].handle)
                dlg.child_window(title="否(N)", control_type="Button").click()
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
    windows = findwindows.find_elements(title_re=".*错误.*插件版本.*", backend="uia")
    if not windows:
        return False
    dismissed = False
    for w in windows:
        try:
            app = Application(backend="uia").connect(handle=w.handle)
            dlg = app.window(handle=w.handle)
            dlg.child_window(title="确定", control_type="Button").click()
            dismissed = True
        except Exception as e:
            logger.warning("点击荼蘼异常退出错误弹窗\"确定\"按钮失败: %s", e)
    return dismissed


def ensure_script_software_open():
    """如果荼蘼未运行，则自动打开；返回荼蘼进程pid（找不到则返回None）"""
    tu_mi_pid = find_pid_by_keyword("荼蘼")
    if tu_mi_pid:
        logger.info("检测到荼蘼已在运行（pid=%s），跳过启动", tu_mi_pid)
    else:
        script_path = config["global_settings"]["script_path"]
        logger.info("未检测到荼蘼进程，正在自动启动: %s", script_path)
        try:
            os.startfile(script_path)
        except Exception as e:
            logger.error("启动荼蘼失败: %s", e)
            return None
        tu_mi_pid = wait_for_process_by_keyword("荼蘼", timeout=30)
        if not tu_mi_pid:
            logger.warning("等待超时，未检测到荼蘼进程，继续后续流程")
            return None
        # 部分电脑系统时区不是"中国北京"时，荼蘼启动过程中会弹出警告框；
        # 按需求点击"否"继续启动软件，不修改系统时区
        dismiss_timezone_warning(timeout=15)
        bring_window_to_front_by_pid(tu_mi_pid, timeout=config["global_settings"]["software_init_delay"])
    return tu_mi_pid

def open_script_window():
    tu_mi_pid = ensure_script_software_open()
    wait_image_and_click(config["global_settings"]["images"]['tu_mi_logo'])
    time.sleep(5)
    if tu_mi_pid:
        bring_window_to_front_by_pid(tu_mi_pid, timeout=5)
    else:
        script_window = pwc.getWindowsWithTitle('荼蘼')[0]
        script_window.moveTo(0, 0)
        script_window.activate()
    wait_image_and_click(config["global_settings"]["images"]['tu_mi_main'])
    pyautogui.click(123, 87)
    return pwc.getWindowsWithTitle('荼蘼')[0]

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

def wait_for_process_by_keyword(keyword, timeout=30, interval=1):
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

def minimize_window_by_pid(pid, timeout=15, interval=1):
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
    """终止指定pid及其所有子进程（idv-login可能会派生子进程，如渠道服账号管理窗口）"""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(children, timeout=5)
        parent.terminate()
        parent.wait(timeout=5)
        logger.info("已关闭idv-login进程（pid=%s）及其子进程", pid)
    except psutil.NoSuchProcess:
        logger.info("idv-login进程（pid=%s）已不存在，无需关闭", pid)
    except Exception as e:
        logger.error("关闭idv-login进程时发生错误: %s", e)

def wait_image_and_click(image_path, region=None, max_retries = 200):
    find_flag = False
    retries = 0
    while not find_flag:
        try:
            if region:
                account_field_pos = pyautogui.locateCenterOnScreen(image_path, region=region, confidence=0.8)
            else:
                account_field_pos = pyautogui.locateCenterOnScreen(image_path, confidence=0.8)
            pyautogui.click(account_field_pos)
            logger.info("已找到图片: %s并点击", image_path)
            find_flag = True
        except pyautogui.ImageNotFoundException:
            retries += 1
            time.sleep(5)
            if retries > max_retries:
                raise Exception("can not find image: %s after %d retries", image_path, retries)
            logger.info("未找到图片: %s，继续寻找...", image_path)

def open_clx_and_login(role):
    idv_pid = None
    is_channel_account = role.get('channel_account', False)
    # 渠道服账号不是官服，不能直接登录，要借助外部脚本idv
    if is_channel_account:
        run_as_admin_powershell(config["global_settings"]["idv_login_path"], '--open-ui')
        idv_pid = wait_for_process_by_keyword("idv-login", timeout=30)
        if idv_pid:
            minimize_window_by_pid(idv_pid, timeout=15)
        else:
            logger.warning("等待超时，未检测到idv-login进程，继续后续流程")
    # 打开一梦江湖
    open_software(config["global_settings"]["software_path"])
    time.sleep(20)
    logger.info("糊糊已打开...")
    # 朕知道了
    wait_image_and_click(config["global_settings"]["images"]['init_known'])
    if is_channel_account:
        wait_image_and_click(config["global_settings"]["images"]['other_account'], max_retries=10)
        wait_image_and_click(config["global_settings"]["images"]['logo'], max_retries=10)
        time.sleep(3)
        pyautogui.press('pagedown')
        pyautogui.press('pagedown')
        wait_image_and_click(config["global_settings"]["images"]['an_login'], max_retries=5)
        idv_channel_title = config["global_settings"]["titles"]["idv_channel_account"]
        an_login_windows = pwc.getWindowsWithTitle(idv_channel_title)
        if len(an_login_windows) > 0:
            an_login_windows[0].minimize()
        time.sleep(10)
        # 渠道服账号登录完成，关闭idv-login，官服账号不依赖该进程，不受影响
        if idv_pid:
            kill_process_tree(idv_pid)
    else:
        # 截图账号下拉框倒三角，确定region，wait_image_and_click(2100, 980, 2220, 1100)
        wait_image_and_click(config["global_settings"]["images"]['account_selection'],
                                region=(2100, 980, 2220, 1100), max_retries=5)
        find_flag = False
        count_down_times = 0
        while not find_flag:
            # 提前截取好账号输入框的图片，保存为 'account_field.png'
            try:
                account_field_pos = pyautogui.locateCenterOnScreen(role["account_image"], confidence=0.8)
                pyautogui.click(account_field_pos)
                logger.info("已找到账号选择框")
                find_flag = True
            except pyautogui.ImageNotFoundException:
                logger.info("未找到账号选择框，继续寻找...")
                if count_down_times == 0:
                    count_times = 6
                else:
                    count_times = randint(1, 3)
                # 向下滚动
                for i in range(count_times):
                    pyautogui.press('down')
                count_down_times += 1
                if count_down_times > 20:
                    raise Exception("can not find account: %s", role["name"])
        # 点击登录
        wait_image_and_click(config["global_settings"]["images"]['login_enter_game'], max_retries=5)
    time.sleep(5)


def find_tu_mi_hwnd():
    """定位当前唯一一个荼蘼实例的窗口hwnd，找不到返回None"""
    pid = find_pid_by_keyword("荼蘼")
    if not pid:
        return None
    return find_visible_hwnd_by_pid(pid, timeout=5)


def snapshot_clx_window_handles():
    """获取当前所有"一梦江湖"窗口的handle集合。一梦江湖的可执行文件是Launcher.exe，
    路径里不含中文关键字，没法像荼蘼那样按exe路径关键字找pid，只能按窗口标题识别；
    队列并发时会同时存在多个"一梦江湖"窗口，登录新角色前先记一次快照，登录后做diff
    才能准确定位"这次新开的是哪一个"，而不是随便抓一个已存在的同名窗口。"""
    windows = findwindows.find_elements(title_re=".*一梦江湖.*", backend="uia")
    return {w.handle for w in windows}


def wait_for_new_clx_window(existing_handles, timeout=30, interval=1):
    """轮询等待出现一个不在existing_handles中的新"一梦江湖"窗口，返回其pid；超时返回None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        windows = findwindows.find_elements(title_re=".*一梦江湖.*", backend="uia")
        for w in windows:
            if w.handle not in existing_handles:
                _, pid = win32process.GetWindowThreadProcessId(w.handle)
                return pid
        time.sleep(interval)
    return None


def setup_role_for_queue(role, row_index):
    """队列模式下单个角色的完整setup流程：登录、选角色、进入游戏、在荼蘼第row_index行
    注册脚本并点击开始。返回新打开的游戏客户端pid，供失败时精确关闭该窗口用。"""
    points = config["global_settings"]["points"]
    images = config["global_settings"]["images"]

    existing_handles = snapshot_clx_window_handles()
    open_clx_and_login(role)
    window_pid = wait_for_new_clx_window(existing_handles, timeout=30)
    if not window_pid:
        logger.warning("未能捕获角色 %s 新打开的游戏客户端pid，失败时可能无法精确关闭该窗口", role['name'])

    # 点开角色选择下拉框
    pyautogui.click(points["select_role"]["x"], points["select_role"]["y"])
    logger.info('已点击角色选择，即将点击角色图片')
    wait_image_and_click(role['login_role_image'], max_retries=5)
    time.sleep(10)
    # 先确定当前要进入经典服还是梦境服：如找到"梦境私服"标记，点击勾选框取消选中
    try:
        pyautogui.locateCenterOnScreen(images['dream_server'], confidence=0.8)
        logger.info("角色：%s检测到已选中梦境服，即将取消选中...", role['name'])
        wait_image_and_click(images['dream_checkbox'], max_retries=1)
    except pyautogui.ImageNotFoundException:
        logger.info("角色：%s未选中梦境服，即将踏入经典服", role['name'])
    wait_image_and_click(images['role_enter_game'], max_retries=5)
    time.sleep(5)

    # 在荼蘼里把脚本注册到分配好的行（row_index），不再假设按顺序追加
    pyautogui.click(points['script_refresh']['x'], points['script_refresh']['y'])
    row_height = config["monitor_settings"]["row"]["height"]
    interval = row_index * row_height
    x = points['script_choose_base']['x']
    y = points['script_choose_base']['y'] + interval
    # 先点击当前方案，再下拉列表，防止目前已选中要查找的方案，导致背景颜色不对找不到
    pyautogui.click(x, y)
    pyautogui.click(x, y + 30)
    pyautogui.click(x, y)
    logger.info("已点击脚本的下拉列表，x：%d，y：%d", x, y)
    time.sleep(3)
    y = points['script_choose_base']['y'] + interval
    logger.info("即将在(0, %d, 1500, 1200)区域中寻找要运行的脚本", y)
    script_pos = pyautogui.locateCenterOnScreen(role['script_image'], region=(0, y, 1500, 1200),
                                                 grayscale=True, confidence=0.8)
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
    close_window_with_pywinauto("一梦江湖", index)

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
    close_top_window_with_pywinauto("一梦江湖")

def close_clx_windows_and_wait():
    """检查是否有名为'一梦江湖'的窗口，如果有则关闭它们并等待30分钟"""
    windows = findwindows.find_elements(title_re=f".*{'一梦江湖'}.*", backend="uia")
    if len(windows) > 0:
        for window in windows:
            # 连接到该窗口的应用程序
            app = Application(backend="uia").connect(handle=window.handle)
            # 强制关闭整个应用程序
            app.kill()
        logger.info("已找到并关闭了%d个一梦江湖的窗口", len(windows))
        time.sleep(1800)
    else:
        logger.info("当前没有打开的一梦江湖")

def main():
    logger.info("当前配置：%s", config)
    # 点击延迟
    pyautogui.PAUSE = config['global_settings']['global_delay']
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
                           dismiss_error_popup_fn=dismiss_tu_mi_error_popup)
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