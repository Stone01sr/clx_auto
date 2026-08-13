"""补传运行记录：把本地已有的每日状态记录重新上报到网站的「挂机运行情况」页。

用在这几种情况：
  - 那几天网断了 / 网站挂了 / 环境变量还没设，跑的时候没传上去；
  - 手工订正过本地的 state/*.json，想让网站上也跟着更新；
  - 刚接上这个功能，想把以前的记录一次性补齐。

只读本地状态文件 + 发 HTTP，不碰荼蘼、不动鼠标，正在跑的挂机完全不受影响，
所以有任务在跑的时候也可以用。
"""

import datetime
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from tu_mi_queue.process_lock import ProcessLock
from tu_mi_queue.remote_sync import (
    OVERVIEW_PATH, RemoteSyncNotConfigured, build_payload, post_snapshot, resolve_endpoint,
)
from tu_mi_queue.state_store import StateStore

# 补传的是历史快照，不是活着的那一轮在报心跳。传 running=True 的话，
# 网页 15 分钟收不到下一次心跳就会把这台机器标成"失联"，反而更容易误导
BACKFILL_RUNNING = False


def load_config():
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def pad(text, width):
    """按终端列数补空格。中文一个字占2列，用 %-12s 那种按字符数的对齐会歪掉"""
    text = str(text)
    shown = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(width - shown, 0)


def fetch_server_dates(base_url, machine):
    """查网站上这台机器已经有哪几天的记录，用于在列表里标出"哪些还没传上去"。
    查不到不影响补传，返回 None 表示"没查着，状态未知"。"""
    url = "%s%s?machine=%s" % (base_url.rstrip("/"), OVERVIEW_PATH, urllib.parse.quote(machine))
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return set(json.loads(resp.read().decode("utf-8")).get("dates", []))
    except Exception as e:
        print("（查询网站已有记录失败：%s，不影响补传）" % e)
        print()
        return None


def build_rows(store, server_dates):
    """(日期, 本地角色数, 网站状态)，按日期倒序，最近的排最前面"""
    rows = []
    for date_str in store.list_dates():
        tasks = store.load(datetime.datetime.strptime(date_str, "%Y-%m-%d").date()) or []
        if server_dates is None:
            status = "未知"
        elif date_str in server_dates:
            status = "已有记录"
        else:
            status = "网站上没有"
        rows.append((date_str, len(tasks), status))
    return rows


def print_rows(rows):
    print("本地有这些天的记录：")
    print()
    print("  %s%s%s%s" % (pad("编号", 6), pad("日期", 14), pad("角色数", 10), pad("网站上", 14)))
    for index, (date_str, count, status) in enumerate(rows, start=1):
        print("  %s%s%s%s" % (pad(index, 6), pad(date_str, 14), pad(count, 10), pad(status, 14)))
    print()


def parse_selection(text, rows):
    """把输入解析成日期列表。支持编号、日期混着填，逗号或空格分隔，all 表示全选。
    有任何一项解析不了就整体退回让他重填——宁可多问一次，也别默默少传一天。"""
    dates = [row[0] for row in rows]
    text = text.strip()
    if text.lower() == "all":
        return list(dates)

    selected, seen = [], set()
    for item in text.replace(",", " ").replace("，", " ").split():
        if item.isdigit():
            index = int(item)
            if not 1 <= index <= len(rows):
                print("编号 %s 超出范围（1-%d）" % (item, len(rows)))
                return None
            date_str = dates[index - 1]
        elif item in dates:
            date_str = item
        else:
            print("无法识别的编号或日期: %s（日期要写成 2026-08-12 这样）" % item)
            return None
        if date_str not in seen:
            seen.add(date_str)
            selected.append(date_str)
    return selected or None


def last_change_at(tasks):
    """那天所有角色里最后一次状态变迁的时间戳，一条都没有则返回 None"""
    stamps = [h.timestamp for task in tasks for h in task.history if h.timestamp]
    return max(stamps) if stamps else None


def push_dates(store, url, token, machine, selected, timeout_sec):
    """按时间正序补传。倒着传的话，机器心跳里的"最近运行日期"会停在最早那天"""
    ok = 0
    for date_str in sorted(selected):
        date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        tasks = store.load(date) or []
        payload = build_payload(
            machine, date_str, [t.to_dict() for t in tasks], running=BACKFILL_RUNNING,
            # 用那天最后一次状态变迁的时间，比"现在"更能说明这份记录停在哪一刻；
            # 一条变迁都没有（比如刚建队列就中断了）就留空，由 build_payload 填当前时间
            client_time=last_change_at(tasks),
        )
        try:
            post_snapshot(url, token, payload, timeout_sec)
            print("  [成功] %s  %d 个角色" % (date_str, len(tasks)))
            ok += 1
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("  [失败] %s  密码不对：环境变量里的值和网站的写入密码不一致" % date_str)
            else:
                print("  [失败] %s  网站返回 HTTP %s" % (date_str, e.code))
        except Exception as e:
            print("  [失败] %s  %s" % (date_str, e))
    return ok


def main():
    config = load_config()
    try:
        url, token, machine, settings = resolve_endpoint(config)
    except RemoteSyncNotConfigured as e:
        print("现在还传不了：%s" % e)
        return

    base_url = settings["base_url"]
    print("本机名称：%s" % machine)
    print("目标网站：%s" % base_url.rstrip("/"))
    print()

    holder = ProcessLock(os.path.join(BASE_DIR, config["storage_settings"]["lock_file"])).read_holder()
    if holder:
        print("提示：当前有一轮挂机正在跑（进程号 %s，从 %s 开始）。" %
              (holder.get("pid"), holder.get("started_at", "未知时间")))
        print("      补传不会打断它。但那一轮自己也在实时上报，补传今天的记录意义不大。")
        print()

    store = StateStore(config["storage_settings"], base_dir=BASE_DIR)
    rows = build_rows(store, fetch_server_dates(base_url, machine))
    if not rows:
        print("本地一天的记录都没有（state/ 是空的），没什么可传的")
        return

    print_rows(rows)
    print("输入要补传的编号或日期，多个用空格或逗号隔开（例如: 1 2 或 2026-08-12）")
    print("输入 all 补传全部，直接回车取消")
    print()
    while True:
        try:
            text = input("要补传哪几天: ")
        except (EOFError, KeyboardInterrupt):
            return
        if not text.strip():
            print("已取消")
            return
        selected = parse_selection(text, rows)
        if selected:
            break

    print()
    print("开始补传这%d天：%s" % (len(selected), "、".join(sorted(selected))))
    ok = push_dates(store, url, token, machine, selected, settings.get("timeout_sec", 5))
    print()
    if ok == len(selected):
        print("全部补传成功，去 %s/clx-status 看看" % base_url.rstrip("/"))
    else:
        print("成功 %d 天，失败 %d 天（失败的可以直接再跑一次，重复补传不会有副作用）"
              % (ok, len(selected) - ok))


if __name__ == "__main__":
    main()
