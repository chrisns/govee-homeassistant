"""Independent "main downlight" light entity for Ceiling Light Pro devices.

Issue #131/#164. On these fixtures (MAIN_LIGHT_TOGGLE_SKUS) the two physical
zones — the central downlight panel and the RGBIC ring around it — sit on two
different control channels:

* **Ring** — ``segmentedColorRgb``. An overlay that wins for its 12 segments.
  "Off" is simply black. This already worked and is untouched here.
* **Main panel** — the whole-device colour/CCT/brightness channel. Every write
  to it drives the ENTIRE fixture, wiping the ring's segment overlay.

The trick that makes the main panel independently switchable is that its "off"
is **not** ``powerSwitch=0``. ``powerSwitch`` is a whole-fixture switch: it
kills the ring too, and (confirmed live) leaves the firmware in a state where
any later light command silently wakes the main panel back up — the coupling
that made this look unsolvable for so long. Setting the whole-device colour to
``RGB(0, 0, 0)`` instead darkens the main panel while leaving ``powerSwitch``
on, and from that state the ring can be lit via its own segment channel with
the main panel staying genuinely dark. Verified live on an H1270 in both
directions: main off + ring lit, and main lit + ring off.

Because every main-channel write clobbers the ring, each one is followed by
``coordinator.async_reassert_segments`` to put the ring back exactly where the
user left it. That re-assert is what keeps the two zones independent in
practice rather than merely in principle.

This replaces an earlier attempt that dimmed the main panel to its minimum
brightness. That was never true independence — brightness is a whole-device
property, so both entities read back the same value and "off" was really just
"very dim".
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

# Colour temperature used when turning the main panel on with nothing else to
# go on — no attributes in the service call and no restored previous value.
# Mid-range neutral white rather than either extreme.
_DEFAULT_ON_KELVIN = 4000

# The whole-device colour that reads as "main panel off".
_OFF_COLOR = RGBColor(r=0, g=0, b=0)


class GoveeMainLightEntity(GoveeEntity, LightEntity, RestoreEntity):
    """The Ceiling Light Pro's main downlight panel, controlled independently.

    ``is_on`` is derived from the device's REAL reported colour — black means
    the panel is dark — rather than tracked optimistically, so it stays honest
    across restarts and through changes made from the Govee app.
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

        # What to restore on turn_on when the service call says nothing. Only
        # ever holds a non-black colour / real colour temperature.
        self._last_on_kelvin: int | None = None
        self._last_on_rgb: tuple[int, int, int] | None = None

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
        if state and state.color_temp_kelvin and ColorMode.COLOR_TEMP in modes:
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
        """Return True when the main panel is lit.

        Black on the whole-device colour channel is how this entity turns the
        panel off, so black reads back as off. A device-level power-off also
        counts as off, since that kills everything.

        Colour temperature is checked FIRST because the two channels are
        mutually exclusive on this firmware: writing a colour clears
        ``colorTemperatureK`` (confirmed live — 2700K became ``None`` the
        moment black was written), and writing a colour temperature leaves the
        stale RGB value behind. So a live colour temperature always means the
        panel is lit, whatever ``color`` still says.
        """
        state = self.device_state
        if state is None:
            return False
        if not state.power_state:
            return False
        if state.color_temp_kelvin:
            return True
        if state.color is not None and state.color.as_tuple == (0, 0, 0):
            return False
        return True

    @property
    def brightness(self) -> int | None:
        """Return brightness (0-255) from the coordinator's tracked state."""
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

    def _remember_current_on_state(self) -> None:
        """Cache the panel's present colour so turn_on can return to it."""
        state = self.device_state
        if state is None:
            return
        if state.color_temp_kelvin:
            self._last_on_kelvin = state.color_temp_kelvin
            self._last_on_rgb = None
        elif state.color is not None and state.color.as_tuple != (0, 0, 0):
            self._last_on_rgb = state.color.as_tuple
            self._last_on_kelvin = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Light the main panel, then put the ring back where it was.

        Whatever colour path is taken, it must end up non-black — otherwise
        the panel would stay dark and the entity would immediately read back
        as off again.
        """
        if ATTR_BRIGHTNESS in kwargs:
            device_brightness = self._ha_to_device_brightness(kwargs[ATTR_BRIGHTNESS])
            if not await self.coordinator.async_control_device(
                self._device_id, BrightnessCommand(brightness=device_brightness)
            ):
                _LOGGER.warning("Brightness command failed for %s", self._device_id)

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            if (r, g, b) == (0, 0, 0):
                # An explicit black request is a request to turn off, not a
                # colour change — otherwise the panel would go dark while the
                # entity still claimed to be on.
                await self.async_turn_off()
                return
            if await self.coordinator.async_control_device(
                self._device_id, ColorCommand(color=RGBColor(r=r, g=g, b=b))
            ):
                self._last_on_rgb = (r, g, b)
                self._last_on_kelvin = None
            else:
                _LOGGER.warning("Color command failed for %s", self._device_id)

        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            if await self.coordinator.async_control_device(
                self._device_id, ColorTempCommand(kelvin=kelvin)
            ):
                self._last_on_kelvin = kelvin
                self._last_on_rgb = None
            else:
                _LOGGER.warning("Color temp command failed for %s", self._device_id)

        elif not self.is_on:
            # No colour specified and the panel is currently black — restore
            # whatever it was last lit with, falling back to neutral white.
            if self._last_on_rgb is not None:
                r, g, b = self._last_on_rgb
                await self.coordinator.async_control_device(
                    self._device_id, ColorCommand(color=RGBColor(r=r, g=g, b=b))
                )
            else:
                kelvin = self._last_on_kelvin or _DEFAULT_ON_KELVIN
                await self.coordinator.async_control_device(
                    self._device_id, ColorTempCommand(kelvin=kelvin)
                )

        await self.coordinator.async_reassert_segments(self._device_id)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Darken the main panel via the colour channel, never powerSwitch.

        See the module docstring for why ``powerSwitch=0`` is unusable here.
        """
        self._remember_current_on_state()

        if not await self.coordinator.async_control_device(
            self._device_id, ColorCommand(color=_OFF_COLOR)
        ):
            _LOGGER.warning("Main light off (colour) failed for %s", self._device_id)
            return

        # Black wipes the ring as well, so restore it — this is what leaves the
        # ring lit while the main panel stays dark.
        await self.coordinator.async_reassert_segments(self._device_id)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore the colour to return to when switched back on."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state:
            return
        kelvin = last_state.attributes.get("color_temp_kelvin")
        rgb = last_state.attributes.get("rgb_color")
        if kelvin:
            self._last_on_kelvin = int(kelvin)
        elif rgb and tuple(rgb) != (0, 0, 0):
            self._last_on_rgb = tuple(rgb)  # type: ignore[assignment]
