import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from delivery.models import Customer, Parcel, ShipmentRequest, ShipmentStatus as S
from delivery.repositories import PostgresCustomerRepository, PostgresParcelRepository, PostgresShipmentRepository
from delivery.service import ShippingService, ShipmentRejectedError


# ГЛОБАЛЬНУ ЗМІННУ shipment_id = 0 ВИДАЛЕНО (Виправлення Flaky Test 1)

class Policy:
    def is_allowed(self, customer, parcel, distance):
        return True


@pytest.fixture(scope="module")
def sut():
    with PostgresContainer("postgres:17.6-alpine") as postgres:
        with psycopg.connect(host=postgres.get_container_host_ip(), port=postgres.get_exposed_port(5432),
                             user=postgres.username, password=postgres.password,
                             dbname=postgres.dbname, autocommit=True) as db:
            migration = Path(__file__).resolve().parents[1] / "migrations/V1__create_tables.sql"
            db.execute(migration.read_text(encoding="utf-8"))
            customers = PostgresCustomerRepository(db)
            parcels = PostgresParcelRepository(db)
            shipments = PostgresShipmentRepository(db)
            service = ShippingService(customers, parcels, shipments, Policy())
            yield db, customers, parcels, shipments, service


def customer(sut):
    return sut[1].create(Customer(0, str(uuid4()), False))


def parcel(sut):
    return sut[2].create(Parcel(0, str(uuid4()), 1000, 0))


def request(person, training):
    return ShipmentRequest(str(uuid4()), person.id, training.id, 100, False, False, False)


@pytest.mark.smoke
def test_create_shipment(sut):
    # Тепер тест не записує нічого в глобальні змінні
    saved = sut[4].create_shipment(request(customer(sut), parcel(sut)))
    assert saved.status == S.REGISTERED
    assert sut[3].find_by_id(saved.id) == saved


def test_dispatch_shipment(sut):
    # ВИПРАВЛЕННЯ 1: Тест повністю незалежний (Order dependency)
    new_shipment = sut[4].create_shipment(request(customer(sut), parcel(sut)))
    saved = sut[4].dispatch(new_shipment.id)
    assert saved.status == S.DISPATCHED
    assert sut[3].find_by_id(new_shipment.id) == saved


def test_parcel_weight(sut):
    # Замість f"REGRESSION-PARCEL-{uuid4()}" просто пишемо str(uuid4())
    saved = sut[2].create(Parcel(0, str(uuid4()), 1000, 0))
    assert sut[2].find_by_id(saved.id).weight_grams == 1000

def test_parcel_value(sut):
    # Так само тут
    saved = sut[2].create(Parcel(0, str(uuid4()), 2000, 10000))
    assert sut[2].find_by_id(saved.id).declared_value == 10000

def test_shipment_after_policy_check(sut):
    class DelayedPolicy:
        def is_allowed(self, person, training, distance):
            time.sleep(float(os.getenv("REGRESSION_POLICY_DELAY_MS", "200")) / 1000)
            return True

    person = customer(sut)
    data = request(person, parcel(sut))
    service = ShippingService(sut[1], sut[2], sut[3], DelayedPolicy())
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(service.create_shipment, data)
        # ВИПРАВЛЕННЯ 3: Condition-based wait (замість time.sleep(0.05))
        operation.result(timeout=1.0)

        saved = sut[3].find_shipments(person.id, S.REGISTERED)
        assert len(saved) == 1
        assert saved[0].reference == data.reference
        assert saved[0].total == 7000


def test_shipment_selection(sut):
    person = customer(sut)
    prefix = str(uuid4())[:8]
    references = [prefix + "-A", prefix + "-B"]
    fixture_order = list(reversed(references)) if os.getenv("REGRESSION_REVERSE_FIXTURE") == "true" else references
    for reference in fixture_order:
        sut[4].create_shipment(ShipmentRequest(reference, person.id, parcel(sut).id, 100, False, False, False))
    # ВИПРАВЛЕННЯ 4: Гарантуємо порядок вибірки з БД (ORDER BY id ASC)
    actual = [row[0] for row in
              sut[0].execute("SELECT reference FROM shipments WHERE customer_id=%s ORDER BY id ASC", (person.id,))]
    assert actual == references


@pytest.mark.smoke
def test_delivery(sut):
    saved = sut[4].create_shipment(request(customer(sut), parcel(sut)))
    sut[4].dispatch(saved.id)
    delivered = sut[4].deliver(saved.id)
    assert delivered.status == S.DELIVERED
    assert sut[3].find_by_id(saved.id) == delivered


def test_premature_delivery(sut):
    saved = sut[4].create_shipment(request(customer(sut), parcel(sut)))
    with pytest.raises(ShipmentRejectedError):
        sut[4].deliver(saved.id)
    assert sut[3].find_by_id(saved.id) == saved


def test_policy_rejection(sut):
    class DeniedPolicy:
        def is_allowed(self, person, training, distance):
            return False

    person = customer(sut)
    service = ShippingService(sut[1], sut[2], sut[3], DeniedPolicy())
    with pytest.raises(ShipmentRejectedError):
        service.create_shipment(request(person, parcel(sut)))
    assert sut[3].find_shipments(person.id, S.REGISTERED) == []


def test_repeated_cancellation(sut):
    saved = sut[4].create_shipment(request(customer(sut), parcel(sut)))
    cancelled = sut[4].cancel(saved.id)
    with pytest.raises(ShipmentRejectedError):
        sut[4].cancel(saved.id)
    assert sut[3].find_by_id(saved.id) == cancelled