"""pytest configuration for the GLS test suite."""
import pytest

from pytest_homeassistant_custom_component.plugins import hass  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make ``custom_components.gls`` loadable from config-flow / setup tests."""
    yield
