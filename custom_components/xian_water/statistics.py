"""Statistics backfill for 西安水务 integration.

把历史「日用水量」与「日水费」按**实际发生日期**回填到 HA 长期统计，使能源面板
能显示完整的历史曲线（既有水量，也有成本），而不只是集成安装之后的数据。

设计要点：
- 两份统计各自一份游标（total_water / total_cost），互不影响、可独立失败重试。
- 每天一条：sum = 累计到当天末的值，state = 当天的值。
  能源面板按 sum 的差分算每日消耗/每日成本，所以首日不会出现「从 0 跳到总额」的假尖峰。
- 幂等靠**三重**保证：
  1) 游标（last_imported_day / last_imported_total）只导入新数据；
  2) 校准指纹（signature）变化 → 全量重导；
  3) **基线自校验**：用当前 storage 重算「截至 last_imported_day 的累计值」，
     与游标里的 last_imported_total 不一致 → 说明历史被重算过（改价、改校准、
     或上游修订了旧数据），直接全量重导。这一条覆盖了 2) 覆盖不到的场景。
- 导入失败时不推进游标、也不写指纹，下次刷新重试同一段完整数据。
- statistic_id 采用外部统计格式：
    xian_water:total_water_<client_code>   用水量（m³）
    xian_water:total_cost_<client_code>    水费（元）
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from .storage import XianWaterStorage

_LOGGER = logging.getLogger(__name__)

# storage 内统计游标使用的键名
_STAT_KEY_TOTAL_WATER = "total_water"
_STAT_KEY_TOTAL_COST = "total_cost"

# 数据稳定窗口：距今至少 N 天才认为该日数据不会被修正
_STABILITY_DAYS = 2

# 基线比对容差（浮点累加误差远小于此值）
_BASELINE_TOLERANCE = 0.01


def statistic_id(client_code: str) -> str:
    """返回该户号「累计用水」的外部统计 ID（能源面板可直接选择）。"""
    return f"{DOMAIN}:total_water_{client_code}"


def cost_statistic_id(client_code: str) -> str:
    """返回该户号「累计水费」的外部统计 ID（能源面板成本跟踪可直接选择）。"""
    return f"{DOMAIN}:total_cost_{client_code}"


def clean_series(records: Iterable[Any], value_field: str) -> list[tuple[str, float]]:
    """把 dayList 形式的记录规范成 [(day, value)]。

    跳过：非 dict、缺 day、日期非法、值非法、值 <= 0。按日期升序返回。
    导入与基线自校验共用这一份清洗逻辑，保证两者口径完全一致。
    """
    series: list[tuple[str, float]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        day = record.get("day")
        if not day:
            continue
        day_str = str(day)

        try:
            date.fromisoformat(day_str)
        except ValueError:
            _LOGGER.debug("storage 中的日期格式无效，跳过: %s", day_str)
            continue

        try:
            value = float(record.get(value_field, 0.0) or 0.0)
        except (TypeError, ValueError):
            _LOGGER.debug("storage 中的 %s 值非法，跳过: %s", value_field, day_str)
            continue

        if value <= 0:
            # 跳过零值日，但不中断序列
            continue

        series.append((day_str, value))

    series.sort(key=lambda item: item[0])
    return series


def build_daily_stats(
    series: list[tuple[str, float]],
    *,
    last_imported_day: str | None,
    running_total: float,
    stability_days: int = _STABILITY_DAYS,
) -> tuple[list[StatisticData], str | None, float]:
    """把清洗后的序列转成 StatisticData（纯函数，方便离线验证）。"""
    cutoff = (date.today() - timedelta(days=stability_days)).isoformat()

    stats: list[StatisticData] = []
    new_last_day = last_imported_day
    new_last_total = running_total

    for day_str, value in series:
        if last_imported_day is not None and day_str <= last_imported_day:
            continue
        if day_str > cutoff:
            # 太新的数据可能还会被修正，留到下次刷新再导
            continue

        day_date = date.fromisoformat(day_str)
        running_total = round(running_total + value, 4)

        # recorder 要求 start 为时区感知的整点时刻，取当天 00:00 本地时间
        start = dt_util.as_local(
            datetime(day_date.year, day_date.month, day_date.day, 0, 0, 0)
        )
        stats.append(
            StatisticData(start=start, sum=running_total, state=round(value, 4))
        )

        new_last_day = day_str
        new_last_total = running_total

    return stats, new_last_day, new_last_total


def baseline_matches(
    series: list[tuple[str, float]],
    *,
    last_imported_day: str | None,
    expected_total: float,
) -> bool:
    """用当前 storage 重算截到 last_imported_day 的累计值，与游标记录比对。"""
    if last_imported_day is None:
        return True

    total = 0.0
    for day_str, value in series:
        if day_str > last_imported_day:
            break
        total = round(total + value, 4)

    return abs(total - expected_total) <= _BASELINE_TOLERANCE


async def _async_import_series(
    hass: HomeAssistant,
    storage: "XianWaterStorage",
    *,
    stat_key: str,
    stat_id: str,
    name: str,
    value_field: str,
    unit_of_measurement: str,
    unit_class: str | None,
    value_unit: str,
    label: str,
    signature: str | None = None,
) -> None:
    """把一类「按日数值」序列导入 HA 外部长期统计。

    signature 为 None 时不做指纹校验（仍有基线自校验兜底）。
    """
    cursor = storage.get_statistics_cursor(stat_key)
    last_imported_day: str | None = cursor.get("last_imported_day") or None
    running_total = float(cursor.get("last_imported_total", 0.0) or 0.0)

    series = clean_series(storage.data.get("dayList", []), value_field)

    signature_changed = signature is not None and cursor.get("signature") != signature
    baseline_changed = last_imported_day is not None and not baseline_matches(
        series, last_imported_day=last_imported_day, expected_total=running_total
    )
    if signature_changed or baseline_changed:
        _LOGGER.info(
            "%s 历史被重算（指纹变化=%s / 基线不一致=%s），将全量重导",
            label,
            signature_changed,
            baseline_changed,
        )
        # 只在内存里重置游标；落盘必须等导入成功后与指纹一起写，
        # 否则会出现「指纹已更新、基线还是旧值」的错位（详见 SKILL §4）。
        last_imported_day = None
        running_total = 0.0

    stats, new_last_day, new_last_total = build_daily_stats(
        series, last_imported_day=last_imported_day, running_total=running_total
    )
    if not stats:
        return

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_mean=False,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=stat_id,
        unit_class=unit_class,
        unit_of_measurement=unit_of_measurement,
    )

    try:
        # 外部统计必须使用 async_add_external_statistics，
        # 否则带 ':' 的 statistic_id 会在内部统计校验中触发 Invalid statistic_id。
        async_add_external_statistics(hass, metadata, stats)
    except Exception as exc:  # pylint: disable=broad-except
        _LOGGER.error(
            "%s 统计导入失败，游标与指纹均保持不变，下次刷新将重试: %s (statistic_id=%s)",
            label,
            exc,
            stat_id,
        )
        return

    await hass.async_add_executor_job(
        storage.set_statistics_cursor,
        stat_key,
        new_last_day,
        new_last_total,
        signature,
    )
    _LOGGER.info(
        "已导入 %d 条%s到 HA 统计 (最新日=%s, 累计=%.2f %s)",
        len(stats),
        label,
        new_last_day,
        new_last_total,
        value_unit,
    )


async def async_import_water_statistics(
    hass: HomeAssistant,
    storage: "XianWaterStorage",
    client_code: str,
    signature: str = "none",
) -> None:
    """把历史日用水量回填至 HA 长期统计（能源面板「水消耗」）。"""
    await _async_import_series(
        hass,
        storage,
        stat_key=_STAT_KEY_TOTAL_WATER,
        stat_id=statistic_id(client_code),
        name=f"西安水费 {client_code} 累计用水",
        value_field="dayEleNum",
        unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        unit_class="volume",
        value_unit="m³",
        label="日用水量",
        signature=signature,
    )


async def async_import_water_cost_statistics(
    hass: HomeAssistant,
    storage: "XianWaterStorage",
    client_code: str,
    signature: str = "none",
) -> None:
    """把历史日水费回填至 HA 长期统计（能源面板「成本跟踪 → 统计」）。

    statistic_id 为 xian_water:total_cost_<client_code>，单位「元」。
    注意它与实体统计 sensor.xian_water_<client_code>_total_cost 是两个不同的
    statistic_id，面板里只能选一条（推荐选这条，它有完整历史）。
    """
    await _async_import_series(
        hass,
        storage,
        stat_key=_STAT_KEY_TOTAL_COST,
        stat_id=cost_statistic_id(client_code),
        name=f"西安水费 {client_code} 累计水费（含历史）",
        value_field="dayEleCost",
        unit_of_measurement="元",
        unit_class=None,
        value_unit="元",
        label="日水费",
        signature=signature,
    )
