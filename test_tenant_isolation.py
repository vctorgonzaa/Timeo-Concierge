"""Cross-tenant access is a containment failure, not an empty result."""

import pytest

from timeo.domain.entities import Reservation, TenantContext
from timeo.domain.errors import TenantIsolationError
from timeo.repositories import InMemoryRepository


@pytest.fixture
def repo():
    return InMemoryRepository[Reservation]("reservation_id")


@pytest.fixture
def t1():
    return TenantContext(tenant_id="t1")


@pytest.fixture
def t2():
    return TenantContext(tenant_id="t2")


def test_owner_can_read_own_entity(repo, t1):
    res = Reservation(reservation_id="r1", tenant_id="t1")
    repo.add(t1, res)
    assert repo.get(t1, "r1") is res


def test_foreign_read_raises_rather_than_returning_none(repo, t1, t2):
    """Returning None here would disguise a breach as a cache miss."""
    repo.add(t1, Reservation(reservation_id="r1", tenant_id="t1"))

    with pytest.raises(TenantIsolationError):
        repo.get(t2, "r1")


def test_foreign_write_raises(repo, t2):
    with pytest.raises(TenantIsolationError):
        repo.add(t2, Reservation(reservation_id="r1", tenant_id="t1"))


def test_missing_entity_is_still_a_plain_miss(repo, t1):
    assert repo.get(t1, "nope") is None


def test_list_is_scoped(repo, t1, t2):
    repo.add(t1, Reservation(reservation_id="r1", tenant_id="t1"))
    repo.add(t1, Reservation(reservation_id="r2", tenant_id="t1"))
    repo.add(t2, Reservation(reservation_id="r3", tenant_id="t2"))

    assert {r.reservation_id for r in repo.list(t1)} == {"r1", "r2"}
    assert {r.reservation_id for r in repo.list(t2)} == {"r3"}
