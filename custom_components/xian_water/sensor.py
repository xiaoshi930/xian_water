"""Sensor platform for 西安水务 integration."""
from __future__ import annotations

import logging
import math
import random
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_CLIENT_CODE,
    CONF_CALIBRATION_DATE,
    CONF_CALIBRATION_AMOUNT,
    TIER_LEVEL_1,
    TIER_LEVEL_2,
    TIER_PRICE_1,
    TIER_PRICE_2,
    TIER_PRICE_3,
    RECHARGE_RECORDS,
)
from .statistics import (
    async_import_water_cost_statistics,
    async_import_water_statistics,
)
from .storage import XianWaterStorage

_LOGGER = logging.getLogger(__name__)

# 默认日均用水量 (m³)
DEFAULT_DAILY_VOLUME = 0.38
# 随机波动范围
VARIATION_RANGE = 0.15


def build_device_info(client_code: str) -> DeviceInfo:
    """构造同户号下多个实体共用的设备信息。

    同一户号的实体必须使用**同一组 identifiers**，否则 HA 会为每个实体各建一个设备。
    """
    return DeviceInfo(
        identifiers={(DOMAIN, client_code)},
        name=f"西安水费 {client_code}",
        manufacturer="西安水务",
        model="水表",
    )


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

    entities = [
        XianWaterSensor(coordinator, config),
        XianWaterTotalWaterSensor(coordinator, config),
        XianWaterTotalCostSensor(coordinator, config),
    ]

    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator
    hass.data[DOMAIN][entry.entry_id]["entities"] = entities

    async_add_entities(entities, True)


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
        # 由 balance / 日均消费 / 剩余天数 / 校准信息组成的摘要，供实体读取
        self.summary: dict[str, Any] = {}

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

            # 先把历史记录还原成「未校准的基准估算」，再按校准点重算。
            # 这样校准点的切换 / 清空都是可重现、可回退的，不会残留上一次的校准值。
            self._ensure_raw(day_list)
            self._restore_raw(day_list)

            # 最近一笔充值与其距今天数
            records_sorted = sorted(RECHARGE_RECORDS, key=lambda x: x["date"], reverse=True)
            latest_recharge = float(records_sorted[0]["cost"])
            latest_recharge_date = datetime.strptime(
                records_sorted[0]["date"], "%Y-%m-%d"
            ).replace(hour=0, minute=0, second=0, microsecond=0)
            days_since_latest = (today - latest_recharge_date).days

            # 优先按校准点重算；未配置校准时沿用原有估算逻辑
            summary = self._apply_calibration(
                day_list, latest_recharge, latest_recharge_date, today
            )
            if summary is None:
                # 日均消费：用除最近一笔外的充值总额 / 首尾充值日期间隔
                other_recharge_total = sum(float(r["cost"]) for r in records_sorted[1:])
                first_recharge_date = datetime.strptime(records_sorted[-1]["date"], "%Y-%m-%d")
                recharge_span = abs((latest_recharge_date - first_recharge_date).days)
                if recharge_span > 0:
                    avg_daily_cost = other_recharge_total / recharge_span
                else:
                    avg_daily_cost = DEFAULT_DAILY_VOLUME * TIER_PRICE_1
                balance = round(latest_recharge - avg_daily_cost * days_since_latest, 2)

                # 日均消费展示值仍取最近 7 天实际估算的均值
                recent = sorted(day_list, key=lambda x: x["day"], reverse=True)[:7]
                recent_costs = [float(d.get("dayEleCost", 0) or 0) for d in recent]
                display_daily = (
                    sum(recent_costs) / len(recent_costs) if recent_costs else avg_daily_cost
                )

                summary = {
                    "balance": balance,
                    "avg_daily_cost": round(display_daily, 2),
                    "remaining_days": (
                        max(0, math.ceil(balance / display_daily)) if display_daily > 0 else None
                    ),
                    "calibrated": False,
                    "reference_date": latest_recharge_date.strftime("%Y-%m-%d"),
                    "reference_amount": round(latest_recharge, 2),
                    "days_since_recharge": days_since_latest,
                    "calibration": None,
                }

            balance = summary["balance"]

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
            self.summary = summary
            self.last_update_time = datetime.now()

            # 回填 HA 长期统计，供能源面板「水消耗」显示历史曲线
            try:
                await async_import_water_statistics(
                    self.hass,
                    self._storage,
                    self.config.get("client_code", "default"),
                    self._calibration_signature(summary),
                )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning("导入用水长期统计失败: %s", ex)

            # 回填「水费」统计，供能源面板「成本跟踪 → 统计」显示历史成本曲线
            try:
                await async_import_water_cost_statistics(
                    self.hass,
                    self._storage,
                    self.config.get("client_code", "default"),
                    self._calibration_signature(summary),
                )
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning("导入水费长期统计失败: %s", ex)

            return self.data

        except Exception as ex:
            _LOGGER.error("更新水费数据失败: %s", ex)
            raise UpdateFailed(f"Error updating water data: {ex}")

    # ------------------------------------------------------------------
    # 校准
    # ------------------------------------------------------------------

    def _get_calibration(self) -> tuple[str, datetime, float] | None:
        """读取并校验校准配置。

        返回 (校准日期字符串, 校准日期, 校准当天实际余额)，配置缺失或非法时返回 None。
        """
        raw_date = self.config.get(CONF_CALIBRATION_DATE)
        raw_amount = self.config.get(CONF_CALIBRATION_AMOUNT)

        if raw_date in (None, "") or raw_amount in (None, ""):
            return None

        if isinstance(raw_date, datetime):
            date_str = raw_date.strftime("%Y-%m-%d")
        elif isinstance(raw_date, date):
            date_str = raw_date.isoformat()
        else:
            date_str = str(raw_date).strip().replace("/", "-").replace(".", "-")

        try:
            calib_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            _LOGGER.warning("校准日期格式无效（应为 YYYY-MM-DD），已忽略校准: %s", raw_date)
            return None

        try:
            calib_balance = float(raw_amount)
        except (TypeError, ValueError):
            _LOGGER.warning("校准金额无效，已忽略校准: %s", raw_amount)
            return None

        if calib_balance < 0:
            _LOGGER.warning("校准金额不能为负，已忽略校准: %s", raw_amount)
            return None

        return date_str, calib_date, calib_balance

    @staticmethod
    def _calibration_signature(summary: dict[str, Any]) -> str:
        """校准指纹：变化时触发长期统计的全量重导。"""
        info = summary.get("calibration") or {}
        if not summary.get("calibrated"):
            return "none"
        return f"{info.get('date')}|{info.get('balance')}"

    def _apply_calibration(
        self,
        day_list: list,
        latest_recharge: float,
        latest_recharge_date: datetime,
        today: datetime,
    ) -> dict[str, Any] | None:
        """用「校准日期 + 当天实际余额」重算自上次充值以来的全部估算值。

        校准点给出的是区间的**真实终点**，于是：
        - 区间真实消耗 = 上次充值金额 − 校准余额
        - 校准日均消费 = 区间真实消耗 ÷ (校准日 − 充值日)
        - 每日估算金额 / 水量：区间内按比例缩放到真实消耗，校准日之后按校准日均续推
        - 余额 = 校准余额 − 校准日均 × (今天 − 校准日)
        - 剩余天数 = 余额 ÷ 校准日均

        返回 None 表示未配置校准或校准无效，调用方应回退到默认估算。
        """
        calibration = self._get_calibration()
        if calibration is None:
            return None

        date_str, calib_date, calib_balance = calibration

        if calib_date <= latest_recharge_date:
            _LOGGER.warning(
                "校准日期 %s 不晚于最近充值日期 %s，本次校准被忽略",
                date_str,
                latest_recharge_date.strftime("%Y-%m-%d"),
            )
            return None
        if calib_date > today:
            _LOGGER.warning("校准日期 %s 晚于今天，本次校准被忽略", date_str)
            return None

        used = latest_recharge - calib_balance
        if used <= 0:
            _LOGGER.warning(
                "校准余额 %.2f 不小于最近充值金额 %.2f（%s），区间消耗非正，本次校准被忽略",
                calib_balance,
                latest_recharge,
                latest_recharge_date.strftime("%Y-%m-%d"),
            )
            return None

        span_days = (calib_date - latest_recharge_date).days
        daily_cost = round(used / span_days, 4) if span_days > 0 else 0.0

        # 1) 上次充值日 → 校准日：整体缩放到真实消耗
        self._rescale_days(day_list, latest_recharge_date, calib_date, used)

        # 2) 校准日 → 今天：按校准后的日均继续估算
        tail_days = (today - calib_date).days
        if tail_days > 0:
            self._rescale_days(day_list, calib_date, today, daily_cost * tail_days)

        balance = round(calib_balance - daily_cost * tail_days, 2)
        remaining_days = max(0, math.ceil(balance / daily_cost)) if daily_cost > 0 else None

        _LOGGER.info(
            "已应用水费校准: 校准日=%s 校准余额=%.2f 参考充值=%s(%.2f) "
            "区间消耗=%.2f 区间天数=%d 校准日均=%.4f 当前余额=%.2f 剩余天数=%s",
            date_str,
            calib_balance,
            latest_recharge_date.strftime("%Y-%m-%d"),
            latest_recharge,
            used,
            span_days,
            daily_cost,
            balance,
            remaining_days,
        )

        return {
            "balance": balance,
            "avg_daily_cost": round(daily_cost, 2),
            "remaining_days": remaining_days,
            "calibrated": True,
            "reference_date": latest_recharge_date.strftime("%Y-%m-%d"),
            "reference_amount": round(latest_recharge, 2),
            "days_since_recharge": (today - latest_recharge_date).days,
            "calibration": {
                "date": date_str,
                "balance": round(calib_balance, 2),
                "used": round(used, 2),
                "span_days": span_days,
                "avg_daily_cost": round(daily_cost, 2),
                "tail_days": tail_days,
            },
        }

    @staticmethod
    def _ensure_raw(day_list: list) -> None:
        """为老记录补齐「未校准基准值」（Raw 字段）。

        历史版本生成的日记录没有 Raw 字段，首次升级时以当前值作为基准。
        """
        for day in day_list:
            if "dayEleNumRaw" not in day:
                day["dayEleNumRaw"] = float(day.get("dayEleNum", 0) or 0)
            if "dayEleCostRaw" not in day:
                day["dayEleCostRaw"] = float(day.get("dayEleCost", 0) or 0)

    @staticmethod
    def _restore_raw(day_list: list) -> None:
        """把日数据还原为未校准的基准估算值。"""
        for day in day_list:
            day["dayEleNum"] = round(float(day.get("dayEleNumRaw", 0) or 0), 2)
            day["dayEleCost"] = round(float(day.get("dayEleCostRaw", 0) or 0), 2)

    @staticmethod
    def _rescale_days(
        day_list: list,
        start_date: datetime,
        end_date: datetime,
        target_cost: float,
    ) -> None:
        """把 (start_date, end_date] 区间的日估算金额 / 水量缩放到 target_cost。

        以 Raw 基准值按比例缩放，可保留原有「每天略有波动」的形态；
        采用累计取整的方式逐日摊分，避免长区间上四舍五入误差堆积到某一天。
        """
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        window = [d for d in day_list if start_str < str(d.get("day", "")) <= end_str]
        if not window:
            return

        target = max(0.0, float(target_cost))
        current = sum(float(d.get("dayEleCostRaw", 0) or 0) for d in window)

        if current <= 0:
            # 基准估算为 0，无法按比例缩放，退化为按天均分
            each_cost = target / len(window)
            each_volume = each_cost / TIER_PRICE_1
            for day in window:
                day["dayEleCost"] = round(each_cost, 2)
                day["dayEleNum"] = round(each_volume, 2)
            return

        factor = target / current
        cum_raw_cost = 0.0
        cum_raw_volume = 0.0
        cum_cost = 0.0
        cum_volume = 0.0

        for day in window:
            cum_raw_cost += float(day.get("dayEleCostRaw", 0) or 0)
            cum_raw_volume += float(day.get("dayEleNumRaw", 0) or 0)

            cost = round(round(cum_raw_cost * factor, 2) - cum_cost, 2)
            volume = round(round(cum_raw_volume * factor, 2) - cum_volume, 2)

            if cost < 0:
                cost = 0.0
            if volume <= 0 and cost > 0:
                volume = 0.01

            day["dayEleCost"] = cost
            day["dayEleNum"] = volume
            cum_cost = round(cum_cost + cost, 2)
            cum_volume = round(cum_volume + volume, 2)

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

        # dayEleNum / dayEleCost 为最终对外值（可能被校准改写）；
        # dayEleNumRaw / dayEleCostRaw 为未校准的基准估算，校准按它按比例缩放。
        return {
            "day": date_str,
            "dayEleNum": daily_volume,
            "dayEleCost": round(daily_cost, 2),
            "dayEleNumRaw": daily_volume,
            "dayEleCostRaw": round(daily_cost, 2),
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
        self._attr_device_info = build_device_info(client_code)

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
        summary = self.coordinator.summary or {}

        if self.coordinator.data:
            # 日均消费 / 剩余天数由 coordinator 统一计算（已校准时使用校准日均）
            if summary:
                attrs["日均消费"] = summary.get("avg_daily_cost", 0)
                attrs["剩余天数"] = summary.get("remaining_days")
                attrs["预付费"] = "否"
                attrs["上次充值日期"] = summary.get("reference_date")
                attrs["上次充值金额"] = summary.get("reference_amount")
                attrs["距上次充值天数"] = summary.get("days_since_recharge")

                calibration = summary.get("calibration") or {}
                if summary.get("calibrated"):
                    attrs["校准信息"] = {
                        "已校准": True,
                        "校准日期": calibration.get("date"),
                        "校准余额": calibration.get("balance"),
                        "区间累计消耗": calibration.get("used"),
                        "区间天数": calibration.get("span_days"),
                        "校准日均消费": calibration.get("avg_daily_cost"),
                        "校准后推算天数": calibration.get("tail_days"),
                    }
                else:
                    attrs["校准信息"] = {
                        "已校准": False,
                        "校准日期": None,
                        "校准余额": None,
                        "说明": "未配置校准，日均消费与余额均为估算值",
                    }

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


class XianWaterTotalCostSensor(SensorEntity):
    """累计水费传感器，供 HA 能源面板的成本跟踪使用。

    取值 = 全部日费用（dayEleCost）之和。

    属性选择说明：
    - `device_class = MONETARY` + `state_class = TOTAL`：HA 的
      `DEVICE_CLASS_STATE_CLASSES[MONETARY]` 只允许 `total`。Recorder 会为它生成
      长期统计，于是它可以直接出现在「能源」面板 → 水 → 成本跟踪的
      「使用跟踪总成本的实体」（stat_cost）下拉里。
    - 必须是 `total` 而不是 `total_increasing`：水费做过校准（`_rescale_days`
      会重算历史日费用），累计值允许下降；`total_increasing` 遇到下降会被
      recorder 当成换表重置，把整个数值记成一笔新消费。
    - 这条统计是**实体统计**（statistic_id 就是 `sensor.xxx`），与累计用水的外部
      统计（`xian_water:total_water_xxx`）互不相干，不会重复计量。
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "元"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: XianWaterCoordinator, config: dict):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self.config = config
        client_code = config.get("client_code", "")
        self._client_code = client_code
        self._attr_unique_id = f"xian_water_{client_code}_total_cost"
        self._attr_name = f"西安水费 {client_code} 累计水费"
        self._attr_icon = "mdi:cash-multiple"
        self._attr_device_info = build_device_info(client_code)
        if client_code:
            self.entity_id = f"sensor.xian_water_{client_code}_total_cost"

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.data is not None and bool(
            self.coordinator.data.get("dayList")
        )

    @property
    def native_value(self):
        """Return lifetime cumulative water cost in CNY."""
        if not self.coordinator.data:
            return 0
        return round(
            sum(
                float(day.get("dayEleCost", 0) or 0)
                for day in self.coordinator.data.get("dayList", [])
            ),
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes."""
        data = self.coordinator.data or {}
        current_year = str(datetime.now().year)
        current_year_cost = next(
            (
                item.get("yearEleCost", 0)
                for item in data.get("yearList", [])
                if item.get("year") == current_year
            ),
            0,
        )
        return {
            "当年水费": round(float(current_year_cost or 0), 2),
            "累计用水": round(
                sum(
                    float(day.get("dayEleNum", 0) or 0)
                    for day in data.get("dayList", [])
                ),
                2,
            ),
            "数据源": "西安水务",
            "最后同步日期": self.coordinator.last_update_time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))


class XianWaterTotalWaterSensor(SensorEntity):
    """累计用水量传感器，供 HA 能源面板的「水消耗」使用。

    取值 = 全部日用水量之和（storage 的 dayList 只增不删、不重算历史日，故单调不减）。

    刻意**不设置** state_class：statistics.py 已把同一份日用量回填为外部长期统计
    （statistic_id `xian_water:total_water_<client_code>`，含完整历史）。若本实体再带
    state_class，Recorder 会为它额外生成一份同源统计，两条会并列出现在能源面板的
    数据源下拉里、且显示名相同，极易被同时选中，导致用水量被算两遍。
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: XianWaterCoordinator, config: dict):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self.config = config
        client_code = config.get("client_code", "")
        self._client_code = client_code
        self._attr_unique_id = f"xian_water_{client_code}_total_water"
        self._attr_name = f"西安水费 {client_code} 累计用水"
        self._attr_icon = "mdi:water"
        self._attr_device_info = build_device_info(client_code)
        if client_code:
            self.entity_id = f"sensor.xian_water_{client_code}_total_water"

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.data is not None and bool(
            self.coordinator.data.get("dayList")
        )

    @property
    def native_value(self):
        """Return lifetime cumulative water usage in m³."""
        if not self.coordinator.data:
            return 0
        return round(
            sum(
                float(day.get("dayEleNum", 0) or 0)
                for day in self.coordinator.data.get("dayList", [])
            ),
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes."""
        data = self.coordinator.data or {}
        current_year = str(datetime.now().year)
        current_year_num = next(
            (
                item.get("yearEleNum", 0)
                for item in data.get("yearList", [])
                if item.get("year") == current_year
            ),
            0,
        )
        return {
            "当年用水": round(float(current_year_num or 0), 2),
            "数据源": "西安水务",
            "最后同步日期": self.coordinator.last_update_time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
