"""Persistent storage for 西安水务 integration."""
import json
import logging
import os
from typing import Any

_LOGGER = logging.getLogger(__name__)


class XianWaterStorage:
    """Manage persistent JSON storage for water data.

    Rules:
    - Data can only be added, never deleted.
    - Existing entries can be updated with new values.
    - dayList is merged by "day" key.
    - monthList is merged by "month" key.
    - yearList is merged by "year" key.

    除业务数据外，另存一段与业务无关的元数据：
    - "statistics": HA 长期统计的导入游标（见 statistics.py）。
      键名与业务字段不冲突，旧文件缺少该键时会自动补上。
    """

    def __init__(self, hass, client_code: str):
        """Initialize storage."""
        self._hass = hass
        self._client_code = client_code
        self._file_path = hass.config.path(f"xian_water_{client_code}.json")
        self._data: dict[str, Any] = {}

    @property
    def file_path(self) -> str:
        """Return the JSON file path."""
        return self._file_path

    @property
    def data(self) -> dict[str, Any]:
        """Return current stored data."""
        return self._data

    def _load_sync(self) -> None:
        """Load data from JSON file (sync, must run in executor)."""
        try:
            if os.path.exists(self._file_path):
                with open(self._file_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                if not isinstance(self._data, dict):
                    self._data = {}
                # 统计游标：老文件没有该键时补上，不影响业务字段
                self._data.setdefault("statistics", {})
                _LOGGER.info(
                    "已加载水费持久化数据: %s (dayList=%d条, monthList=%d条, yearList=%d条)",
                    self._file_path,
                    len(self._data.get("dayList", [])),
                    len(self._data.get("monthList", [])),
                    len(self._data.get("yearList", [])),
                )
            else:
                self._data = {
                    "date": "",
                    "balance": 0,
                    "dayList": [],
                    "monthList": [],
                    "yearList": [],
                    "statistics": {},
                }
                _LOGGER.info("水费持久化文件不存在，初始化空数据: %s", self._file_path)
        except (json.JSONDecodeError, IOError) as ex:
            _LOGGER.error("加载水费持久化数据失败: %s", ex)
            self._data = {
                "date": "",
                "balance": 0,
                "dayList": [],
                "monthList": [],
                "yearList": [],
                "statistics": {},
            }

    async def async_load(self) -> None:
        """Load data from JSON file asynchronously."""
        await self._hass.async_add_executor_job(self._load_sync)

    def _save_sync(self) -> None:
        """Save data to JSON file (sync, must run in executor)."""
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            _LOGGER.debug("已保存水费持久化数据: %s", self._file_path)
        except IOError as ex:
            _LOGGER.error("保存水费持久化数据失败: %s", ex)

    def _merge_list_by_key(self, existing: list, new_items: list, key: str) -> list:
        """Merge two lists by a key field.

        - New items are added.
        - Existing items (matched by key) are updated with new values.
        - Items only in existing are kept (never deleted).
        """
        existing_map = {item[key]: item for item in existing}
        for item in new_items:
            k = item[key]
            if k in existing_map:
                existing_map[k].update(item)
            else:
                existing_map[k] = item
        return sorted(existing_map.values(), key=lambda x: x[key])

    def update(self, new_data: dict[str, Any]) -> dict[str, Any]:
        """Update storage with new data, then return merged result.

        Note: This method does synchronous file I/O via _save_sync.
        It must be called via hass.async_add_executor_job from async code.
        """
        if not new_data:
            return self._data

        # Merge scalar fields - always update
        self._data["date"] = new_data.get("date", self._data.get("date", ""))
        self._data["balance"] = new_data.get("balance", self._data.get("balance", 0))

        # Merge dayList by "day"
        if "dayList" in new_data:
            self._data["dayList"] = self._merge_list_by_key(
                self._data.get("dayList", []), new_data["dayList"], "day"
            )

        # Merge monthList by "month"
        if "monthList" in new_data:
            self._data["monthList"] = self._merge_list_by_key(
                self._data.get("monthList", []), new_data["monthList"], "month"
            )

        # Merge yearList by "year"
        if "yearList" in new_data:
            self._data["yearList"] = self._merge_list_by_key(
                self._data.get("yearList", []), new_data["yearList"], "year"
            )

        # Save to file
        self._save_sync()

        _LOGGER.info(
            "水费数据已合并并持久化: dayList=%d条, monthList=%d条, yearList=%d条",
            len(self._data.get("dayList", [])),
            len(self._data.get("monthList", [])),
            len(self._data.get("yearList", [])),
        )

        return dict(self._data)

    # ------------------------------------------------------------------
    # HA 长期统计游标（供 statistics.py 使用）
    # ------------------------------------------------------------------

    def get_statistics_cursor(self, key: str) -> dict[str, Any]:
        """读取指定统计的导入游标。

        返回 {last_imported_day, last_imported_total}，无记录时返回空字典。
        纯内存读取，不需要 executor。
        """
        stats = self._data.get("statistics")
        if not isinstance(stats, dict):
            return {}
        cursor = stats.get(key)
        return dict(cursor) if isinstance(cursor, dict) else {}

    def set_statistics_cursor(self, key: str, last_day, last_total: float) -> None:
        """写入统计游标并落盘（同步，需通过 executor 调用）。

        只写入 "statistics" 键，不触碰任何业务字段。
        """
        stats = self._data.get("statistics")
        if not isinstance(stats, dict):
            stats = {}
            self._data["statistics"] = stats
        stats[key] = {
            "last_imported_day": last_day,
            "last_imported_total": round(float(last_total or 0.0), 4),
        }
        self._save_sync()
