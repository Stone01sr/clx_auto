import datetime
import json
import logging
import os
import shutil

from tu_mi_queue.models import RoleTask

logger = logging.getLogger(__name__)


class StateStore:
    """每日队列运行状态的JSON持久化 + 截图留存 + 过期数据清理"""

    def __init__(self, storage_settings, base_dir=".", role_order=None):
        """role_order 传 config.yaml 里的完整角色顺序，落盘时按它排序。
        重跑只跑几个角色时，队列里只有这几个，没有它就没法把记录排回原来的顺序。"""
        self.state_dir = os.path.join(base_dir, storage_settings["state_dir"])
        self.screenshot_dir = os.path.join(base_dir, storage_settings["screenshot_dir"])
        self.lock_path = os.path.join(base_dir, storage_settings["lock_file"])
        self.retention_days = storage_settings["retention_days"]
        self.role_order = list(role_order or [])
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.screenshot_dir, exist_ok=True)

    def _state_path(self, date: datetime.date):
        return os.path.join(self.state_dir, f"{date:%Y-%m-%d}.json")

    def save(self, date: datetime.date, tasks):
        """把角色任务的最新状态落盘，按角色名合并进当天已有的记录里。

        必须合并而不能整份覆盖：重跑时队列里只有被选中的那几个角色，直接覆盖会把当天
        其他角色的记录连同历史一起抹掉，查看页面上就只剩重跑的这几个了。
        """
        merged = {t.role_name: t for t in (self.load(date) or [])}
        for task in tasks:
            merged[task.role_name] = task
        payload = {
            "date": f"{date:%Y-%m-%d}",
            "tasks": [merged[name].to_dict() for name in self._ordered_names(merged)],
        }
        path = self._state_path(date)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _ordered_names(self, merged):
        """按 config.yaml 的角色顺序排，剩下的（已经从配置里删掉或停用、但当天有记录的）
        按原文件里的先后顺序缀在后面，不丢记录也不打乱既有排版"""
        names = [name for name in self.role_order if name in merged]
        seen = set(names)
        names.extend(name for name in merged if name not in seen)
        return names

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
