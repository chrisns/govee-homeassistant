"""Independent "main downlight" light entity for Ceiling Light Pro devices.

Issue #131/#164 follow-up: on MAIN_LIGHT_LAN_TOGGLE_SKUS, neither the cloud
``mainLightToggle`` capability nor the master ``powerSwitch`` reliably
control the main panel independently of the ring/segments — see
``coordinator.async_send_main_light_toggle`` for the full story. This module
is the payoff: a real light entity backed by the one command that does work.
"""

from __future__ import annotations

import logging
from typing import Any

# mypy --strict: HA's `light` module re-exports without __all__, so
# `--no-implicit-reexport` raises attr-defined for each member. The
# suppression is upstream-stub-bound, not a real type error here.
from homeassistant.components.light import (  # type: ignore[attr-defined]
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.restore_state import RestoreEntity

from ..const import DOMAIN, SUFFIX_MAIN_LIGHT_LAN
from ..coordinator import GoveeCoordinator
from ..entity import GoveeEntity
from ..models import BrightnessCommand, ColorCommand, ColorTempCommand, GoveeDevice, RGBColor

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# HA's brightness attribute is always 0-255 by protocol definition (not a
# device-specific value) — matches light.py's own HA_BRIGHTNESS_MAX, kept as
# a plain literal here rather than imported to avoid a circular import
# (light.py imports this module to build GoveeMainLightEntity).
_HA_BRIGHTNESS_MAX = 255


def main_light_optimistic_signal(device_id: str) -> str:
    """Per-device dispatcher signal carrying the main light's last write.

    GoveeMainSegmentsGroupEntity listens on this (alongside the segments'
    equivalent signal) so it knows, without ever guessing, whether the main
    panel is already in the state its own turn_on/turn_off wants — needed
    because the underlying LAN command is a blind toggle, not a set.
    """
    return f"{DOMAIN}_main_light_optimistic_{device_id}"


class GoveeMainLightEntity(GoveeEntity, LightEntity, RestoreEntity):
    """The Ceiling Light Pro's main downlight, controlled independently.

    On/off is a purely optimistic, RestoreEntity-persisted local flag — the
    LAN command is a blind toggle with no readback, so this entity (like
    the segment entities, for the same underlying reason: Govee reports no
    real state for this) is the only source of truth for "is main on".
    Brightness and colour temperature are NOT optimistic: they still go
    through the existing cloud BrightnessCommand/ColorTempCommand/
    ColorCommand path, which reports real values back via
    coordinator.get_state() same as it always has for this device — only the
    on/off transition was ever broken.
    """

    _attr_translation_key = "govee_main_light_lan"

    def __init__(self, coordinator: GoveeCoordinator, device: GoveeDevice) -> None:
        """Initialize the main light entity."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}{SUFFIX_MAIN_LIGHT_LAN}"
        self._attr_translation_placeholders = {"device_name": device.name}

        modes: set[ColorMode] = set()
        if device.supports_rgb:
            modes.add(ColorMode.RGB)
        if device.supports_color_temp:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes and device.supports_brightness:
            modes.add(ColorMode.BRIGHTNESS)
        if not modes:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes

        self._brightness_min, self._brightness_max = device.brightness_range

        # Optimistic on/off — see class docstring.
        self._is_on = True

    def _ha_to_device_brightness(self, ha_brightness: int) -> int:
        ratio = ha_brightness / _HA_BRIGHTNESS_MAX
        result = int(
            self._brightness_min + ratio * (self._brightness_max - self._brightness_min)
        )
        return max(self._brightness_min, min(self._brightness_max, result))

    def _device_to_ha_brightness(self, device_brightness: int) -> int:
        device_range = self._brightness_max - self._brightness_min
        if device_range <= 0:
            return 0
        result = int(
            (device_brightness - self._brightness_min)
            / device_range
            * _HA_BRIGHTNESS_MAX
        )
        return max(0, min(_HA_BRIGHTNESS_MAX, result))

    @property
    def color_mode(self) -> ColorMode:
        """Return current colour mode, always within supported_color_modes."""
        state = self.device_state
        modes = self.supported_color_modes or {ColorMode.ONOFF}
        if state and state.color_temp_kelvin is not None and ColorMode.COLOR_TEMP in modes:
            return ColorMode.COLOR_TEMP
        if state and state.color is not None and ColorMode.RGB in modes:
            return ColorMode.RGB
        if ColorMode.BRIGHTNESS in modes:
            return ColorMode.BRIGHTNESS
        if ColorMode.COLOR_TEMP in modes:
            return ColorMode.COLOR_TEMP
        return ColorMode(next(iter(modes)))

    @property
    def is_on(self) -> bool:
        """Return True if the main panel is on (optimistic, see class docstring)."""
        return self._is_on

    @property
    def brightness(self) -> int | None:
        """Return brightness (0-255) — real, from the coordinator's tracked state."""
        state = self.device_state
        if state is None:
            return None
        return self._device_to_ha_brightness(state.brightness)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        state = self.device_state
        if state and state.color:
            return state.color.as_tuple
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        state = self.device_state
        return state.color_temp_kelvin if state and state.color_temp_kelvin else None

    @property
    def min_color_temp_kelvin(self) -> int:
        temp_range = self._device.color_temp_range
        return temp_range.min_kelvin if temp_range else 2000

    @property
    def max_color_temp_kelvin(self) -> int:
        temp_range = self._device.color_temp_range
        return temp_range.max_kelvin if temp_range else 9000

    async def _async_toggle_if_needed(self, want_on: bool) -> None:
        """Send the LAN toggle only when the tracked state actually needs to flip.

        The command is a blind toggle — sending it when already in the wanted
        state would flip it the WRONG way, not confirm it.
        """
        if self._is_on == want_on:
            return
        sent = await self.coordinator.async_send_main_light_toggle(self._device_id)
        if not sent:
            _LOGGER.warning(
                "Main light LAN toggle failed for %s (%s) — is CONF_LAN_TARGETS "
                "set to '%s=<ip>!' for this device?",
                self._device.name,
                self._device_id,
                self._device_id,
            )
            return
        self._is_on = want_on
        self.async_write_ha_state()
        async_dispatcher_send(
            self.hass, main_light_optimistic_signal(self._device_id), self._is_on
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the main panel on, optionally adjusting brightness/colour."""
        if ATTR_BRIGHTNESS in kwargs:
            device_brightness = self._ha_to_device_brightness(kwargs[ATTR_BRIGHTNESS])
            if not await self.coordinator.async_control_device(
                self._device_id, BrightnessCommand(brightness=device_brightness)
            ):
                _LOGGER.warning("Brightness command failed for %s", self._device_id)

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            if not await self.coordinator.async_control_device(
                self._device_id, ColorCommand(color=RGBColor(r=r, g=g, b=b))
            ):
                _LOGGER.warning("Color command failed for %s", self._device_id)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            if not await self.coordinator.async_control_device(
                self._device_id, ColorTempCommand(kelvin=kwargs[ATTR_COLOR_TEMP_KELVIN])
            ):
                _LOGGER.warning("Color temp command failed for %s", self._device_id)

        await self._async_toggle_if_needed(want_on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the main panel off."""
        await self._async_toggle_if_needed(want_on=False)

    async def async_added_to_hass(self) -> None:
        """Restore previous optimistic on/off state."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state:
            self._is_on = last_state.state == "on"
