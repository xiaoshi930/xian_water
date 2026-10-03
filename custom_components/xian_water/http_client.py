"""HTTP client for 西安水务."""
import logging
import os
from datetime import datetime, timedelta
import aiohttp
import async_timeout
import json

from .const import API_ENDPOINT

_LOGGER = logging.getLogger(__name__)

# 缓存有效期：6 小时内认为缓存「新鲜」
CACHE_TTL = timedelta(hours=6)
# 单次请求超时（秒）。配置流里只做一次校验，不能让用户干等 2 分钟。
DEFAULT_TIMEOUT = 30


class XianWaterClient:
    """西安水务 API client."""

    def __init__(self, client_code, client_type, cid, hass=None, timeout=DEFAULT_TIMEOUT):
        """Initialize the client."""
        self.client_code = client_code
        self.client_type = client_type
        self.cid = cid
        self.session = None
        self.hass = hass
        self.timeout = timeout
        self.cache_file = None

        if hass:
            # 基于用户信息创建唯一的缓存文件名
            cache_key = f"xian_water_{client_code}_{cid}"
            self.cache_file = os.path.join(hass.config.config_dir, f"{cache_key}_cache.json")

    def _load_cache(self, allow_expired: bool = False):
        """从缓存文件加载数据。

        allow_expired=True 时忽略 6 小时有效期 —— API 不可用时（如 502），
        过期的本地 JSON 仍然比「什么都拿不到」有用。
        """
        if not self.cache_file or not os.path.exists(self.cache_file):
            return None

        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # 检查缓存是否过期（6小时内）
            cache_time = datetime.fromisoformat(cache_data.get('timestamp', '1970-01-01'))
            age = datetime.now() - cache_time
            if allow_expired:
                _LOGGER.info(
                    "使用本地缓存 JSON（%s，已存放 %s）: %s",
                    "允许过期" if age >= CACHE_TTL else "未过期",
                    age,
                    self.cache_file,
                )
                return cache_data.get('data')
            if age < CACHE_TTL:
                _LOGGER.info("使用西安水务的缓存数据")
                return cache_data.get('data')
            _LOGGER.info("西安水务缓存数据已过期")
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
            _LOGGER.error("读取缓存文件失败: %s", error)

        return None

    def _fallback(self, reason, allow_stale_cache: bool):
        """API 不可用时统一回落到本地缓存 JSON。"""
        _LOGGER.warning(
            "西安水务 API 不可用（%s），尝试改用本地 JSON 缓存: %s",
            reason,
            self.cache_file or "未配置（未传入 hass）",
        )
        return self._load_cache(allow_expired=allow_stale_cache)

    def _save_cache(self, data):
        """保存数据到缓存文件。"""
        if not self.cache_file:
            return
            
        try:
            cache_data = {
                'timestamp': datetime.now().isoformat(),
                'data': data
            }
            
            # 确保目录存在
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
                
            _LOGGER.info("已保存西安水务数据到缓存")
        except (IOError, OSError) as error:
            _LOGGER.error(f"保存缓存文件失败: {error}")

    async def async_get_data(self, allow_stale_cache: bool = False):
        """Get data from the API.

        allow_stale_cache=True 时，API 失败后允许使用**已过期**的本地缓存 JSON：
        配置流用它来保证「API 挂了也能把集成加进来」。
        """
        if self.session is None:
            self.session = aiohttp.ClientSession()

        payload = {
            "clientCode": self.client_code,
            "clientType": self.client_type,
            "cid": self.cid,
            "page": {
                "current": 1,
                "size": 10
            }
        }

        try:
            async with async_timeout.timeout(self.timeout):
                response = await self.session.post(
                    API_ENDPOINT,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )

                # 502/503 之类会直接返回错误页（text/html）。先看状态码，
                # 别等 json() 抛 ContentTypeError 才反应。
                if response.status != 200:
                    return self._fallback(f"HTTP {response.status}", allow_stale_cache)

                # content_type=None：部分网关用 text/html 回一句 JSON 或错误页，
                # 强制按 JSON 解析，解析不了再回落缓存。
                try:
                    response_json = await response.json(content_type=None)
                except ValueError as err:
                    return self._fallback(f"响应不是合法 JSON: {err}", allow_stale_cache)

                if not response_json.get("success", False):
                    _LOGGER.error(
                        "API request failed: %s",
                        response_json.get("message", "Unknown error"),
                    )
                    return self._fallback("success=false", allow_stale_cache)

                processed_data = self._process_data(response_json)
                # 如果成功获取数据，保存到缓存
                if processed_data and self.hass:
                    # 在异步上下文中安全地保存缓存
                    await self.hass.async_add_executor_job(self._save_cache, processed_data)

                return processed_data
        except aiohttp.ClientError as err:
            return self._fallback(err, allow_stale_cache)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Unexpected error: %s", err)
            return self._fallback(err, allow_stale_cache)

    def _process_data(self, response_json):
        """Process the API response data."""
        try:
            records = response_json.get("resultData", {}).get("records", [])
            if not records:
                _LOGGER.warning("No records found in API response")
                return None

            data = [{"date": record["pdate"], "cost": record["rlje"]} for record in records]
            return self._calculate_water_usage(data)
        except KeyError as err:
            _LOGGER.error("Missing expected field in API response: %s", err)
            return None

    def _calculate_water_usage(self, data):
        """Calculate water usage statistics."""
        try:
            first_date = datetime.strptime(data[0]["date"], "%Y-%m-%d")
            last_date = datetime.strptime(data[-1]["date"], "%Y-%m-%d")
            s1 = abs((first_date - last_date).days)
            
            if s1 == 0:
                _LOGGER.warning("Cannot calculate usage: first and last dates are the same")
                return None
            
            a = sum(float(record["cost"]) for record in data[1:])
            c = a / s1  # Daily cost
            
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            s2 = abs((today - first_date).days)
            
            b = float(data[0]["cost"])
            balance = b - c * s2
            usage_days = balance / c if c > 0 else 0
            
            return {
                "price": round(c, 2),
                "balance": round(balance, 2),
                "usage_days": int(usage_days),
                "data": data
            }
        except (ValueError, ZeroDivisionError) as err:
            _LOGGER.error("Error calculating water usage: %s", err)
            return None

    async def async_close(self):
        """Close the session."""
        if self.session:
            await self.session.close()
            self.session = None