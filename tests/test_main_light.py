"""Test the independent main-light entity (issue #131/#164).

Verifies GoveeMainLightEntity drives the main panel via BrightnessCommand
alone (dim to device minimum = "off") rather than powerSwitch or the parked
ptReal toggle, and that is_on is derived from real reported brightness
rather than tracked optimistically.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.govee.models import (
    BrightnessCommand,
    ColorCommand,
    ColorTempCommand,
    GoveeDeviceState,
    RGBColor,
)
from custom_components.govee.platforms.main_light import GoveeMainLightEntity


def _make_main_light_entity(
    *,
    device_brightness: int = 254,
    brightness_range: tuple[int, int] = (1, 254),
    state_exists: bool = True,
) -> GoveeMainLightEntity:
    """Create a GoveeMainLightEntity with a mocked coordinator.

    Args:
        device_brightness: Device-scale brightness returned by get_state().
        brightness_range: (min, max) device-scale brightness range.
        state_exists: Whether get_state() returns a state or None.
    """
    coordinator = MagicMock()
    coordinator.async_control_device = AsyncMock(return_value=True)

    if state_exists:
        state = GoveeDeviceState.create_empty("AA:BB:CC:DD:EE:FF:00:11")
        state.brightness = device_brightness
        coordinator.get_state = MagicMock(return_value=state)
    else:
        coordinator.get_state = MagicMock(return_value=None)

    device = MagicMock()
    device.device_id = "AA:BB:CC:DD:EE:FF:00:11"
    device.sku = "H1270"
    device.name = "Ceiling Light"
    device.supports_rgb = True
    device.supports_color_temp = True
    device.supports_brightness = True
    device.brightness_range = brightness_range
    device.color_temp_range = None

    with patch.object(GoveeMainLightEntity, "__init__", lambda self, *a, **kw: None):
        entity = GoveeMainLightEntity.__new__(GoveeMainLightEntity)

    entity.coordinator = coordinator
    entity._device = device
    entity._device_id = device.device_id
    entity._brightness_min, entity._brightness_max = brightness_range
    entity._last_on_brightness = entity._brightness_max
    entity.async_write_ha_state = MagicMock()

    return entity


class TestMainLightIsOn:
    """is_on is derived from real reported brightness, not tracked optimistically."""

    def test_is_on_true_above_minimum(self):
        entity = _make_main_light_entity(device_brightness=254, brightness_range=(1, 254))
        assert entity.is_on is True

    def test_is_on_false_at_minimum(self):
        entity = _make_main_light_entity(device_brightness=1, brightness_range=(1, 254))
        assert entity.is_on is False

    def test_is_on_false_when_no_state(self):
        entity = _make_main_light_entity(state_exists=False)
        assert entity.is_on is False


class TestMainLightTurnOff:
    """async_turn_off dims to the device minimum — never powerSwitch."""

    @pytest.mark.asyncio
    async def test_turn_off_sends_minimum_brightness(self):
        entity = _make_main_light_entity(device_brightness=254, brightness_range=(1, 254))

        await entity.async_turn_off()

        entity.coordinator.async_control_device.assert_called_once()
        args = entity.coordinator.async_control_device.call_args
        assert args[0][0] == "AA:BB:CC:DD:EE:FF:00:11"
        cmd = args[0][1]
        assert isinstance(cmd, BrightnessCommand)
        assert cmd.brightness == 1

    @pytest.mark.asyncio
    async def test_turn_off_remembers_current_brightness(self):
        entity = _make_main_light_entity(device_brightness=180, brightness_range=(1, 254))

        await entity.async_turn_off()

        assert entity._last_on_brightness == 180

    @pytest.mark.asyncio
    async def test_turn_off_does_not_overwrite_memory_when_already_off(self):
        entity = _make_main_light_entity(device_brightness=1, brightness_range=(1, 254))
        entity._last_on_brightness = 200

        await entity.async_turn_off()

        assert entity._last_on_brightness == 200

    @pytest.mark.asyncio
    async def test_turn_off_writes_state_on_success(self):
        entity = _make_main_light_entity()

        await entity.async_turn_off()

        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_skips_write_state_on_command_failure(self):
        entity = _make_main_light_entity()
        entity.coordinator.async_control_device = AsyncMock(return_value=False)

        await entity.async_turn_off()

        entity.async_write_ha_state.assert_not_called()


class TestMainLightTurnOn:
    """async_turn_on restores the last non-off brightness, or an explicit request."""

    @pytest.mark.asyncio
    async def test_turn_on_restores_last_on_brightness(self):
        entity = _make_main_light_entity(device_brightness=1, brightness_range=(1, 254))
        entity._last_on_brightness = 180

        await entity.async_turn_on()

        args = entity.coordinator.async_control_device.call_args
        cmd = args[0][1]
        assert isinstance(cmd, BrightnessCommand)
        assert cmd.brightness == 180

    @pytest.mark.asyncio
    async def test_turn_on_with_explicit_brightness(self):
        entity = _make_main_light_entity(device_brightness=1, brightness_range=(1, 254))

        # HA brightness 128/255 scaled into device range (1, 254).
        await entity.async_turn_on(brightness=128)

        args = entity.coordinator.async_control_device.call_args
        cmd = args[0][1]
        assert isinstance(cmd, BrightnessCommand)
        assert 1 < cmd.brightness < 254

    @pytest.mark.asyncio
    async def test_turn_on_falls_back_to_max_when_memory_is_at_minimum(self):
        entity = _make_main_light_entity(device_brightness=1, brightness_range=(1, 254))
        entity._last_on_brightness = 1  # never actually recorded a real "on" value

        await entity.async_turn_on()

        args = entity.coordinator.async_control_device.call_args
        cmd = args[0][1]
        assert cmd.brightness == 254

    @pytest.mark.asyncio
    async def test_turn_on_sends_color(self):
        entity = _make_main_light_entity()

        await entity.async_turn_on(rgb_color=(255, 0, 0))

        calls = entity.coordinator.async_control_device.call_args_list
        color_calls = [c for c in calls if isinstance(c[0][1], ColorCommand)]
        assert len(color_calls) == 1
        assert color_calls[0][0][1].color == RGBColor(r=255, g=0, b=0)

    @pytest.mark.asyncio
    async def test_turn_on_sends_color_temp(self):
        entity = _make_main_light_entity()

        await entity.async_turn_on(color_temp_kelvin=4000)

        calls = entity.coordinator.async_control_device.call_args_list
        ct_calls = [c for c in calls if isinstance(c[0][1], ColorTempCommand)]
        assert len(ct_calls) == 1
        assert ct_calls[0][0][1].kelvin == 4000

    @pytest.mark.asyncio
    async def test_turn_on_updates_memory_on_success(self):
        entity = _make_main_light_entity(device_brightness=1, brightness_range=(1, 254))

        await entity.async_turn_on(brightness=255)

        assert entity._last_on_brightness == 254

    @pytest.mark.asyncio
    async def test_turn_on_skips_write_state_on_brightness_failure(self):
        entity = _make_main_light_entity()
        entity.coordinator.async_control_device = AsyncMock(return_value=False)

        await entity.async_turn_on()

        entity.async_write_ha_state.assert_not_called()
