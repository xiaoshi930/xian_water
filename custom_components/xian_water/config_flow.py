"""Config flow for 西安水务 integration."""
import json
import logging
import os
from datetime import date, datetime

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    NAME,
    CONF_CLIENT_CODE,
    CONF_CLIENT_TYPE,
    CONF_CID,
    CONF_CALIBRATION_DATE,
    CONF_CALIBRATION_AMOUNT,
    DEFAULT_CLIENT_TYPE,
)
from .http_client import XianWaterClient

_LOGGER = logging.getLogger(__name__)

# 仅用于表单的「离线添加」开关，勾选后跳过 API 校验，不写进 entry.data
CONF_SKIP_API_CHECK = "skip_api_check"

# 校准日期/金额允许清空：空字符串或 None 都会被视图层清掉
_CAL_DATE_VALIDATOR = vol.Any(selector.DateSelector(), vol.In(["", None]))
_CAL_AMOUNT_VALIDATOR = vol.Any(
    selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=1000000,
            step=0.01,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="元",
        )
    ),
    vol.In(["", None]),
)


def _safe_path_part(value) -> str:
    """清掉客户编号 / CID 里的路径分隔符，避免拼出配置目录之外的路径。"""
    text = str(value or "").strip()
    for char in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        text = text.replace(char, "_")
    return text


def _usable_payload(payload) -> bool:
    """判断本地 JSON 里是否有可用数据。

    客户端缓存文件形如 {"timestamp":…, "data": {...}}；
    集成自己的持久化文件是 {"dayList": [...], "balance": …}。
    """
    if not isinstance(payload, dict):
        return False
    cached = payload.get("data")
    if isinstance(cached, list) and cached:
        return True
    if isinstance(cached, dict) and any(cached.get(k) for k in ("data", "balance", "price")):
        return True
    return any(payload.get(key) for key in ("dayList", "monthList", "yearList"))


def find_local_json(hass: HomeAssistant, client_code, cid) -> str | None:
    """API 不可用时，尝试从本地 JSON 里找一份可用的配置/数据。

    依次检查：客户端缓存文件 → 集成自己的持久化文件。
    返回命中的文件路径；文件不存在、解析失败、内容为空都返回 None。
    """
    code = _safe_path_part(client_code)
    candidates = [
        hass.config.path(f"xian_water_{code}_{_safe_path_part(cid)}_cache.json"),
        hass.config.path(f"xian_water_{code}.json"),
    ]

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("本地 JSON 无法读取/解析，跳过: %s (%s)", path, err)
            continue
        if _usable_payload(payload):
            return path
        _LOGGER.debug("本地 JSON 内容为空，跳过: %s", path)

    return None


class XianWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 西安水务."""

    VERSION = 1

    async def async_step_import(self, import_info=None) -> FlowResult:
        """Handle import from configuration."""
        return await self.async_step_user(import_info)

    def _create_entry(self, user_input: dict) -> FlowResult:
        """创建配置项（三个入口共用）。"""
        return self.async_create_entry(
            title=f"{NAME} - {user_input.get(CONF_CLIENT_CODE, '')}",
            data=user_input,
        )

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step.

        API 校验只是「顺手确认一下」，不该成为添加入口：
        1. 勾选「离线添加」→ 直接创建；
        2. 正常走 API（失败时客户端自己会回落本地缓存 JSON）；
        3. API 与缓存都拿不到 → 再找一遍本地 JSON（含过期缓存）；
        4. 仍然没有 → 报 cannot_connect，并提示可勾选「离线添加」。
        """
        errors = {}

        if user_input is not None:
            # 开关只是表单选项，不能落库
            skip_api_check = bool(user_input.pop(CONF_SKIP_API_CHECK, False))
            client_code = user_input.get(CONF_CLIENT_CODE, "")

            if skip_api_check:
                _LOGGER.warning("已跳过 API 校验，离线添加西安水务集成: %s", client_code)
                return self._create_entry(user_input)

            client = XianWaterClient(
                client_code,
                user_input[CONF_CLIENT_TYPE],
                user_input[CONF_CID],
                hass=self.hass,
            )

            try:
                # allow_stale_cache：API 挂了（502 / 网络不通）也要能用本地 JSON 兜底
                result = await client.async_get_data(allow_stale_cache=True)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                result = None
            finally:
                await client.async_close()

            if result:
                return self._create_entry(user_input)

            local_json = await self.hass.async_add_executor_job(
                find_local_json, self.hass, client_code, user_input[CONF_CID]
            )
            if local_json:
                _LOGGER.warning(
                    "西安水务 API 无法访问，改用本地 JSON 添加集成: %s", local_json
                )
                return self._create_entry(user_input)

            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLIENT_CODE): str,
                    vol.Required(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): str,
                    vol.Required(CONF_CID): str,
                    vol.Optional(CONF_SKIP_API_CHECK, default=False): cv.boolean,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return XianWaterOptionsFlowHandler(config_entry)


class XianWaterOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            # DateSelector 只校验、原样返回入参：从 YAML/代码路径传入的 date/datetime
            # 对象会被原样写进 options，所以要在这里统一规范化成字符串
            raw_date = user_input.get(CONF_CALIBRATION_DATE)
            if isinstance(raw_date, datetime):
                user_input[CONF_CALIBRATION_DATE] = raw_date.strftime("%Y-%m-%d")
            elif isinstance(raw_date, date):
                user_input[CONF_CALIBRATION_DATE] = raw_date.isoformat()
            return self.async_create_entry(title="", data=user_input)

        current = {**self._config_entry.data, **self._config_entry.options}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CLIENT_CODE,
                        default=self._config_entry.data.get(CONF_CLIENT_CODE),
                    ): str,
                    vol.Required(
                        CONF_CLIENT_TYPE,
                        default=self._config_entry.data.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE),
                    ): str,
                    vol.Required(
                        CONF_CID,
                        default=self._config_entry.data.get(CONF_CID),
                    ): str,
                    vol.Optional(
                        CONF_CALIBRATION_DATE,
                        description={
                            "suggested_value": current.get(CONF_CALIBRATION_DATE) or None
                        },
                    ): _CAL_DATE_VALIDATOR,
                    vol.Optional(
                        CONF_CALIBRATION_AMOUNT,
                        description={
                            "suggested_value": current.get(CONF_CALIBRATION_AMOUNT)
                        },
                    ): _CAL_AMOUNT_VALIDATOR,
                }
            ),
        )