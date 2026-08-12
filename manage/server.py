import argparse
import datetime
import html
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from tu_mi_queue.state_store import StateStore


def load_config():
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def render_page(title, body):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; color: #222; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 14px; }}
  th {{ background: #f5f5f5; }}
  a {{ color: #2a6ebb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .status-运行完成 {{ color: #2e7d32; }}
  .status-失败 {{ color: #c62828; font-weight: bold; }}
  .status-运行中 {{ color: #1565c0; }}
  img.shot {{ max-width: 640px; display: block; margin: 8px 0; border: 1px solid #ccc; }}
</style>
</head>
<body>
<h2>{html.escape(title)}</h2>
{body}
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    store: StateStore = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/screenshots/"):
            self._serve_screenshot(parsed.path)
            return
        qs = parse_qs(parsed.query)
        if parsed.path == "/role":
            self._render_role(qs)
        else:
            self._render_index(qs)

    def _serve_screenshot(self, path):
        rel_path = path.lstrip("/")
        full_path = os.path.normpath(os.path.join(BASE_DIR, rel_path))
        if not full_path.startswith(os.path.normpath(BASE_DIR)) or not os.path.isfile(full_path):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        with open(full_path, "rb") as f:
            self.wfile.write(f.read())

    def _resolve_date(self, qs):
        date_str = qs.get("date", [None])[0] or f"{datetime.date.today():%Y-%m-%d}"
        try:
            return datetime.datetime.strptime(date_str, "%Y-%m-%d").date(), date_str
        except ValueError:
            today = datetime.date.today()
            return today, f"{today:%Y-%m-%d}"

    def _render_index(self, qs):
        date, date_str = self._resolve_date(qs)
        tasks = self.store.load(date) or []
        dates = self.store.list_dates()

        date_options = "".join(
            f'<option value="{d}" {"selected" if d == date_str else ""}>{d}</option>' for d in dates
        )
        rows = ""
        for t in tasks:
            rows += (
                f"<tr><td><a href='/role?date={date_str}&name={t.role_name}'>{html.escape(t.role_name)}</a></td>"
                f"<td class='status-{t.status}'>{html.escape(t.status)}</td>"
                f"<td>{html.escape(t.tu_mi_raw_status or '-')}</td>"
                f"<td>{t.retry_count}</td>"
                f"<td>{t.rerun_count}</td>"
                f"<td>{t.row_index if t.row_index >= 0 else '-'}</td></tr>"
            )
        body = f"""
<form method="get" action="/">
  查看日期：<select name="date" onchange="this.form.submit()">{date_options}</select>
</form>
<table>
<tr><th>角色</th><th>状态</th><th>荼蘼原始状态</th><th>自动重试</th><th>人工重跑</th><th>荼蘼行号</th></tr>
{rows or '<tr><td colspan="6">当天暂无记录</td></tr>'}
</table>
"""
        self._send_html(render_page(f"脚本运行队列 - {date_str}", body))

    def _render_role(self, qs):
        date, date_str = self._resolve_date(qs)
        role_name = qs.get("name", [None])[0]
        tasks = self.store.load(date) or []
        task = next((t for t in tasks if t.role_name == role_name), None)
        if not task:
            self.send_error(404)
            return

        rows = ""
        for h in reversed(task.history):
            img_tag = f"<img class='shot' src='/{h.screenshot_path}'>" if h.screenshot_path else "-"
            rows += (
                f"<tr><td>{h.timestamp}</td><td>{html.escape(h.from_status)} → {html.escape(h.to_status)}</td>"
                f"<td>{html.escape(h.tu_mi_raw_status or '-')}</td><td>{img_tag}</td></tr>"
            )
        body = f"""
<p><a href="/?date={date_str}">&larr; 返回{html.escape(date_str)}列表</a></p>
<p>当前状态：<b>{html.escape(task.status)}</b>　荼蘼原始状态：{html.escape(task.tu_mi_raw_status or '-')}　自动重试：{task.retry_count} 次　人工重跑：{task.rerun_count} 次</p>
<table>
<tr><th>时间</th><th>状态变化</th><th>荼蘼原始状态</th><th>截图</th></tr>
{rows or '<tr><td colspan="4">暂无状态变迁记录</td></tr>'}
</table>
"""
        self._send_html(render_page(f"{role_name} - {date_str} 状态变迁", body))

    def _send_html(self, content):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # 静默HTTP访问日志，避免刷屏


def main():
    parser = argparse.ArgumentParser(description="脚本运行队列 - 本地查看页面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    config = load_config()
    Handler.store = StateStore(config["storage_settings"], base_dir=BASE_DIR)

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"队列状态查看页面已启动: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
