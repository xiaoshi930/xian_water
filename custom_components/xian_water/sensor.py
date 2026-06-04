"""Sensor platform for 西安水务 integration."""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_CLIENT_CODE,
    TIER_LEVEL_1,
    TIER_LEVEL_2,
    TIER_PRICE_1,
    TIER_PRICE_2,
    TIER_PRICE_3,
    RECHARGE_RECORDS,
)
from .storage import XianWaterStorage

_LOGGER = logging.getLogger(__name__)

# 默认日均用水量 (m³)
DEFAULT_DAILY_VOLUME = 0.38
# 随机波动范围
VARIATION_RANGE = 0.15


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the 西安水务 sensor platform."""
    config = entry.data

    coordinator = XianWaterCoordinator(hass, config)
    await coordinator.async_load_storage()
    await coordinator.async_config_entry_first_refresh()

    client_code = config.get("client_code", "")
    sensor = XianWaterSensor(coordinator, config)

    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator
    hass.data[DOMAIN][entry.entry_id]["entities"] = [sensor]

    async_add_entities([sensor], True)


class XianWaterCoordinator(DataUpdateCoordinator):
    """Coordinator for 西安水务 data."""

    def __init__(self, hass: HomeAssistant, config: dict):
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=2),
        )
        self.config = config
        self.last_update_time = datetime.now()
        client_code = config.get("client_code", "default")
        self._storage = XianWaterStorage(hass, client_code)
        self.data = None

    async def async_load_storage(self) -> None:
        """Load persistent storage data asynchronously."""
        await self._storage.async_load()
        if self._storage.data.get("dayList"):
            self.data = dict(self._storage.data)

    async def _async_update_data(self):
        """Fetch data - generate fake daily water usage data."""
        try:
            day_list = list(self._storage.data.get("dayList", []))
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

            if not day_list:
                # 无历史数据，从充值明细最早日期开始生成全部数据
                day_list = self._generate_all_data()
            else:
                # 有历史数据，补充缺失的日期到今天
                last_day = max(d["day"] for d in day_list)
                last_date = datetime.strptime(last_day, "%Y-%m-%d")
                current_date = last_date + timedelta(days=1)

                while current_date <= today:
                    date_str = current_date.strftime("%Y-%m-%d")
                    day_data = self._generate_single_day(date_str, day_list)
                    day_list.append(day_data)
                    current_date += timedelta(days=1)

            # 计算余额 = 最近一笔充值金额 - 日均消费 × 距今天数
            records_sorted = sorted(RECHARGE_RECORDS, key=lambda x: x["date"], reverse=True)
            latest_recharge = float(records_sorted[0]["cost"])
            latest_recharge_date = datetime.strptime(records_sorted[0]["date"], "%Y-%m-%d")
            days_since_latest = (today - latest_recharge_date).days
            # 日均消费：用除最近一笔外的充值总额 / 首尾充值日期间隔
            other_recharge_total = sum(float(r["cost"]) for r in records_sorted[1:])
            first_recharge_date = datetime.strptime(records_sorted[-1]["date"], "%Y-%m-%d")
            recharge_span = abs((latest_recharge_date - first_recharge_date).days)
            if recharge_span > 0:
                avg_daily_cost = other_recharge_total / recharge_span
            else:
                avg_daily_cost = DEFAULT_DAILY_VOLUME * TIER_PRICE_1
            balance = round(latest_recharge - avg_daily_cost * days_since_latest, 2)

            # 处理月数据和年数据
            month_list = self._process_month_data(day_list)
            year_list = self._process_year_data(month_list)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            processed = {
                "date": now_str,
                "balance": balance,
                "dayList": day_list,
                "monthList": month_list,
                "yearList": year_list,
            }

            # 持久化存储
            merged = await self.hass.async_add_executor_job(self._storage.update, processed)
            self.data = merged
            self.last_update_time = datetime.now()
            return self.data

        except Exception as ex:
            _LOGGER.error("更新水费数据失败: %s", ex)
            raise UpdateFailed(f"Error updating water data: {ex}")

    def _generate_all_data(self) -> list:
        """从最早充值日期到今天，生成全部伪造的每日用水数据."""
        records = sorted(RECHARGE_RECORDS, key=lambda x: x["date"])
        first_recharge_date = datetime.strptime(records[0]["date"], "%Y-%m-%d")
        # 第一笔充值金额代表之前已用掉的水费，往前推算起始日期
        first_cost = float(records[0]["cost"])
        avg_daily_cost = DEFAULT_DAILY_VOLUME * TIER_PRICE_1
        days_before = int(first_cost / avg_daily_cost)
        start_date = first_recharge_date - timedelta(days=days_before)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        day_list = []
        current_date = start_date

        while current_date <= today:
            date_str = current_date.strftime("%Y-%m-%d")
            day_data = self._generate_single_day(date_str, day_list)
            day_list.append(day_data)
            current_date += timedelta(days=1)

        return day_list

    def _generate_single_day(self, date_str: str, existing_days: list) -> dict:
        """生成单日伪造用水数据，基于平均用量加随机波动，按年阶梯计算费用."""
        # 日均用量 + 随机波动
        variation = random.uniform(-VARIATION_RANGE, VARIATION_RANGE)
        daily_volume = max(0.05, DEFAULT_DAILY_VOLUME + variation)
        daily_volume = round(daily_volume, 2)

        # 计算该年累计用水量（本日之前）
        year = int(date_str[:4])
        prev_annual = sum(
            d["dayEleNum"] for d in existing_days
            if d["day"].startswith(str(year)) and d["day"] < date_str
        )
        current_annual = prev_annual + daily_volume

        # 根据年阶梯计算费用
        daily_cost = self._calculate_tier_cost(daily_volume, prev_annual, current_annual)

        return {
            "day": date_str,
            "dayEleNum": daily_volume,
            "dayEleCost": round(daily_cost, 2),
        }

    def _calculate_tier_cost(self, daily_volume, prev_annual, current_annual):
        """根据年阶梯水价计算单日费用."""
        if daily_volume == 0:
            return 0

        if current_annual <= TIER_LEVEL_1:
            # 全部在第1档
            return daily_volume * TIER_PRICE_1
        elif current_annual <= TIER_LEVEL_2:
            if prev_annual <= TIER_LEVEL_1:
                # 跨第1档和第2档
                tier1_part = TIER_LEVEL_1 - prev_annual
                tier2_part = daily_volume - tier1_part
                return tier1_part * TIER_PRICE_1 + tier2_part * TIER_PRICE_2
            else:
                # 全部在第2档
                return daily_volume * TIER_PRICE_2
        else:
            if prev_annual <= TIER_LEVEL_1:
                # 跨第1、2、3档
                tier1_part = TIER_LEVEL_1 - prev_annual
                remaining = daily_volume - tier1_part
                tier2_capacity = TIER_LEVEL_2 - TIER_LEVEL_1
                if remaining <= tier2_capacity:
                    return tier1_part * TIER_PRICE_1 + remaining * TIER_PRICE_2
                else:
                    tier2_part = tier2_capacity
                    tier3_part = remaining - tier2_part
                    return tier1_part * TIER_PRICE_1 + tier2_part * TIER_PRICE_2 + tier3_part * TIER_PRICE_3
            elif prev_annual <= TIER_LEVEL_2:
                # 跨第2、3档
                tier2_part = TIER_LEVEL_2 - prev_annual
                tier3_part = daily_volume - tier2_part
                return tier2_part * TIER_PRICE_2 + tier3_part * TIER_PRICE_3
            else:
                # 全部在第3档
                return daily_volume * TIER_PRICE_3

    def _process_month_data(self, day_list: list) -> list:
        """从日数据汇总月数据."""
        try:
            month_map = {}
            for day_item in day_list:
                month_str = day_item["day"][:7]  # YYYY-MM
                if month_str not in month_map:
                    month_map[month_str] = {
                        "month": month_str,
                        "monthEleNum": 0,
                        "monthEleCost": 0,
                    }
                month_map[month_str]["monthEleNum"] += day_item.get("dayEleNum", 0)
                month_map[month_str]["monthEleCost"] += day_item.get("dayEleCost", 0)

            result = []
            for month_data in month_map.values():
                result.append({
                    "month": month_data["month"],
                    "monthEleNum": round(month_data["monthEleNum"], 2),
                    "monthEleCost": round(month_data["monthEleCost"], 2),
                })

            return sorted(result, key=lambda x: x["month"])
        except Exception as ex:
            _LOGGER.error("处理月数据失败: %s", ex)
            return []

    def _process_year_data(self, month_list: list) -> list:
        """从月数据汇总年数据."""
        try:
            year_map = {}
            for month_data in month_list:
                year = month_data["month"].split("-")[0]
                if year not in year_map:
                    year_map[year] = {
                        "year": year,
                        "yearEleNum": 0,
                        "yearEleCost": 0,
                    }
                year_map[year]["yearEleNum"] += month_data.get("monthEleNum", 0)
                year_map[year]["yearEleCost"] += month_data.get("monthEleCost", 0)

            result = []
            for year_data in year_map.values():
                result.append({
                    "year": year_data["year"],
                    "yearEleNum": round(year_data["yearEleNum"], 2),
                    "yearEleCost": round(year_data["yearEleCost"], 2),
                })

            return sorted(result, key=lambda x: x["year"], reverse=True)
        except Exception as ex:
            _LOGGER.error("处理年数据失败: %s", ex)
            return []


class XianWaterSensor(SensorEntity):
    """Representation of a 西安水费 sensor."""

    def __init__(self, coordinator: XianWaterCoordinator, config: dict):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self.config = config
        client_code = config.get("client_code", "")
        self._attr_unique_id = f"xian_water_{client_code}"
        self._attr_name = f"西安水费 {client_code}"
        self._attr_icon = "mdi:water"
        self._attr_native_unit_of_measurement = "元"
        self._client_code = client_code

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.data is not None and bool(self.coordinator.data.get("dayList"))

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self.coordinator.data:
            return self.coordinator.data.get("balance", 0)
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        attrs = {}

        if self.coordinator.data:
            day_list = self.coordinator.data.get("dayList", [])
            if day_list:
                sorted_days = sorted(day_list, key=lambda x: x["day"], reverse=True)
                recent_days = sorted_days[:7]

                if recent_days:
                    daily_costs = [day.get("dayEleCost", 0) for day in recent_days]
                    avg_daily_cost = sum(daily_costs) / len(daily_costs)

                    balance = self.coordinator.data.get("balance", 0)
                    if avg_daily_cost > 0:
                        estimated_days = balance / avg_daily_cost
                        try:
                            latest_day = sorted_days[0]["day"]
                            latest_date = datetime.strptime(latest_day, "%Y-%m-%d")
                            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                            days_since_latest = (today - latest_date).days
                            remaining_days = max(0, estimated_days - days_since_latest)

                            attrs["日均消费"] = round(avg_daily_cost, 2)
                            attrs["剩余天数"] = math.ceil(remaining_days)
                            attrs["预付费"] = "否"
                        except (ValueError, IndexError) as e:
                            _LOGGER.error("计算剩余天数时出错: %s", e)

            # 按日期倒序输出列表
            sorted_daylist = sorted(
                self.coordinator.data.get("dayList", []),
                key=lambda x: x["day"], reverse=True
            )
            sorted_monthlist = sorted(
                self.coordinator.data.get("monthList", []),
                key=lambda x: x["month"], reverse=True
            )

            attrs.update({
                "date": self.coordinator.data.get("date", ""),
                "daylist": sorted_daylist,
                "monthlist": sorted_monthlist,
                "yearlist": self.coordinator.data.get("yearList", []),
            })

        # 计费标准信息
        billing_attrs = {"计费标准": "年阶梯"}
        ladder_info = self._get_ladder_info()
        billing_attrs.update(ladder_info)

        billing_attrs["年阶梯第2档起始水量"] = TIER_LEVEL_1
        billing_attrs["年阶梯第3档起始水量"] = TIER_LEVEL_2
        billing_attrs["年阶梯第1档水价"] = TIER_PRICE_1
        billing_attrs["年阶梯第2档水价"] = TIER_PRICE_2
        billing_attrs["年阶梯第3档水价"] = TIER_PRICE_3

        # 当前年阶梯日期范围
        current_date = datetime.now()
        current_year = current_date.year
        year_ladder_start_date = f"{current_year}.01.01"
        year_ladder_end_date = f"{current_year}.12.31"
        billing_attrs["当前年阶梯起始日期"] = year_ladder_start_date
        billing_attrs["当前年阶梯结束日期"] = year_ladder_end_date

        attrs["计费标准"] = billing_attrs

        attrs["数据源"] = "西安水务"
        attrs["最后同步日期"] = self.coordinator.last_update_time.strftime("%Y-%m-%d %H:%M:%S")

        return attrs

    def _get_ladder_info(self):
        """获取当前阶梯档和累计用水量信息."""
        try:
            if not self.coordinator.data or not self.coordinator.data.get("dayList"):
                return {}

            day_list = self.coordinator.data.get("dayList", [])
            if not day_list:
                return {}

            current_year = str(datetime.now().year)
            year_accumulated = sum(
                d["dayEleNum"] for d in day_list
                if d["day"].startswith(current_year)
            )

            if year_accumulated <= TIER_LEVEL_1:
                current_ladder = "第1档"
            elif year_accumulated <= TIER_LEVEL_2:
                current_ladder = "第2档"
            else:
                current_ladder = "第3档"

            return {
                "当前年阶梯档": current_ladder,
                "年阶梯累计用水量": round(year_accumulated, 2),
            }
        except Exception as ex:
            _LOGGER.error("获取阶梯信息失败: %s", ex)
            return {}

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
