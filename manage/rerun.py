"""重跑指定角色：列出当天各角色的运行结果，挑几个出来只跑这几个。

典型场景是当天跑完后有两三个角色失败了，不想为了这几个把全部角色重跑一遍。

只支持在没有任务运行时使用。两轮挂机并行会互相抢鼠标、抢荼蘼空闲行，而且新起的那轮
开头就会关掉所有一梦江湖窗口，把正在挂机的角色一起杀掉，所以这里检测到有任务在跑就直接
劝退——真正拦住并发的是 daily_cleanup 里的进程锁，这里只是提前给一句人话提示。
"""

import datetime
import os
import subprocess
import sys
import unicodedata

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from tu_mi_queue.process_lock import ProcessLock
from tu_mi_queue.state_store import StateStore

RUN_NOTICE = ("即将开始重跑，请确认：\n"
              "  - 已经手动关掉所有「一梦江湖」窗口\n"
              "  - 接下来不要操作鼠标键盘，也不要让屏幕息屏或锁屏")


def load_config():
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_rows(config, store):
    """(角色名, 当天状态, 自动重试次数, 人工重跑次数)，顺序与 config.yaml 一致"""
    roles = [role for role in config.get("roles", []) if role.get("enable", True)]
    tasks = {t.role_name: t for t in (store.load(datetime.date.today()) or [])}
    rows = []
    for role in roles:
        task = tasks.get(role["name"])
        if task is None:
            rows.append((role["name"], "今天没跑过", 0, 0))
        else:
            rows.append((role["name"], task.status, task.retry_count, task.rerun_count))
    return rows


def pad(text, width):
    """按终端列数补空格。状态是中文，一个字占2列，直接用 %-12s 那种按字符数的对齐会歪掉"""
    text = str(text)
    shown = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(width - shown, 0)


def print_rows(rows):
    print("当天各角色运行情况：")
    print()
    print("  %s%s%s%s%s" % (pad("编号", 6), pad("角色", 22), pad("状态", 14),
                            pad("自动重试", 10), pad("人工重跑", 10)))
    for index, (name, status, retry_count, rerun_count) in enumerate(rows, start=1):
        print("  %s%s%s%s%s" % (pad(index, 6), pad(name, 22), pad(status, 14),
                                pad(retry_count, 10), pad(rerun_count, 10)))
    print()


def parse_selection(text, rows):
    """把用户输入解析成角色名列表。支持编号、角色名混着填，逗号或空格分隔，all 表示全选。
    有任何一项解析不了就整体退回让他重填——宁可多问一次，也不要默默少跑一个角色。"""
    names = [row[0] for row in rows]
    text = text.strip()
    if text.lower() == "all":
        return list(names)

    selected, seen = [], set()
    for item in text.replace(",", " ").replace("，", " ").split():
        if item.isdigit():
            index = int(item)
            if not 1 <= index <= len(rows):
                print("编号 %s 超出范围（1-%d）" % (item, len(rows)))
                return None
            name = names[index - 1]
        elif item in names:
            name = item
        else:
            print("无法识别的角色或编号: %s" % item)
            return None
        if name not in seen:
            seen.add(name)
            selected.append(name)
    return selected or None


def main():
    config = load_config()
    store = StateStore(config["storage_settings"], base_dir=BASE_DIR)

    holder = ProcessLock(store.lock_path).read_holder()
    if holder:
        print("当前已经有一轮挂机在运行中（进程号 %s，从 %s 开始跑）。"
              % (holder.get("pid"), holder.get("started_at", "未知时间")))
        print()
        print("两轮同时跑会互相抢鼠标，还会把正在挂机的角色关掉，所以不能同时跑。")
        print("请等当前这轮全部跑完之后，再来重跑。")
        print("（可以用菜单里的「查看运行状态」看当前这轮跑到哪了）")
        return

    rows = build_rows(config, store)
    if not rows:
        print("config.yaml 里没有启用的角色，先用菜单里的「添加或修改角色」加一个")
        return

    print_rows(rows)
    print("输入要重跑的角色编号或角色名，多个用空格或逗号隔开（例如: 1 3 5 或 stone,xie）")
    print("输入 all 重跑全部角色，直接回车取消")
    print()
    while True:
        try:
            text = input("要重跑哪些角色: ")
        except (EOFError, KeyboardInterrupt):
            return
        if not text.strip():
            print("已取消")
            return
        selected = parse_selection(text, rows)
        if selected:
            break

    print()
    print("将要重跑这%d个角色：%s" % (len(selected), "、".join(selected)))
    print()
    print(RUN_NOTICE)
    print()
    try:
        input("准备好后按回车开始，或按 Ctrl+C 取消...")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        return

    # 用当前解释器跑，保证和菜单用的是同一个Python环境；cwd固定到项目根目录，
    # 因为 daily_cleanup.py 用相对路径读 config.yaml、写 state/ 和 screenshots/
    try:
        subprocess.run([sys.executable, os.path.join(BASE_DIR, "daily_cleanup.py"),
                        "--roles", ",".join(selected)], cwd=BASE_DIR)
    except KeyboardInterrupt:
        print("\n已中断")


if __name__ == "__main__":
    main()
