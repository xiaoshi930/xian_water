"""HTTP client for 西安水务."""
import logging
import os
from datetime import datetime, timedelta
import aiohttp
import async_timeout
import json

from .const import API_ENDPOINT

_LOGGER = logging.getLogger(__name__)

class XianWaterClient:
    """西安水务 API client."""

    def __init__(self, client_code, client_type, cid, hass=None):
        """Initialize the client."""
        self.client_code = client_code
        self.client_type = client_type
        self.cid = cid
        self.session = None
        self.hass = hass
        self.cache_file = None
        
        if hass:
            # 基于用户信息创建唯一的缓存文件名
            cache_key = f"xian_water_{client_code}_{cid}"
            self.cache_file = os.path.join(hass.config.config_dir, f"{cache_key}_cache.json")

    def _load_cache(self):
        """从缓存文件加载数据。"""
        if not self.cache_file or not os.path.exists(self.cache_file):
            return None
            
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                
            # 检查缓存是否过期（6小时内）
            cache_time = datetime.fromisoformat(cache_data.get('timestamp', '1970-01-01'))
            if datetime.now() - cache_time < timedelta(hours=6):
                _LOGGER.info("使用西安水务的缓存数据")
                return cache_data.get('data')
            else:
                _LOGGER.info("西安水务缓存数据已过期")
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            _LOGGER.error(f"读取缓存文件失败: {error}")
        
        return None

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

    async def async_get_data(self):
        """Get data from the API."""
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
            async with async_timeout.timeout(120):
                response = await self.session.post(
                    API_ENDPOINT,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )
                response_json = await response.json()
                
                if not response_json.get("success", False):
                    _LOGGER.error("API request failed: %s", response_json.get("message", "Unknown error"))
                    # API返回失败时，尝试使用缓存
                    cached_data = self._load_cache()
                    if cached_data:
                        _LOGGER.info("API失败，使用缓存数据")
                        return cached_data
                    return None
                
                processed_data = self._process_data(response_json)
                # 如果成功获取数据，保存到缓存
                if processed_data and self.hass:
                    # 在异步上下文中安全地保存缓存
                    await self.hass.async_add_executor_job(self._save_cache, processed_data)
                
                return processed_data
        except aiohttp.ClientError as err:
            _LOGGER.error("Error requesting data from API: %s", err)
            # 网络错误时，尝试使用缓存
            cached_data = self._load_cache()
            if cached_data:
                _LOGGER.info("API访问失败，使用缓存数据")
                return cached_data
            return None
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Unexpected error: %s", err)
            # 其他错误时，尝试使用缓存
            cached_data = self._load_cache()
            if cached_data:
                _LOGGER.info("发生错误，使用缓存数据")
                return cached_data
            return None

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