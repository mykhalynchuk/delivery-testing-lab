import os
import pytest
import psycopg
import requests
import time
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer


@pytest.fixture(scope="session")
def postgres_container():
    """Піднімає PostgreSQL у Docker."""
    with PostgresContainer("postgres:17") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def wiremock_container():
    """Піднімає WireMock у Docker."""
    with DockerContainer("wiremock/wiremock:latest").with_exposed_ports(8080) as wiremock:
        yield wiremock


@pytest.fixture
def env_setup(postgres_container, wiremock_container):
    """Налаштовує змінні середовища та БД перед імпортом API."""

    os.environ["DB_HOST"] = postgres_container.get_container_host_ip()
    os.environ["DB_PORT"] = str(postgres_container.get_exposed_port(5432))

    os.environ["DB_NAME"] = postgres_container.dbname
    os.environ["DB_USER"] = postgres_container.username
    os.environ["DB_PASSWORD"] = postgres_container.password

    wm_port = wiremock_container.get_exposed_port(8080)
    wm_host = wiremock_container.get_container_host_ip()
    wm_url = f"http://{wm_host}:{wm_port}"
    os.environ["SHIPPING_POLICY_URL"] = wm_url

    conn_url = postgres_container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
    conn = psycopg.connect(conn_url, autocommit=True)
    with conn.cursor() as cur:
        with open("migrations/V1__create_tables.sql", "r") as f:
            cur.execute(f.read())

    yield wm_url

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS shipments, parcels, customers CASCADE;")
    conn.close()


@pytest.fixture
def api_client(env_setup):
    """Створює HTTP-клієнт для тестування REST API."""
    wm_url = env_setup

    for _ in range(10):
        try:
            requests.get(f"{wm_url}/__admin/")
            break
        except requests.exceptions.ConnectionError:
            time.sleep(0.5)

    requests.post(f"{wm_url}/__admin/mappings/reset")
    requests.post(f"{wm_url}/__admin/requests/reset")

    requests.post(
        f"{wm_url}/__admin/mappings",
        json={
            "request": {"method": "POST", "url": "/shipping-policy"},
            "response": {"status": 200, "jsonBody": {"allowed": True}, "headers": {"Content-Type": "application/json"}}
        }
    )

    from delivery.api import app
    with TestClient(app) as client:
        yield client

def test_1_create_resource_post(api_client):
    """1. POST — створення ресурсу: статус 201, отримання id."""
    response = api_client.post("/customers", json={"code": "CUST-API-1", "regular": True})

    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["code"] == "CUST-API-1"
    assert data["regular"] is True


def test_2_get_resource_by_id(api_client):
    """2. GET за id: статус 200, дані відповідають створеному ресурсу."""
    post_resp = api_client.post("/customers", json={"code": "CUST-API-2", "regular": False})
    customer_id = post_resp.json()["id"]

    get_resp = api_client.get(f"/customers/{customer_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["code"] == "CUST-API-2"


def test_3_get_nonexistent_resource(api_client):
    """3. GET неіснуючого ресурсу: статус 404, перевірка полів code і message."""
    response = api_client.get("/customers/999999")

    assert response.status_code == 404
    data = response.json()
    assert "code" in data
    assert "message" in data


def test_4_workflow_and_action_endpoint(api_client):
    """4 & 8. Workflow: POST -> GET -> action -> GET. Динамічна передача ID."""
    # КРОК 1: POST (Створюємо клієнта і посилку)
    cust_id = api_client.post("/customers", json={"code": "W1", "regular": True}).json()["id"]
    parc_id = api_client.post("/parcels", json={"code": "P1", "weight_grams": 1000, "declared_value": 1000}).json()[
        "id"]

    # КРОК 2: POST (Створюємо відправлення)
    ship_resp = api_client.post("/shipments", json={
        "reference": "REF-WORKFLOW", "customer_id": cust_id, "parcel_id": parc_id,
        "distance_km": 100, "urgent": False, "pickup": False, "coupon_valid": False
    })
    assert ship_resp.status_code == 201
    ship_id = ship_resp.json()["id"]

    # КРОК 3: GET (Перевіряємо, що статус Registered)
    assert api_client.get(f"/shipments/{ship_id}").json()["status"] == "Registered"

    # КРОК 4: Action (Відправляємо посилку)
    action_resp = api_client.post(f"/shipments/{ship_id}/dispatch")
    assert action_resp.status_code == 200

    # КРОК 5: GET (Перевіряємо, що статус змінився на Dispatched)
    assert api_client.get(f"/shipments/{ship_id}").json()["status"] == "Dispatched"


def test_5_and_6_forbidden_action_and_unchanged_state(api_client):
    """5 & 6. Повторна дія (409) та незмінність стану після відхилення."""
    cust_id = api_client.post("/customers", json={"code": "W2", "regular": True}).json()["id"]
    parc_id = api_client.post("/parcels", json={"code": "P2", "weight_grams": 1000, "declared_value": 1000}).json()[
        "id"]
    ship_id = api_client.post("/shipments", json={
        "reference": "REF-FAIL", "customer_id": cust_id, "parcel_id": parc_id, "distance_km": 100
    }).json()["id"]

    api_client.post(f"/shipments/{ship_id}/dispatch")

    conflict_resp = api_client.post(f"/shipments/{ship_id}/cancel")
    assert conflict_resp.status_code == 409
    assert "code" in conflict_resp.json()

    get_resp = api_client.get(f"/shipments/{ship_id}")
    assert get_resp.json()["status"] == "Dispatched"



@pytest.mark.parametrize("weight, distance, expected_status", [
    (10000, 500, 201),  # Дозволено (межові значення)
    (10001, 500, 409),  # Відхилено (перевищено вагу)
    (5000, 501, 409),  # Відхилено (перевищено відстань)
])
def test_7_personal_business_rule_parameterized(api_client, weight, distance, expected_status):
    """7. Перевірка персонального правила з параметризацією."""
    cust_id = api_client.post("/customers", json={"code": "RULE-C", "regular": False}).json()["id"]
    parc_id = api_client.post("/parcels",
                              json={"code": f"RULE-P-{weight}", "weight_grams": weight, "declared_value": 100}).json()[
        "id"]

    ship_resp = api_client.post("/shipments", json={
        "reference": f"REF-RULE-{weight}-{distance}",
        "customer_id": cust_id,
        "parcel_id": parc_id,
        "distance_km": distance,
        "urgent": True
    })

    assert ship_resp.status_code == expected_status