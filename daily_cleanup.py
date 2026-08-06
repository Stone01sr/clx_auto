import datetime
import sys
from random import randint

import pyautogui
import subprocess
import time
import pywinctl as pwc
import yaml
import logging
from pywinauto import Application, findwindows

logger = logging.getLogger(__name__)
today = datetime.datetime.now()
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),  # 输出到控制台
        logging.FileHandler(f"app_{today:%Y-%m-%d}.log")      # 输出到文件
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
        logger.error("启动软件失败", e)



def open_script_window():
    wait_image_and_click(config["global_settings"]["images"]['tu_mi_logo'])
    time.sleep(5)
    script_window = pwc.getWindowsWithTitle('荼蘼')[0]
    script_window.moveTo(0, 0)
    script_window.activate()
    wait_image_and_click(config["global_settings"]["images"]['tu_mi_main'])
    pyautogui.click(123, 87)
    return script_window

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
    # 桉桉不是官服，不能直接登录，要借助外部脚本idv
    if role['name'] == "an_an":
        run_as_admin_powershell(config["global_settings"]["idv_login_path"], '--mitm')
        time.sleep(20)
        idv_window = pwc.getWindowsWithTitle('C:\\Users\\74484\\Downloads\\idv-login-v5.7.3-stable-Py3.8.exe')[0]
        idv_window.minimize()
    # 打开一梦江湖
    open_software(config["global_settings"]["software_path"])
    time.sleep(20)
    logger.info("糊糊已打开...")
    # 朕知道了
    wait_image_and_click(config["global_settings"]["images"]['init_known'])
    if role['name'] == "an_an":
        wait_image_and_click(config["global_settings"]["images"]['other_account'], max_retries=10)
        wait_image_and_click(config["global_settings"]["images"]['logo'], max_retries=10)
        time.sleep(3)
        pyautogui.press('pagedown')
        pyautogui.press('pagedown')
        wait_image_and_click(config["global_settings"]["images"]['an_login'], max_retries=5)
        an_login_windows = pwc.getWindowsWithTitle('渠道服账号管理 - Google Chrome')
        if len(an_login_windows) > 0:
            an_login_windows[0].minimize()
        time.sleep(10)
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
    
    role_index = 0
    succeed = 0
    points = config["global_settings"]["points"]
    all_roles = get_roles()
    init_length = len(all_roles)
    while role_index < len(all_roles) and role_index < init_length * 2:
        role = all_roles[role_index]
        role_index += 1
        logger.info("开始挂： %s 的脚本", role["name"])
        try:
            open_clx_and_login(role)
            # 点开选择角色
            # 点开账号选择下拉框
            pyautogui.click(points["select_role"]["x"], points["select_role"]["y"])
            # select_account_pos = pyautogui.locateCenterOnScreen(config["global_settings"]["images"]['role_selection'],
            #                         region=(1740, 1200, 1850, 1280), grayscale=True, confidence=0.60)
            # pyautogui.click(select_account_pos)
            logger.info('已点击角色选择，即将点击角色图片')
            # 提前截取好角色的图片，保存为 'stone_role_login.png'
            wait_image_and_click(role['login_role_image'], max_retries=5)
            time.sleep(10)
            # 先确定当前要进入经典服还是梦境服：在屏幕上指定区域(2200, 700, 2600, 1270)查找“梦境私服”，如找到，点击勾选框
            try:
                pyautogui.locateCenterOnScreen(config["global_settings"]["images"]['dream_server'], confidence=0.8)
                logger.info("角色：%s检测到已选中梦境服，即将取消选中...", role['name'])
                wait_image_and_click(config["global_settings"]["images"]['dream_checkbox'], max_retries=1)
            except pyautogui.ImageNotFoundException:
                logger.info("角色：%s未选中梦境服，即将踏入经典服", role['name'])
            # 点击踏入江湖
            wait_image_and_click(config["global_settings"]["images"]['role_enter_game'], max_retries=5)
            time.sleep(5)
            # 脚本刷新
            pyautogui.click(points['script_refresh']['x'], points['script_refresh']['y'])
            # 脚本方案下拉列表
            interval = succeed * config['global_settings']['script_item_interval']
            x = points['script_choose_base']['x']
            y = points['script_choose_base']['y'] + interval
            pyautogui.click(x, y)
            # 先点击当前方案，再下拉列表，防止目前已选中要查找的方案，导致背景颜色不对找不到
            pyautogui.click(x, y + 30)
            pyautogui.click(x, y)
            logger.info("已点击脚本的下拉列表，x：%d，y：%d", x, y)
            time.sleep(3)
            y = points['script_choose_base']['y'] + interval
            logger.info("即将在(0, %d, 1500, 1200)区域中寻找要运行的脚本", y)
            account_field_pos = pyautogui.locateCenterOnScreen(role['script_image'],
                                                               region=(0, y, 1500, 1200),
                                                               grayscale=True, confidence=0.8)
            pyautogui.click(account_field_pos)
            logger.info("已找到角色要运行的脚本: %s并点击", role['script_image'])
            # 开始
            x = points['script_run_base']['x']
            y = points['script_run_base']['y'] + interval
            pyautogui.click(x, y)
            logger.info("于 x=%d，y=%d 处点击运行脚本", x, y)
            succeed += 1
        except Exception as e:
            # close_top_clx_window()
            # all_roles.append(role)
            logger.error("role: %s run error", role['name'], exc_info=True)
            logger.error(e)


if __name__ == "__main__":
    main()