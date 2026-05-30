"""Config flow for 西安水务 integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    NAME,
    CONF_CLIENT_CODE,
    CONF_CLIENT_TYPE,
    CONF_CID,
    DEFAULT_CLIENT_TYPE,
)
from .http_client import XianWaterClient

_LOGGER = logging.getLogger(__name__)

class XianWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 西安水务."""

    VERSION = 1
    
    async def async_step_import(self, import_info=None) -> FlowResult:
        """Handle import from configuration."""
        return await self.async_step_user(import_info)

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            client = XianWaterClient(
                user_input[CONF_CLIENT_CODE],
                user_input[CONF_CLIENT_TYPE],
                user_input[CONF_CID],
            )

            try:
                result = await client.async_get_data()
                await client.async_close()
                
                if result:
                    return self.async_create_entry(
                        title=f"{NAME} - {user_input[CONF_CLIENT_CODE]}",
                        data=user_input,
                    )
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLIENT_CODE): str,
                    vol.Required(CONF_CLIENT_TYPE, default=DEFAULT_CLIENT_TYPE): str,
                    vol.Required(CONF_CID): str,
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
            return self.async_create_entry(title="", data=user_input)

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
                }
            ),
        )