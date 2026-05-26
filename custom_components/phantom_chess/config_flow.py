"""Config flow for the Phantom Chess Board integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_LICHESS_TOKEN,
    CONF_LICHESS_USER,
    DOMAIN,
    LICHESS_ACCOUNT_URL,
)

_LOGGER = logging.getLogger(__name__)

# BLE address canonical form: uppercase, colon-separated. HA's Bluetooth
# integration (and bleak under it) normalizes all discovered addresses to
# this form, so any stored unique_id MUST match it or _abort_if_unique_id_
# configured() will silently fail to abort and the same board will keep
# showing up as "Discovered" forever.
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


def _normalize_ble_address(raw: str | None) -> str | None:
    """Return the canonical BLE-address form HA's BT stack uses, or None if invalid.

    Accepts colon-separated or dash-separated input, any case. Returns
    uppercase colon-separated. Anything that isn't a valid 6-octet MAC
    returns None — callers should treat that as user input error.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace("-", ":")
    if not _MAC_RE.match(cleaned):
        return None
    return cleaned.upper()


class PhantomChessConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Phantom Chess Board."""

    # VERSION 4 (2026-05-24): v3 had a priority bug in the "both lowercase
    # AND uppercase variants exist for the same suffix" branch — it kept
    # the auto-created uppercase entry (with `_2` entity_id suffix) and
    # deleted the user's original clean entity_id. v4 renames any
    # `_2`-suffix entity_ids back to their base form when the base is free.
    # The v3 logic itself is also fixed in this release so a fresh
    # upgrader in the duplicate state would pick the cleaner entity_id;
    # v3→v4 handles the residual mess on installs that already went
    # through the broken v3 step.
    VERSION = 4

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._discovered_devices: dict[str, str] = {}  # address → name
        # Used by the reauth flow to remember the originating entry's data.
        self._reauth_entry_data: dict[str, Any] | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle discovery via Bluetooth."""
        # HA's BT stack already gives us an uppercase address, but we still
        # canonicalize defensively so a future bleak/HA change can't break
        # the abort path. _abort_if_unique_id_configured does a string
        # equality check, so case must match what's stored in the entry.
        address = _normalize_ble_address(discovery_info.address) or discovery_info.address
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._discovered_address = address
        self._discovered_name = discovery_info.name or f"Phantom {address}"

        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm Bluetooth discovery."""
        if user_input is not None:
            # Advance to the Lichess-token step WITHOUT forwarding
            # bluetooth_confirm's (empty) user_input — that step expects
            # `user_input=None` to render its own form, and
            # `user_input={'lichess_token': '...'}` when the user submits
            # it. Forwarding the bluetooth_confirm empty dict caused a
            # KeyError on `user_input[CONF_LICHESS_TOKEN]` which HA
            # surfaced as "Unknown error occurred" in the UI. Fixed
            # 2026-05-25 after the clean-install validation exposed it.
            return await self.async_step_lichess_token()

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovered_name,
                "address": self._discovered_address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup — show discovered boards or allow manual MAC entry."""
        errors: dict[str, str] = {}

        # Collect any Phantom boards already discovered by HA's BT scanner.
        # Normalize stored entry addresses so comparison is case-insensitive
        # — protects against legacy entries that were stored mixed-case
        # before VERSION 2's normalization landed.
        current_addresses = {
            (_normalize_ble_address(entry.data[CONF_BLE_ADDRESS])
             or entry.data[CONF_BLE_ADDRESS])
            for entry in self._async_current_entries()
            if CONF_BLE_ADDRESS in entry.data
        }

        for info in async_discovered_service_info(self.hass, connectable=True):
            normalized = _normalize_ble_address(info.address) or info.address
            if (
                info.name
                and info.name.startswith("Phantom")
                and normalized not in current_addresses
            ):
                self._discovered_devices[normalized] = (
                    info.name or f"Phantom {normalized}"
                )

        if user_input is not None:
            raw_address = user_input.get(CONF_BLE_ADDRESS, "")
            address = _normalize_ble_address(raw_address)
            if address is None:
                errors[CONF_BLE_ADDRESS] = "invalid_ble_address"
            else:
                name = user_input.get(CONF_DEVICE_NAME) or f"Phantom {address}"

                if address in self._discovered_devices:
                    name = self._discovered_devices[address]

                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()

                self._discovered_address = address
                self._discovered_name = name
                return await self.async_step_lichess_token()

        # Build address selector — discovered devices + manual entry option
        address_options: list[str] = list(self._discovered_devices.keys())

        schema = vol.Schema(
            {
                vol.Required(CONF_BLE_ADDRESS): (
                    vol.In(
                        {
                            addr: f"{name} ({addr})"
                            for addr, name in self._discovered_devices.items()
                        }
                    )
                    if self._discovered_devices
                    else str
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "discovered": ", ".join(self._discovered_devices.values()) or "none found yet",
            },
        )

    async def async_step_lichess_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the Lichess Board API token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_LICHESS_TOKEN].strip()
            username = await self._validate_lichess_token(token)
            if username is None:
                errors[CONF_LICHESS_TOKEN] = "invalid_lichess_token"
            else:
                return self.async_create_entry(
                    title=self._discovered_name or "Phantom Chess Board",
                    data={
                        CONF_BLE_ADDRESS: self._discovered_address,
                        CONF_DEVICE_NAME: self._discovered_name,
                        CONF_LICHESS_TOKEN: token,
                        CONF_LICHESS_USER: username,
                    },
                )

        return self.async_show_form(
            step_id="lichess_token",
            data_schema=vol.Schema({vol.Required(CONF_LICHESS_TOKEN): str}),
            errors=errors,
            description_placeholders={
                "device": self._discovered_name or self._discovered_address or "board",
            },
        )

    async def _validate_lichess_token(self, token: str) -> str | None:
        """Return Lichess username if token is valid, else None."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                LICHESS_ACCOUNT_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("username")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Lichess token validation failed: %s", err)
        return None

    # ── Reauth flow (Task #20, 2026-05-16 release-readiness) ─────────────
    # Triggered by the integration via `self.async_start_reauth(entry)` when
    # Lichess returns 401 / 403 on a stream or move request. Without this,
    # a user whose token expires has to delete and re-create the integration
    # entry — losing entity history and re-doing BLE pairing.

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start the reauth flow — preserves entry, lets user re-enter token."""
        self._reauth_entry_data = entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the token-entry form and update the existing entry on success."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_LICHESS_TOKEN].strip()
            username = await self._validate_lichess_token(token)
            if username is None:
                errors[CONF_LICHESS_TOKEN] = "invalid_lichess_token"
            else:
                # Update the existing entry's data with the new token. We
                # also refresh the username in case the token belongs to a
                # different Lichess account than before.
                entry = self._get_reauth_entry()
                if entry is not None:
                    new_data = {
                        **entry.data,
                        CONF_LICHESS_TOKEN: token,
                        CONF_LICHESS_USER: username,
                    }
                    self.hass.config_entries.async_update_entry(entry, data=new_data)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
                # Fall through to error display if entry is gone (rare race).
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_LICHESS_TOKEN): str}),
            errors=errors,
            description_placeholders={
                "device": (self._reauth_entry_data or {}).get(
                    CONF_DEVICE_NAME, "Phantom Chess Board"
                ),
            },
        )

    def _get_reauth_entry(self) -> ConfigEntry | None:
        """Look up the ConfigEntry the reauth flow is targeting."""
        # HA stores the source entry_id in context['entry_id'] during reauth.
        entry_id = self.context.get("entry_id")
        if not entry_id:
            return None
        return self.hass.config_entries.async_get_entry(entry_id)

    # ── Options flow ─────────────────────────────────────────────────────
    # Registers the static helper HA calls to launch the user's options UI.

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PhantomChessOptionsFlow:
        return PhantomChessOptionsFlow(config_entry)


class PhantomChessOptionsFlow(OptionsFlow):
    """Per-entry options: TTS forwarding targets, debug dumps, token rotation.

    Added 2026-05-16 (Task #20). The integration previously had no options
    flow at all — users couldn't rotate their Lichess token, change debug
    settings, or wire TTS without delete-and-recreate.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options or {}
        schema = vol.Schema(
            {
                vol.Optional(
                    "tts_service",
                    description={"suggested_value": current.get("tts_service", "")},
                ): str,
                vol.Optional(
                    "tts_media_player_entity_id",
                    description={
                        "suggested_value": current.get("tts_media_player_entity_id", "")
                    },
                ): str,
                vol.Optional(
                    "debug_dump",
                    default=bool(current.get("debug_dump", False)),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
