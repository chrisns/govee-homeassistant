"""Independent "main downlight" light entity for Ceiling Light Pro devices.

Issue #131/#164 follow-up: on MAIN_LIGHT_TOGGLE_SKUS, neither the cloud
``mainLightToggle`` capability nor the master ``powerSwitch`` control the main
panel independently of the ring/segments — ``mainLightToggle`` is confirmed
dead (issue #131), and ``powerSwitch=0`` kills the segments too (confirmed
live: turning segments on while powered off wakes main as a side effect, and
powering off while segments are on kills them too — it is a whole-device
switch, not a per-zone one).

A reverse-engineered ptReal command that toggles ONLY the main panel was
found and confirmed working when sent by the real Govee app (byte-for-byte
captured twice, live, cleanly correlated to real button presses) — but every
attempt to send those exact bytes from this integration (LAN, MQTT, several
envelope variations matching docs/govee-protocol-reference.md exactly) had
zero physical effect despite the device acking receipt. Root cause
undetermined; parked pending a packet capture of the app's real session.

This module instead drives the main panel via ``BrightnessCommand`` alone,
never ``powerSwitch``: "off" dims to the device's minimum brightness, "on"
restores it. This is a REAL, already-proven-reliable REST command, confirmed
live to dim only the main panel while segments — which have their own
independent brightness/colour — are untouched. On/off state is read directly
from the device's real reported brightness, not tracked optimistically.
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
from homeassistant.helpers.restore_state import RestoreEntity

from ..const import SUFFIX_MAIN_LIGHT_TOGGLE
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


class GoveeMainLightEntity(GoveeEntity, LightEntity, RestoreEntity):
    """The Ceiling Light Pro's main downlight, dimmed independently.

    On/off is derived from the device's REAL reported brightness (at/below
    the device's minimum reads as off) — not optimistic, since brightness is
    a normal polled/reported value for this device, unlike the blind ptReal
    toggle this replaced. Only ``_last_on_brightness`` (what to restore to on
    the next turn_on) is RestoreEntity-persisted, as a nice-to-have across
    restarts, not as the source of truth for is_on.
    """

    _attr_translation_key = "govee_main_light_lan"

    def __init__(self, coordinator: GoveeCoordinator, device: GoveeDevice) -> None:
        """Initialize the main light entity."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}{SUFFIX_MAIN_LIGHT_TOGGLE}"
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
        self._last_on_brightness = self._brightness_max

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
        """Return True if the device's real brightness is above the device minimum."""
        state = self.device_state
        if state is None:
            return False
        return state.brightness > self._brightness_min

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

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the main panel on, optionally adjusting brightness/colour."""
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

        if ATTR_BRIGHTNESS in kwargs:
            target = self._ha_to_device_brightness(kwargs[ATTR_BRIGHTNESS])
        else:
            target = self._last_on_brightness
        if target <= self._brightness_min:
            target = self._brightness_max

        if not await self.coordinator.async_control_device(
            self._device_id, BrightnessCommand(brightness=target)
        ):
            _LOGGER.warning("Brightness command failed for %s", self._device_id)
            return
        self._last_on_brightness = target
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Dim the main panel to the device's minimum brightness."""
        state = self.device_state
        if state and state.brightness > self._brightness_min:
            self._last_on_brightness = state.brightness

        if not await self.coordinator.async_control_device(
            self._device_id, BrightnessCommand(brightness=self._brightness_min)
        ):
            _LOGGER.warning("Brightness command failed for %s", self._device_id)
            return
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore the last non-off brightness to return to on turn_on."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get("brightness"):
            ha_brightness = last_state.attributes["brightness"]
            device_brightness = self._ha_to_device_brightness(ha_brightness)
            if device_brightness > self._brightness_min:
                self._last_on_brightness = device_brightness
