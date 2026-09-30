"""Statistics backfill for 西安水务 integration.

把历史日用水量按**实际用水日期**导入 HA 长期统计，使能源面板的「水消耗」
板块能够显示完整历史曲线，而不只是集成安装之后的数据。

设计要点：
- 使用游标（last_imported_day / last_imported_total）保证幂等，重复刷新不会重复累加。
- 仅回填「距今至少 2 天」的数据，避免后续修正导致错误值被固化。
- 导入失败时不推进游标，下次刷新自动重试。
- statistic_id 采用外部统计格式：xian_water:total_water_<client_code>
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfVolume
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from .storage import XianWaterStorage

_LOGGER = logging.getLogger(__name__)

# storage 内统计游标使用的键名
_STAT_KEY_TOTAL_WATER = "total_water"

# 数据稳定窗口：距今至少 N 天才认为该日数据不会被修正
_STABILITY_DAYS = 2


def statistic_id(client_code: str) -> str:
    """返回该户号的外部统计 ID（能源面板可直接选择）。"""
    return f"{DOMAIN}:total_water_{client_code}"


async def async_import_water_statistics(
    hass: HomeAssistant,
    storage: "XianWaterStorage",
    client_code: str,
) -> None:
    """把历史日用水量回填至 HA 长期统计。

    1. 读取游标，得到上次导入到哪一天（last_imported_day）与累计值（last_imported_total）。
    2. 从 storage 的 dayList 中选出晚于游标、且距今至少 _STABILITY_DAYS 天的记录。
    3. 生成 StatisticData 序列（每天一条，sum = 累计到当天末的总用水量）。
    4. 调用 async_add_external_statistics（调度到 recorder 线程执行）。
    5. 成功后推进游标；失败时保留游标等待下次重试。
    """
    cursor = storage.get_statistics_cursor(_STAT_KEY_TOTAL_WATER)
    last_imported_day: str | None = cursor.get("last_imported_day") or None
    running_total = float(cursor.get("last_imported_total", 0.0) or 0.0)

    cutoff = (date.today() - timedelta(days=_STABILITY_DAYS)).isoformat()

    pending = sorted(
        [
            r
            for r in storage.data.get("dayList", [])
            if isinstance(r, dict)
            and r.get("day")
            and (last_imported_day is None or r["day"] > last_imported_day)
            and r["day"] <= cutoff
        ],
        key=lambda x: x["day"],
    )
    if not pending:
        return

    stats: list[StatisticData] = []
    new_last_day = last_imported_day
    new_last_total = running_total

    for record in pending:
        day_str = str(record["day"])
        water = float(record.get("dayEleNum", 0.0) or 0.0)
        if water <= 0:
            # 跳过零值日，但不中断序列
            continue

        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            _LOGGER.debug("storage 中的日期格式无效，跳过: %s", day_str)
            continue

        running_total = round(running_total + water, 4)

        # recorder 要求 start 为时区感知的整点时刻，取当天 00:00 本地时间
        start = dt_util.as_local(
            datetime(day_date.year, day_date.month, day_date.day, 0, 0, 0)
        )
        stats.append(
            StatisticData(start=start, sum=running_total, state=round(water, 4))
        )

        new_last_day = day_str
        new_last_total = running_total

    if not stats:
        return

    stat_id = statistic_id(client_code)
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_mean=False,
        has_sum=True,
        name=f"西安水费 {client_code} 累计用水",
        source=DOMAIN,
        statistic_id=stat_id,
        unit_class="volume",
        unit_of_measurement=UnitOfVolume.CUBIC_METERS,
    )

    try:
        # 外部统计必须使用 async_add_external_statistics，
        # 否则带 ':' 的 statistic_id 会在内部统计校验中触发 Invalid statistic_id。
        async_add_external_statistics(hass, metadata, stats)
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.error(
            "用水统计导入失败，游标保持不变，下次刷新将重试: %s (statistic_id=%s)",
            exc,
            stat_id,
        )
        return

    await hass.async_add_executor_job(
        storage.set_statistics_cursor,
        _STAT_KEY_TOTAL_WATER,
        new_last_day,
        new_last_total,
    )
    _LOGGER.info(
        "已导入 %d 条日用水量到 HA 统计 (最新日=%s, 累计=%.2f m³)",
        len(stats),
        new_last_day,
        new_last_total,
    )
