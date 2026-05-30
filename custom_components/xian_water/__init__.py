"""The 西安水务 integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_CLIENT_CODE,
    CONF_CLIENT_TYPE,
    CONF_CID,
    DEFAULT_CLIENT_CODE,
    DEFAULT_CLIENT_TYPE,
    DEFAULT_CID,
)
from .http_client import XianWaterClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the 西安水务 component."""
    hass.data.setdefault(DOMAIN, {})
    
    # If no config entry exists, create one with default values
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data={
                    CONF_CLIENT_CODE: DEFAULT_CLIENT_CODE,
                    CONF_CLIENT_TYPE: DEFAULT_CLIENT_TYPE,
                    CONF_CID: DEFAULT_CID,
                },
            )
        )
    
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 西安水务 from a config entry."""
    client = XianWaterClient(
        entry.data.get(CONF_CLIENT_CODE, DEFAULT_CLIENT_CODE),
        entry.data.get(CONF_CLIENT_TYPE, DEFAULT_CLIENT_TYPE),
        entry.data.get(CONF_CID, DEFAULT_CID),
    )

    # Create a custom coordinator with daily 12:00 update
    coordinator = DailyUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=client.async_get_data,
        entry=entry,
    )

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator and client for platforms to access
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up all platforms for this device/entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Add update listener for config entry changes
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Clean up
    if unload_ok:
        coordinator = hass.data[DOMAIN][entry.entry_id]
        client = coordinator.update_method.__self__
        await client.async_close()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when it changed."""
    await hass.config_entries.async_reload(entry.entry_id)


class DailyUpdateCoordinator(DataUpdateCoordinator):
    """Custom coordinator that updates daily at 12:00."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_method: callable,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the daily update coordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_method=update_method,
            update_interval=timedelta(days=1),  # Default interval, will be overridden
        )
        self._entry = entry
        self._update_task = None
        self._last_successful_data = None  # Store last successful data

    async def async_config_entry_first_refresh(self) -> None:
        """Perform first refresh and start daily update schedule."""
        await super().async_config_entry_first_refresh()
        # Store successful data after first refresh
        if self.data is not None:
            self._last_successful_data = self.data
        self._start_daily_update()

    async def async_refresh(self) -> None:
        """Refresh data with error handling to preserve previous data."""
        try:
            # Try to get new data
            data = await self._async_update_data()
            
            if data is not None:
                # Update successful, store the new data
                self._last_successful_data = data
                self.async_set_updated_data(data)
                _LOGGER.debug("Data updated successfully")
            else:
                # API returned None (failed), keep previous data
                if self._last_successful_data is not None:
                    _LOGGER.warning("API update failed, keeping previous data")
                    # Keep the last successful data but don't trigger listeners
                    # This prevents unnecessary state changes
                else:
                    _LOGGER.error("No data available and no previous data to fallback to")
                    
        except Exception as err:
            _LOGGER.warning("Error during refresh, keeping previous data: %s", err)
            # Don't re-raise the exception to prevent breaking the update loop

    def _start_daily_update(self) -> None:
        """Start the daily update schedule."""
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
        
        self._update_task = self.hass.async_create_task(self._daily_update_loop())

    async def _daily_update_loop(self) -> None:
        """Main loop for daily updates at 12:00."""
        while True:
            try:
                # Calculate time until next 12:00
                now = datetime.now()
                next_update = datetime(now.year, now.month, now.day, 12, 0, 0)
                
                # If it's already past 12:00 today, schedule for tomorrow
                if now >= next_update:
                    next_update = next_update + timedelta(days=1)
                
                # Calculate seconds until next update
                wait_seconds = (next_update - now).total_seconds()
                
                _LOGGER.debug(
                    "Next update scheduled for %s (in %d seconds)",
                    next_update,
                    wait_seconds
                )
                
                # Wait until next update time
                await asyncio.sleep(wait_seconds)
                
                # Perform the update with error handling to preserve data
                try:
                    await self.async_refresh()
                    _LOGGER.debug("Daily update completed at 12:00")
                except Exception as err:
                    _LOGGER.warning("API update failed at 12:00, keeping previous data: %s", err)
                    # Continue with next day's update even if this one failed
                    
            except asyncio.CancelledError:
                # Task was cancelled, exit the loop
                break
            except Exception as err:
                _LOGGER.error("Error in daily update loop: %s", err)
                # Wait 1 hour before retrying on error
                await asyncio.sleep(3600)

    async def async_unload(self) -> None:
        """Clean up when coordinator is unloaded."""
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass