import pytest
from unittest.mock import MagicMock

from delivery.database import connect
from delivery.models import Customer, Parcel, Shipment, ShipmentRequest, ShipmentStatus
from delivery.repositories import PostgresCustomerRepository, PostgresParcelRepository, PostgresShipmentRepository
from delivery.service import ShippingService, ShipmentRejectedError
from delivery.ports import ShippingPolicy


@pytest.fixture
def db_conn():
    """
    Фікстура для підключення до БД.
    Гарантує НЕЗАЛЕЖНІСТЬ тестів (п. 3.7): перед кожним тестом і після нього
    таблиці повністю очищаються, тому тести не впливають один на одного.
    """
    conn = connect()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE shipments, parcels, customers RESTART IDENTITY CASCADE;")

    yield conn

    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE shipments, parcels, customers RESTART IDENTITY CASCADE;")
    conn.close()


@pytest.fixture
def customer_repo(db_conn):
    return PostgresCustomerRepository(db_conn)


@pytest.fixture
def parcel_repo(db_conn):
    return PostgresParcelRepository(db_conn)


@pytest.fixture
def shipment_repo(db_conn):
    return PostgresShipmentRepository(db_conn)


@pytest.fixture
def mock_policy():
    """
    Test Double (Mock) для зовнішньої HTTP-залежності.
    Вона НЕ входить в Integration Boundary (межу інтеграції бази даних).
    """
    return MagicMock(spec=ShippingPolicy)


@pytest.fixture
def shipping_service(customer_repo, parcel_repo, shipment_repo, mock_policy):
    """Створюємо реальний сервіс із реальними репозиторіями та фейковою політикою."""
    return ShippingService(customer_repo, parcel_repo, shipment_repo, mock_policy)



def test_create_and_find_customer_repositoryState(customer_repo):
    """Перевірка того, що репозиторій дійсно записує клієнта в PostgreSQL і може його прочитати."""
    # Arrange
    new_customer = Customer(id=0, code="CUST-001", regular=True)

    # Act
    created_customer = customer_repo.create(new_customer)
    found_customer = customer_repo.find_by_id(created_customer.id)

    # Assert (Перевірка State в базі даних)
    assert found_customer is not None
    assert found_customer.code == "CUST-001"
    assert found_customer.regular is True


def test_create_and_find_parcel_repositoryState(parcel_repo):
    """Перевірка збереження посилки в БД."""
    new_parcel = Parcel(id=0, code="PAR-001", weight_grams=5000, declared_value=15000)
    created_parcel = parcel_repo.create(new_parcel)

    found_parcel = parcel_repo.find_by_id(created_parcel.id)
    assert found_parcel is not None
    assert found_parcel.weight_grams == 5000



def test_create_shipment_success_serviceState(
        shipping_service, customer_repo, parcel_repo, shipment_repo, mock_policy
):
    """Позитивний сценарій: успішне оформлення відправлення зі збереженням у БД."""
    # Arrange: Готуємо реальні дані в базі
    customer = customer_repo.create(Customer(id=0, code="CUST-100", regular=True))
    parcel = parcel_repo.create(Parcel(id=0, code="PAR-100", weight_grams=2000, declared_value=5000))

    mock_policy.is_allowed.return_value = True

    request = ShipmentRequest(
        reference="REF-123", customer_id=customer.id, parcel_id=parcel.id,
        distance_km=100, urgent=False, pickup=True, coupon_valid=False
    )

    # Act: Викликаємо бізнес-операцію
    result = shipping_service.create_shipment(request)

    # Assert 1: Перевірка відповіді сервісу
    assert result.status == ShipmentStatus.REGISTERED

    # Assert 2: ПЕРЕВІРКА ФАКТИЧНОГО СТАНУ СИСТЕМИ (БД)
    db_shipment = shipment_repo.find_by_id(result.id)
    assert db_shipment is not None
    assert db_shipment.reference == "REF-123"
    assert db_shipment.status == ShipmentStatus.REGISTERED
    assert db_shipment.total > 0


def test_external_policy_rejected_serviceState(
        shipping_service, customer_repo, parcel_repo, shipment_repo, mock_policy
):
    """Негативний сценарій: зовнішня політика відмовила. Запис НЕ повинен з'явитися в БД."""
    customer = customer_repo.create(Customer(id=0, code="CUST-101", regular=True))
    parcel = parcel_repo.create(Parcel(id=0, code="PAR-101", weight_grams=2000, declared_value=5000))

    mock_policy.is_allowed.return_value = False
    request = ShipmentRequest(
        reference="REF-999", customer_id=customer.id, parcel_id=parcel.id, distance_km=100
    )

    # Act & Assert 1: Перевіряємо помилку бізнес-логіки
    with pytest.raises(ShipmentRejectedError, match="Доставку не дозволено"):
        shipping_service.create_shipment(request)

    # Assert 2: ПЕРЕВІРКА СТАНУ СИСТЕМИ (БД не повинна була змінитися)
    shipments_in_db = shipment_repo.find_shipments(customer.id, ShipmentStatus.REGISTERED)
    assert len(shipments_in_db) == 0


def test_urgent_delivery_allowed_business_rule_serviceState(
        shipping_service, customer_repo, parcel_repo, shipment_repo, mock_policy
):
    """Персональне правило (Позитивний): рівно 10000 г і рівно 500 км - термінова дозволена."""
    customer = customer_repo.create(Customer(id=0, code="CUST-URG-1", regular=False))
    parcel = parcel_repo.create(Parcel(id=0, code="PAR-URG-1", weight_grams=10000, declared_value=1000))
    mock_policy.is_allowed.return_value = True

    request = ShipmentRequest(
        reference="REF-URGENT-OK", customer_id=customer.id, parcel_id=parcel.id,
        distance_km=500, urgent=True
    )

    # Act
    result = shipping_service.create_shipment(request)

    # Assert: Перевірка бази даних
    db_shipment = shipment_repo.find_by_id(result.id)
    assert db_shipment.urgent is True
    assert db_shipment.status == ShipmentStatus.REGISTERED


def test_urgent_delivery_rejected_weight_business_rule_serviceState(
        shipping_service, customer_repo, parcel_repo, shipment_repo, mock_policy
):
    """Персональне правило (Негативний): вага більша за 10000 г (10001 г) - відмова до HTTP-запиту."""
    customer = customer_repo.create(Customer(id=0, code="CUST-URG-2", regular=False))
    parcel = parcel_repo.create(Parcel(id=0, code="PAR-URG-2", weight_grams=10001, declared_value=1000))

    request = ShipmentRequest(
        reference="REF-URGENT-FAIL", customer_id=customer.id, parcel_id=parcel.id,
        distance_km=500, urgent=True
    )

    # Act & Assert 1
    with pytest.raises(ShipmentRejectedError, match="Умови термінової доставки не виконані"):
        shipping_service.create_shipment(request)

    # Assert 2: HTTP-політика навіть не викликалась (бо відмова сталася раніше)
    mock_policy.is_allowed.assert_not_called()

    # Assert 3: База даних чиста від цього відправлення
    shipments_in_db = shipment_repo.find_shipments(customer.id, ShipmentStatus.REGISTERED)
    assert len(shipments_in_db) == 0


def test_urgent_delivery_rejected_distance_business_rule_serviceState(
        shipping_service, customer_repo, parcel_repo, shipment_repo, mock_policy
):
    """Персональне правило (Негативний): відстань більша за 500 км - відмова."""
    customer = customer_repo.create(Customer(id=0, code="CUST-URG-3", regular=False))
    parcel = parcel_repo.create(Parcel(id=0, code="PAR-URG-3", weight_grams=5000, declared_value=1000))

    request = ShipmentRequest(
        reference="REF-URGENT-FAIL2", customer_id=customer.id, parcel_id=parcel.id,
        distance_km=501, urgent=True
    )

    with pytest.raises(ShipmentRejectedError, match="Умови термінової доставки не виконані"):
        shipping_service.create_shipment(request)

    shipments_in_db = shipment_repo.find_shipments(customer.id, ShipmentStatus.REGISTERED)
    assert len(shipments_in_db) == 0


def test_dispatch_shipment_success_serviceState(
        shipping_service, customer_repo, parcel_repo, shipment_repo, mock_policy
):
    """Перевірка того, що зміна статусу (dispatch) фізично оновлює рядок у БД."""
    # Arrange: Створюємо відправлення
    customer = customer_repo.create(Customer(id=0, code="CUST-DISP", regular=True))
    parcel = parcel_repo.create(Parcel(id=0, code="PAR-DISP", weight_grams=1000, declared_value=1000))
    mock_policy.is_allowed.return_value = True

    request = ShipmentRequest(reference="REF-DISP", customer_id=customer.id, parcel_id=parcel.id, distance_km=10)
    created_shipment = shipping_service.create_shipment(request)

    assert shipment_repo.find_by_id(created_shipment.id).status == ShipmentStatus.REGISTERED

    # Act: Відправляємо
    shipping_service.dispatch(created_shipment.id)

    # Assert: Звертаємось у БД і перевіряємо, що статус оновився на DISPATCHED
    updated_shipment = shipment_repo.find_by_id(created_shipment.id)
    assert updated_shipment.status == ShipmentStatus.DISPATCHED


def test_invalid_status_transition_serviceState(
        shipping_service, customer_repo, parcel_repo, shipment_repo, mock_policy
):
    """Негативний сценарій: спроба відправити вже відправлену посилку."""
    customer = customer_repo.create(Customer(id=0, code="CUST-TRANS", regular=True))
    parcel = parcel_repo.create(Parcel(id=0, code="PAR-TRANS", weight_grams=1000, declared_value=1000))
    mock_policy.is_allowed.return_value = True

    request = ShipmentRequest(reference="REF-TRANS", customer_id=customer.id, parcel_id=parcel.id, distance_km=10)
    created_shipment = shipping_service.create_shipment(request)

    shipping_service.dispatch(created_shipment.id)

    # Act & Assert: Намагаємось зробити Dispatch ще раз (заборонений перехід)
    with pytest.raises(ShipmentRejectedError, match="Перехід статусу заборонений"):
        shipping_service.dispatch(created_shipment.id)

    shipment_in_db = shipment_repo.find_by_id(created_shipment.id)
    assert shipment_in_db.status == ShipmentStatus.DISPATCHED