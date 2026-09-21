import pytest

from timeo.audit import AuditLog
from timeo.domain.entities import ActionRequest, Property, Tenant, TenantContext
from timeo.domain.enums import Channel, OperatingMode
from timeo.gateway import ActionGateway, AdapterRegistry, MockAdapter
from timeo.policy import PolicyEngine


@pytest.fixture
def tenant():
    return Tenant(tenant_id="t1", name="Casa Verde Group", default_mode=OperatingMode.SIMULATION)


@pytest.fixture
def property_(tenant):
    return Property(property_id="p1", tenant_id=tenant.tenant_id, name="Villa Azul")


@pytest.fixture
def context():
    return TenantContext(tenant_id="t1", property_id="p1", channel=Channel.WHATSAPP)


@pytest.fixture
def registry():
    reg = AdapterRegistry()
    for name in (
        "send_message",
        "issue_refund",
        "dispatch_maintenance",
        "file_safety_report",
        "delete_reservation",
    ):
        reg.register(MockAdapter(name))
    return reg


@pytest.fixture
def audit():
    return AuditLog()


@pytest.fixture
def gateway(registry, audit):
    return ActionGateway(registry=registry, policy=PolicyEngine(), audit=audit)


@pytest.fixture
def make_request(context):
    def _make(action: str, **params) -> ActionRequest:
        return ActionRequest(
            action=action, params=params, context=context, conversation_id="c1"
        )

    return _make
