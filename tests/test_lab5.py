import pytest
import psycopg
import requests
from testcontainers.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from delivery.models import Customer, Parcel, ShipmentRequest, ShipmentStatus
from delivery.repositories import PostgresCustomerRepository, PostgresParcelRepository, PostgresShipmentRepository
from delivery.service import ShippingService, ShipmentRejectedError
from delivery.http_shipping_policy import HttpShippingPolicy

@pytest.fixture(scope="session")
def postgres_container():
    """Піднімає Docker-контейнер з PostgreSQL."""
    with PostgresContainer("postgres:17") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def wiremock_container():
    """Піднімає Docker-контейнер з WireMock як базовий контейнер."""
    with DockerContainer("wiremock/wiremock:latest").with_exposed_ports(8080) as wiremock:
        yield wiremock


@pytest.fixture
def db_conn(postgres_container):
    """Створює з'єднання і накочує міграції."""

    connection_url = postgres_container.get_connection_url()
    connection_url = connection_url.replace("postgresql+psycopg2", "postgresql")
    conn = psycopg.connect(connection_url, autocommit=True)

    with conn.cursor() as cur:
        with open("migrations/V1__create_tables.sql", "r") as f:
            cur.execute(f.read())

    yield conn

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS shipments, parcels, customers CASCADE;")
    conn.close()


@pytest.fixture
def wiremock_url(wiremock_container):
    """Повертає базовий URL WireMock і очищає його перед тестом."""
    port = wiremock_container.get_exposed_port(8080)
    host = wiremock_container.get_container_host_ip()
    base_url = f"http://{host}:{port}"

    requests.post(f"{base_url}/__admin/mappings/reset")
    requests.post(f"{base_url}/__admin/requests/reset")

    return base_url


@pytest.fixture
def shipping_service(db_conn, wiremock_url):
    """Створює реальний сервіс із БД та реальним HTTP-клієнтом до WireMock."""
    customer_repo = PostgresCustomerRepository(db_conn)
    parcel_repo = PostgresParcelRepository(db_conn)
    shipment_repo = PostgresShipmentRepository(db_conn)

    policy = HttpShippingPolicy(base_url=wiremock_url, timeout=2)

    return ShippingService(customer_repo, parcel_repo, shipment_repo, policy), customer_repo, parcel_repo, shipment_repo


def test_successful_shipment_with_wiremock(shipping_service, wiremock_url):
    service, c_repo, p_repo, s_repo = shipping_service
    customer = c_repo.create(Customer(id=0, code="C1", regular=True))
    parcel = p_repo.create(Parcel(id=0, code="P1", weight_grams=1000, declared_value=5000))
    request = ShipmentRequest(reference="REF-1", customer_id=customer.id, parcel_id=parcel.id, distance_km=100)

    requests.post(
        f"{wiremock_url}/__admin/mappings",
        json={
            "request": {"method": "POST", "url": "/shipping-policy"},
            "response": {
                "status": 200,
                "jsonBody": {"allowed": True},
                "headers": {"Content-Type": "application/json"}
            }
        }
    )

    result = service.create_shipment(request)

    assert s_repo.find_by_id(result.id).status == ShipmentStatus.REGISTERED

    verify_resp = requests.post(
        f"{wiremock_url}/__admin/requests/count",
        json={"method": "POST", "url": "/shipping-policy"}
    )
    assert verify_resp.json()["count"] == 1


def test_external_rejection_with_wiremock(shipping_service, wiremock_url):
    service, c_repo, p_repo, s_repo = shipping_service
    customer = c_repo.create(Customer(id=0, code="C2", regular=True))
    parcel = p_repo.create(Parcel(id=0, code="P2", weight_grams=1000, declared_value=5000))
    request = ShipmentRequest(reference="REF-2", customer_id=customer.id, parcel_id=parcel.id, distance_km=100)

    requests.post(
        f"{wiremock_url}/__admin/mappings",
        json={
            "request": {"method": "POST", "url": "/shipping-policy"},
            "response": {
                "status": 200,
                "jsonBody": {"allowed": False},
                "headers": {"Content-Type": "application/json"}
            }
        }
    )

    with pytest.raises(ShipmentRejectedError, match="Доставку не дозволено"):
        service.create_shipment(request)

    assert len(s_repo.find_shipments(customer.id, ShipmentStatus.REGISTERED)) == 0


def test_external_service_unavailable(shipping_service, wiremock_url):
    service, c_repo, p_repo, s_repo = shipping_service
    customer = c_repo.create(Customer(id=0, code="C3", regular=True))
    parcel = p_repo.create(Parcel(id=0, code="P3", weight_grams=1000, declared_value=5000))
    request = ShipmentRequest(reference="REF-3", customer_id=customer.id, parcel_id=parcel.id, distance_km=100)

    requests.post(
        f"{wiremock_url}/__admin/mappings",
        json={
            "request": {"method": "POST", "url": "/shipping-policy"},
            "response": {"status": 503}
        }
    )

    with pytest.raises(RuntimeError):
        service.create_shipment(request)

    assert len(s_repo.find_shipments(customer.id, ShipmentStatus.REGISTERED)) == 0


def test_local_business_rule_rejection(shipping_service, wiremock_url):
    service, c_repo, p_repo, s_repo = shipping_service
    customer = c_repo.create(Customer(id=0, code="C4", regular=True))

    parcel = p_repo.create(Parcel(id=0, code="P4", weight_grams=10001, declared_value=5000))
    request = ShipmentRequest(reference="REF-4", customer_id=customer.id, parcel_id=parcel.id, distance_km=100,
                              urgent=True)

    with pytest.raises(ShipmentRejectedError, match="Умови термінової доставки не виконані"):
        service.create_shipment(request)

    assert len(s_repo.find_shipments(customer.id, ShipmentStatus.REGISTERED)) == 0

    verify_resp = requests.post(
        f"{wiremock_url}/__admin/requests/count",
        json={"method": "POST", "url": "/shipping-policy"}
    )
    assert verify_resp.json()["count"] == 0