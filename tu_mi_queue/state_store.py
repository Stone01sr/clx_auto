import datetime
import json
import logging
import os
import shutil

from tu_mi_queue.models import RoleTask

logger = logging.getLogger(__name__)


class StateStore:
    """每日队列运行状态的JSON持久化 + 截图留存 + 过期数据清理"""

    def __init__(self, storage_settings, base_dir="."):
        self.state_dir = os.path.join(base_dir, storage_settings["state_dir"])
        self.screenshot_dir = os.path.join(base_dir, storage_settings["screenshot_dir"])
        self.retention_days = storage_settings["retention_days"]
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.screenshot_dir, exist_ok=True)

    def _state_path(self, date: datetime.date):
        return os.path.join(self.state_dir, f"{date:%Y-%m-%d}.json")

    def save(self, date: datetime.date, tasks):
        """把当天所有角色任务的最新状态整体落盘"""
        payload = {
            "date": f"{date:%Y-%m-%d}",
            "tasks": [t.to_dict() for t in tasks],
        }
        path = self._state_path(date)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def load(self, date: datetime.date):
        """读取某天的状态记录，返回RoleTask列表；当天还没有记录则返回None"""
        path = self._state_path(date)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return [RoleTask.from_dict(d) for d in payload.get("tasks", [])]

    def list_dates(self):
        """列出所有存在状态记录的日期（用于历史查询页面下拉选择），按日期倒序"""
        dates = []
        for name in os.listdir(self.state_dir):
            if name.endswith(".json"):
                dates.append(name[:-5])
        return sorted(dates, reverse=True)

    def save_screenshot(self, date: datetime.date, image, moment: datetime.datetime = None):
        """保存一张荼蘼截图，返回相对路径（相对项目根目录），供历史记录关联展示"""
        moment = moment or datetime.datetime.now()
        day_dir = os.path.join(self.screenshot_dir, f"{date:%Y-%m-%d}")
        os.makedirs(day_dir, exist_ok=True)
        filename = f"{moment:%H%M%S}.png"
        full_path = os.path.join(day_dir, filename)
        image.save(full_path)
        return os.path.relpath(full_path, os.path.dirname(self.screenshot_dir))

    def cleanup_old_data(self, today: datetime.date = None):
        """删除超过retention_days天的状态文件和截图目录"""
        today = today or datetime.date.today()
        cutoff = today - datetime.timedelta(days=self.retention_days)

        for name in os.listdir(self.state_dir):
            if not name.endswith(".json"):
                continue
            day_str = name[:-5]
            if self._is_older_than(day_str, cutoff):
                try:
                    os.remove(os.path.join(self.state_dir, name))
                    logger.info("已清理过期状态文件: %s", name)
                except OSError as e:
                    logger.warning("清理状态文件失败: %s, %s", name, e)

        for name in os.listdir(self.screenshot_dir):
            full = os.path.join(self.screenshot_dir, name)
            if os.path.isdir(full) and self._is_older_than(name, cutoff):
                try:
                    shutil.rmtree(full)
                    logger.info("已清理过期截图目录: %s", name)
                except OSError as e:
                    logger.warning("清理截图目录失败: %s, %s", name, e)

    @staticmethod
    def _is_older_than(day_str, cutoff: datetime.date):
        try:
            day = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            return False
        return day < cutoff
