"""统一的日志配置：给每条日志打上"是哪个线程写的"的标签。

主流程（登录、选角色、在荼蘼里注册脚本）和监控线程（定时截图识别行状态）是并发跑的，
两边的日志按时间穿插着往同一个控制台/文件里写，不加标签根本分不清哪条是谁的。
所以在时间戳后面固定加一列线程标签，控制台上再给这一列上色，扫一眼就能分流。
"""
import logging
import os
import sys
import threading
import unicodedata

# 线程名 -> 日志里显示的标签。线程名在 TuMiMonitor.start() 里指定，改名时这里要同步
THREAD_TAGS = {
    "MainThread": "主流程",
    "tu-mi-monitor": "监控",
    "remote-sync": "上报",
}

# 标签列的显示宽度（按终端列数算，一个中文字符占2列），够放下最长的标签并留一格间距
_TAG_WIDTH = 8

# 控制台上给标签列上的色（ANSI），文件里不带这些控制字符
_TAG_COLORS = {
    "主流程": "\033[36m",   # 青色
    "监控": "\033[35m",     # 品红
    "上报": "\033[32m",     # 绿色
}
_RESET = "\033[0m"
_DEFAULT_COLOR = "\033[33m"  # 其他线程（如果以后有）统一黄色

_FORMAT = "[%(asctime)s] %(tag)s %(levelname)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def _display_width(text):
    """中文/全角字符在终端里占2列，按列数算宽度才能让标签列真正对齐"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text, width):
    return text + " " * max(width - _display_width(text), 0)


class ThreadTagFilter(logging.Filter):
    """给每条记录补上 tag 字段：已知线程用中文标签，其他线程直接用线程名兜底"""

    def filter(self, record):
        name = threading.current_thread().name
        record.tag = _pad("[%s]" % THREAD_TAGS.get(name, name), _TAG_WIDTH)
        return True


class ColorTagFormatter(logging.Formatter):
    """只给标签列上色的控制台格式化器。颜色码不进 record，避免污染同一条记录的文件输出"""

    def format(self, record):
        tag = getattr(record, "tag", "")
        color = _DEFAULT_COLOR
        for label, code in _TAG_COLORS.items():
            if label in tag:
                color = code
                break
        text = super().format(record)
        return text.replace(tag, color + tag + _RESET, 1)


def _enable_ansi_on_windows(stream):
    """Win10+ 的控制台默认不解析 ANSI 转义符，要显式打开 VT 模式，否则日志里会出现乱码方块"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # 4 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4))
    except Exception:
        return False


def setup_logging(log_file=None, level=logging.INFO, stream=None):
    """配置根logger：控制台（带颜色）+ 可选的日志文件（纯文本）。
    log_file 为 None 时只输出到控制台，供 snatch_veggies 这类不需要留档的小脚本用。"""
    stream = stream or sys.stdout
    tag_filter = ThreadTagFilter()

    handlers = []
    console = logging.StreamHandler(stream)
    use_color = getattr(stream, "isatty", lambda: False)() and _enable_ansi_on_windows(stream)
    formatter_cls = ColorTagFormatter if use_color else logging.Formatter
    console.setFormatter(formatter_cls(_FORMAT, datefmt=_DATEFMT))
    handlers.append(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        handlers.append(file_handler)

    for handler in handlers:
        # 过滤器挂在handler上而不是logger上：第三方库直接往根logger写的记录也能拿到tag，
        # 不然格式串里的 %(tag)s 会因为字段缺失把整条日志打成错误
        handler.addFilter(tag_filter)

    logging.basicConfig(level=level, handlers=handlers, force=True)
