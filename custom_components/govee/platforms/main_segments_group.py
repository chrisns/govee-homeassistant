"""Convenience "everything" light for Ceiling Light Pro devices.

Replaces the plain master light entity (GoveeLightEntity, backed by the
``powerSwitch`` capability) on MAIN_LIGHT_LAN_TOGGLE_SKUS. ``powerSwitch=0``
puts the fixture into a state where ANY later light command — including a
segment turning on — silently also wakes the main panel back up: a real,
confirmed firmware coupling (issue #131/#164 follow-up), not something this
integration can fix by sending a different cloud command. The fix is to
never use ``powerSwitch`` for on/off on these SKUs at all: this entity's
turn_on/turn_off instead drive the main panel via the independent LAN
toggle and the ring via the segment capability, exactly the two paths
GoveeMainLightEntity and GoveeGroupedSegmentEntity already use on their own.

This is the third parallel "everything" convenience alongside those two
independent entities — like GoveeGroupedSegmentEntity is to the individual
segments, this is additive, not a replacement for controlling main or the
segments on their own.
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
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.restore_state import RestoreEntity

from ..const import SUFFIX_MAIN_SEGMENTS_GROUP
from ..coordinator import GoveeCoordinator
from ..entity import GoveeEntity
from ..models import BrightnessCommand, ColorCommand, ColorTempCommand, GoveeDevice, RGBColor, SegmentColorCommand
from .grouped_segment import segments_optimistic_signal
from .main_light import main_light_optimistic_signal

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

_HA_BRIGHTNESS_MAX = 255  # see platforms/main_light.py for why this is a literal


class GoveeMainSegmentsGroupEntity(GoveeEntity, LightEntity, RestoreEntity):
    """"Everything" light: main panel (LAN toggle) + all ring segments.

    Tracks both constituents' on/off optimistically (neither Govee capability
    involved reports real state) via the same per-device dispatcher signals
    GoveeMainLightEntity and GoveeGroupedSegmentEntity already broadcast —
    kept in sync with whatever last touched either one directly, the same
    way SEGMENT_MODE_BOTH keeps the segment group and individual segments in
    sync. ``is_on`` is "either constituent is on", matching a standard HA
    light group's default aggregation.
    """

    _attr_translation_key = "govee_main_segments_group"

    def __init__(self, coordinator: GoveeCoordinator, device: GoveeDevice) -> None:
        """Initialize the group entity."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}{SUFFIX_MAIN_SEGMENTS_GROUP}"
        self._attr_name = None  # has_entity_name -> use the device name

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
        self._segment_indices = tuple(range(device.segment_count))

        # Optimistic, dispatcher-fed knowledge of both constituents.
        self._main_is_on = True
        self._segments_is_on = True
        self._segments_brightness = 255
        self._segments_rgb_color: tuple[int, int, int] = (255, 255, 255)

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
        """On if either the main panel or the ring is on."""
        return self._main_is_on or self._segments_is_on

    @property
    def brightness(self) -> int | None:
        """Main panel's brightness (real, from the coordinator's tracked state)."""
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
        """Turn everything on; brightness/colour attributes apply to the main panel."""
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

        if not self._main_is_on:
            if await self.coordinator.async_send_main_light_toggle(self._device_id):
                self._main_is_on = True
                async_dispatcher_send(
                    self.hass, main_light_optimistic_signal(self._device_id), True
                )

        if not self._segments_is_on:
            color = RGBColor(*self._segments_rgb_color)
            await self.coordinator.async_control_device(
                self._device_id,
                SegmentColorCommand(segment_indices=self._segment_indices, color=color),
            )
            self._segments_is_on = True
            async_dispatcher_send(
                self.hass,
                segments_optimistic_signal(self._device_id),
                True,
                self._segments_brightness,
                self._segments_rgb_color,
            )

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn everything off — never touches powerSwitch, see module docstring."""
        if self._main_is_on:
            if await self.coordinator.async_send_main_light_toggle(self._device_id):
                self._main_is_on = False
                async_dispatcher_send(
                    self.hass, main_light_optimistic_signal(self._device_id), False
                )

        if self._segments_is_on:
            await self.coordinator.async_control_device(
                self._device_id,
                SegmentColorCommand(
                    segment_indices=self._segment_indices, color=RGBColor(r=0, g=0, b=0)
                ),
            )
            self._segments_is_on = False
            async_dispatcher_send(
                self.hass,
                segments_optimistic_signal(self._device_id),
                False,
                self._segments_brightness,
                self._segments_rgb_color,
            )

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore previous state, then listen for either constituent changing."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state:
            self._main_is_on = last_state.state == "on"
            self._segments_is_on = last_state.state == "on"
            if last_state.attributes.get("brightness"):
                self._segments_brightness = last_state.attributes["brightness"]
            if last_state.attributes.get("rgb_color"):
                self._segments_rgb_color = tuple(last_state.attributes["rgb_color"])

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                main_light_optimistic_signal(self._device_id),
                self._handle_main_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                segments_optimistic_signal(self._device_id),
                self._handle_segments_update,
            )
        )

    @callback
    def _handle_main_update(self, is_on: bool) -> None:
        """Mirror a write made through GoveeMainLightEntity."""
        self._main_is_on = is_on
        self.async_write_ha_state()

    @callback
    def _handle_segments_update(
        self, is_on: bool, brightness: int, rgb_color: tuple[int, int, int]
    ) -> None:
        """Mirror a write made through the segment group or an individual segment."""
        self._segments_is_on = is_on
        self._segments_brightness = brightness
        self._segments_rgb_color = rgb_color
        self.async_write_ha_state()
