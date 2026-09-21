"""Mode must resolve per event, most specific scope winning."""

from timeo.domain.entities import Property, Tenant
from timeo.domain.enums import Channel, OperatingMode
from timeo.gateway import resolve_mode


def test_falls_back_to_tenant_default(tenant, property_):
    tenant.default_mode = OperatingMode.SHADOW
    assert resolve_mode(tenant=tenant, property_=property_) is OperatingMode.SHADOW


def test_property_overrides_tenant(tenant, property_):
    tenant.default_mode = OperatingMode.SHADOW
    property_.mode_override = OperatingMode.AUTONOMOUS
    assert resolve_mode(tenant=tenant, property_=property_) is OperatingMode.AUTONOMOUS


def test_channel_overrides_property(tenant, property_):
    tenant.default_mode = OperatingMode.AUTONOMOUS
    property_.mode_override = OperatingMode.AUTONOMOUS
    property_.channel_mode_overrides[Channel.AIRBNB] = OperatingMode.SHADOW

    assert resolve_mode(tenant=tenant, property_=property_, channel=Channel.AIRBNB) is (
        OperatingMode.SHADOW
    )
    # A channel without an override still follows the property.
    assert resolve_mode(tenant=tenant, property_=property_, channel=Channel.SMS) is (
        OperatingMode.AUTONOMOUS
    )


def test_staged_rollout_is_expressible():
    """One tenant, two properties, different modes at the same moment."""
    tenant = Tenant(tenant_id="t1", default_mode=OperatingMode.SHADOW)
    piloted = Property(tenant_id="t1", mode_override=OperatingMode.AUTONOMOUS)
    rest = Property(tenant_id="t1")

    assert resolve_mode(tenant=tenant, property_=piloted) is OperatingMode.AUTONOMOUS
    assert resolve_mode(tenant=tenant, property_=rest) is OperatingMode.SHADOW


def test_unknown_scope_defaults_to_simulation():
    """The safe default is the mode that cannot reach the outside world."""
    assert resolve_mode(tenant=None) is OperatingMode.SIMULATION
