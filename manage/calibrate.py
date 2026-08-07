"""荼蘼面板标定检查工具。

把 config.yaml 里 monitor_settings.row 的标定参数画到荼蘼窗口的实际截图上，
生成一张带标注的检查图，并逐行打印取色结果，用来确认标定是否正确。

用法：
    python manage/calibrate.py

不需要懂代码：运行后看生成的检查图，如果每一行的圆点都落在状态色块上、
方框都框住了状态文字，说明标定正确；否则按提示调整 config.yaml 里的数值再跑一次。
"""

import datetime
import os
import sys
import time

import psutil
import win32api
import win32con
import win32gui
import win32process
import yaml
from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
OUTPUT_PATH = os.path.join(BASE_DIR, "标定检查图.png")
FOREGROUND_SETTLE_SEC = 0.6   # 把荼蘼置前后，等它重绘完成再截图的时间

# 已确认的两个参考色：运行中的行是浅绿、游戏异常退出的行是粉红。
# 判定容差统一读 config.yaml 的 monitor_settings.color_tolerance，
# 保证这里的检查结论和 monitor.py 实际运行时的判定完全一致
COLOR_RUNNING = (144, 238, 144)
COLOR_ABNORMAL = (255, 192, 203)
WHITE_MIN = 200          # 三个通道都高于这个值就算白色（空闲行）


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_tu_mi_window(process_keyword):
    """按可执行文件名找到荼蘼进程的可见窗口，返回hwnd；找不到返回None。

    这里没有直接复用 daily_cleanup 里的同名函数：那个模块在import时就会读取相对路径的
    config.yaml 并初始化日志，要求必须在项目根目录下运行；本工具希望在任何目录下
    双击/运行都能用，所以自带一份最小实现。
    """
    pid = None
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            exe = proc.info["exe"]
            if exe and process_keyword in exe:
                pid = proc.info["pid"]
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not pid:
        return None

    found = []

    def _enum(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            _, hwnd_pid = win32process.GetWindowThreadProcessId(hwnd)
            if hwnd_pid == pid:
                found.append(hwnd)

    win32gui.EnumWindows(_enum, None)
    return found[0] if found else None


def bring_to_foreground(hwnd):
    """把荼蘼窗口提到最前。

    截图截的是屏幕上那块区域的像素，所以荼蘼一旦被别的窗口盖住，取到的就是别人的颜色。
    本工具自己最后会打开生成的检查图，那个看图窗口正好会盖在荼蘼上，
    于是"看图→改配置→再检查"这个循环里，第二次之后的读数就全是错的——必须先置前。

    Windows有前台锁定限制，非前台线程直接调SetForegroundWindow常被静默忽略，
    所以用AttachThreadInput把自己的输入线程临时接到目标窗口线程上绕过。
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
        cur_thread = win32api.GetCurrentThreadId()

        attached_fg = fg_thread and fg_thread != target_thread and \
            win32process.AttachThreadInput(fg_thread, target_thread, True)
        attached_cur = cur_thread != target_thread and \
            win32process.AttachThreadInput(cur_thread, target_thread, True)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached_fg:
                win32process.AttachThreadInput(fg_thread, target_thread, False)
            if attached_cur:
                win32process.AttachThreadInput(cur_thread, target_thread, False)
        return True
    except Exception:
        return False


def find_obscuring_window(hwnd, sample_points):
    """检查荼蘼在这些取色点上是不是被别的窗口盖住了。
    截图截的是屏幕像素，被盖住就会取到别人的颜色，必须先发现这种情况而不是给出错误结论。
    返回遮挡窗口的标题；没有遮挡返回None。"""
    for x, y in sample_points:
        try:
            top_hwnd = win32gui.WindowFromPoint((x, y))
            root = win32gui.GetAncestor(top_hwnd, 2)  # GA_ROOT，取顶层窗口
        except Exception:
            continue
        if root and root != hwnd:
            return win32gui.GetWindowText(root) or "（无标题窗口）"
    return None


def grab_live_screenshot(row_cfg, max_rows, process_keyword):
    """截取当前荼蘼窗口，返回(图片, 说明)；无法可靠采集时返回(None, 原因)"""
    hwnd = find_tu_mi_window(process_keyword)
    if not hwnd:
        return None, "没有检测到正在运行的荼蘼"
    import pyautogui  # 只有真要截图时才导入，荼蘼没开时省掉这个较重的依赖

    # 置前失败不用报错：Windows的前台锁定经常让SetForegroundWindow静默失败，
    # 但只要窗口本来就露在外面就不影响取色。真正有没有被挡住由下面的遮挡检查判定。
    bring_to_foreground(hwnd)
    time.sleep(FOREGROUND_SETTLE_SEC)  # 等窗口真正到前台并重绘完成，再截图

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)

    # 挂机运行期间窗口会被移动/改变大小，可能截到只有一部分的窗口，
    # 那样取色全是错的。先确认窗口大到能放下要检查的所有行。
    needed_h = row_cfg["top_y"] + max_rows * row_cfg["height"]
    needed_w = row_cfg.get("content_left_x", 0) + row_cfg["status_text_region"]["x2"]
    if (bottom - top) < needed_h or (right - left) < needed_w:
        return None, (f"荼蘼窗口当前只有 {right - left}x{bottom - top}，"
                      f"放不下要检查的 {max_rows} 行（至少需要 {needed_w}x{needed_h}）。\n"
                      f"    请把荼蘼窗口恢复成正常大小；如果正在挂机运行中，"
                      f"请等这一轮跑完再做标定")

    sample_points = [
        (left + row_cfg.get("content_left_x", 0) + row_cfg["status_color_point"]["x"],
         top + row_cfg["top_y"] + i * row_cfg["height"] + row_cfg["status_color_point"]["y"])
        for i in range(max_rows)
    ]
    blocker = find_obscuring_window(hwnd, sample_points)
    if blocker:
        return None, (f"荼蘼窗口被「{blocker}」挡住了，取到的颜色不可信。\n"
                      f"    请把挡住它的窗口关掉或移开后重试")

    image = pyautogui.screenshot(region=(left, top, right - left, bottom - top))
    return image, f"实时截取的荼蘼窗口（位置 {left},{top}，大小 {image.width}x{image.height}）"


def find_latest_saved_screenshot(config):
    """荼蘼没开时，退而用监控线程之前存下来的最新一张截图"""
    screenshot_dir = os.path.join(BASE_DIR, config["storage_settings"]["screenshot_dir"])
    if not os.path.isdir(screenshot_dir):
        return None, "还没有任何历史截图（screenshots 目录不存在）"

    candidates = []
    for day in os.listdir(screenshot_dir):
        day_dir = os.path.join(screenshot_dir, day)
        if not os.path.isdir(day_dir):
            continue
        for name in os.listdir(day_dir):
            if name.lower().endswith(".png"):
                candidates.append(os.path.join(day_dir, name))
    if not candidates:
        return None, "还没有任何历史截图（screenshots 目录是空的）"

    latest = max(candidates, key=os.path.getmtime)
    taken_at = datetime.datetime.fromtimestamp(os.path.getmtime(latest))
    return Image.open(latest).convert("RGB"), \
        f"历史截图 {os.path.relpath(latest, BASE_DIR)}（{taken_at:%Y-%m-%d %H:%M:%S}）"


def classify_color(rgb, tolerance):
    """把取到的颜色翻译成人话。判定规则与 monitor.py 的 _is_green/_is_red 保持一致"""
    if rgb is None:
        return "取色失败（坐标超出图片范围）", "×"
    r, g, b = rgb[:3]
    if g > r + tolerance and g > b + tolerance:
        return "绿色 = 正在运行", "√"
    if r > g + tolerance and r > b + tolerance:
        return "粉红 = 游戏异常退出", "√"
    if r > WHITE_MIN and g > WHITE_MIN and b > WHITE_MIN:
        return "白色 = 空闲，没有任务", "√"
    return "既不是绿/粉红/白，标定可能不对", "?"


def draw_annotations(image, row_cfg, max_rows):
    """把行分界线、取色点、文字识别区画到截图上，生成一张肉眼可核对的检查图"""
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)

    top_y = row_cfg["top_y"]
    height = row_cfg["height"]
    left_x = row_cfg.get("content_left_x", 0)
    color_point = row_cfg["status_color_point"]
    text_region = row_cfg["status_text_region"]

    # 内容区左边缘：所有行共用的横向基准线
    draw.line([(left_x, 0), (left_x, annotated.height)], fill=(0, 128, 255), width=2)

    for row_index in range(max_rows):
        row_top = top_y + row_index * height
        row_bottom = row_top + height
        if row_top >= annotated.height:
            break

        # 行分界线
        draw.line([(0, row_top), (annotated.width, row_top)], fill=(255, 140, 0), width=1)
        draw.text((6, row_top + 2), f"{row_index + 1}", fill=(255, 140, 0))

        # 取色点：画个空心圈，圈心就是实际取色的那个像素
        cx = left_x + color_point["x"]
        cy = row_top + color_point["y"]
        draw.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], outline=(255, 0, 0), width=2)

        # 状态文字识别区
        draw.rectangle(
            [left_x + text_region["x1"], row_top + text_region["y1"],
             left_x + text_region["x2"], row_top + text_region["y2"]],
            outline=(0, 200, 0), width=2,
        )

        draw.line([(0, row_bottom), (annotated.width, row_bottom)], fill=(255, 140, 0), width=1)

    return annotated


def sample_rows(image, row_cfg, max_rows, tolerance):
    """逐行取色，返回[(行号, rgb, 说明, 标记), ...]"""
    top_y = row_cfg["top_y"]
    height = row_cfg["height"]
    left_x = row_cfg.get("content_left_x", 0)
    color_point = row_cfg["status_color_point"]

    results = []
    for row_index in range(max_rows):
        x = left_x + color_point["x"]
        y = top_y + row_index * height + color_point["y"]
        try:
            rgb = image.getpixel((x, y))[:3]
        except Exception:
            rgb = None
        meaning, mark = classify_color(rgb, tolerance)
        results.append((row_index + 1, (x, y), rgb, meaning, mark))
    return results


def main():
    print("=" * 66)
    print("荼蘼面板标定检查工具")
    print("=" * 66)

    config = load_config()
    row_cfg = config["monitor_settings"]["row"]
    max_rows = config["monitor_settings"]["max_visible_rows"]

    print("\n当前 config.yaml 里的标定参数：")
    print(f"  第一行顶部 top_y          = {row_cfg['top_y']}")
    print(f"  每行高度   height         = {row_cfg['height']}")
    print(f"  内容区左边缘 content_left_x = {row_cfg.get('content_left_x', 0)}")
    print(f"  取色点     status_color_point = "
          f"x={row_cfg['status_color_point']['x']}, y={row_cfg['status_color_point']['y']}")
    print(f"  面板可见行数 max_visible_rows = {max_rows}")

    print("\n正在获取荼蘼界面截图...")
    image, source = grab_live_screenshot(row_cfg, max_rows,
                                         config["global_settings"]["process_keywords"]["tu_mi"])
    if image is None:
        print(f"  实时截图不可用：{source}")
        print("  改用之前存下来的历史截图")
        image, source = find_latest_saved_screenshot(config)
        if image is None:
            print(f"\n失败：{source}")
            print("请先打开荼蘼，或先运行一次挂机让它存下截图，再运行本工具。")
            return
    print(f"  来源：{source}")

    print("\n逐行取色结果：")
    print(f"  {'行号':<6}{'取色坐标':<16}{'颜色RGB':<20}{'判断'}")
    print("  " + "-" * 60)
    unknown = 0
    tolerance = config['monitor_settings']['color_tolerance']
    for row_no, (x, y), rgb, meaning, mark in sample_rows(image, row_cfg, max_rows, tolerance):
        rgb_text = str(rgb) if rgb else "取不到"
        print(f"  {mark} {row_no:<4}({x:>4},{y:>4})    {rgb_text:<20}{meaning}")
        if mark != "√":
            unknown += 1

    annotated = draw_annotations(image, row_cfg, max_rows)
    annotated.save(OUTPUT_PATH)
    print(f"\n已生成标注检查图：{os.path.relpath(OUTPUT_PATH, BASE_DIR)}")
    print("  红色圆圈 = 取色点（应落在每行左侧的状态色块上）")
    print("  绿色方框 = 状态文字识别区（应正好框住「未启动」「初始化」这类文字）")
    print("  橙色横线 = 每行的上下边界（应和界面上的行分隔线对齐）")
    print("  蓝色竖线 = 内容区左边缘（应贴着左侧导航栏的右边）")

    print("\n" + "=" * 66)
    if unknown == 0:
        print("结论：每一行都取到了预期的颜色，标定看起来是正确的。")
        print("      再打开检查图确认一下圆圈和方框的位置就可以了。")
    else:
        print(f"结论：有 {unknown} 行取到的颜色不在预期范围内，标定可能需要调整。")
        print("      打开检查图看红色圆圈偏到哪儿了，然后按下面的方法调整 config.yaml：")
        print("        圆圈整体偏上/偏下  -> 调 monitor_settings.row.top_y")
        print("        越往下的行偏得越多 -> 调 monitor_settings.row.height")
        print("        圆圈整体偏左/偏右  -> 调 monitor_settings.row.content_left_x")
        print("      改完再运行一次本工具，直到每行都是 √。")
    print("=" * 66)

    try:
        os.startfile(OUTPUT_PATH)
    except Exception:
        print(f"\n（自动打开检查图失败，请手动打开 {OUTPUT_PATH} 查看）")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断")
