import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import issue_token
from app.routes.products import products_bp


class ProductService:
    def __init__(self):
        self.products = {
            1: {"id": 1, "sku": "GLR-MOB-000001", "name": "Phone", "category": "Mobile Phones", "unit_price": "100.00", "cost_price": "70.00", "is_active": True, "shop_ids": [1]}
        }
        self.movements = [{"product_id": 1, "shop_id": 1, "quantity_delta": 4}]
        self.audit = []

    def get_staff(self, staff_id):
        return {"id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1, "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("secret")}

    def list_products(self, include_inactive=False):
        values = list(self.products.values())
        return values if include_inactive else [value for value in values if value["is_active"]]

    def get_product(self, product_id):
        return self.products.get(int(product_id))

    def get_product_by_client_request_id(self, client_request_id):
        if not client_request_id:
            return None
        for product in self.products.values():
            if product.get("client_request_id") == client_request_id:
                return product
        return None

    def _allocate_product_id(self):
        return max(self.products, default=0) + 1

    def save_product(self, product_id, **fields):
        self.products[int(product_id)] = {**self.products.get(int(product_id), {"id": int(product_id)}), **fields, "id": int(product_id)}
        return self.products[int(product_id)]

    def stock_map(self, product_ids=None, shop_id=None):
        ids = set(product_ids or self.products)
        result = {}
        for movement in self.movements:
            if movement["product_id"] in ids and (shop_id is None or movement["shop_id"] == shop_id):
                result[movement["product_id"]] = result.get(movement["product_id"], 0) + movement["quantity_delta"]
        return result

    def write_audit(self, audit_id, **fields):
        self.audit.append({"id": audit_id, **fields})


class FirestoreProductTests(unittest.TestCase):
    def setUp(self):
        self.service = ProductService()
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(products_bp)
        with self.app.app_context():
            self.token = issue_token(self.service.get_staff(1))

    def test_product_crud_and_stock_use_firestore(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff):
            client = self.app.test_client()
            listed = client.get("/api/products", headers=headers)
            created = client.post("/api/products", headers=headers, json={"name": "Laptop", "category": "Computers & Laptops", "unit_price": 500, "cost_price": 350})
            updated = client.put("/api/products/1", headers=headers, json={"name": "Updated Phone", "reason": "Catalog correction"})
            deleted = client.delete("/api/products/1", headers=headers, json={"reason": "Discontinued"})
        self.assertEqual(listed.get_json()[0]["stock"], 4)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(updated.get_json()["name"], "Updated Phone")
        self.assertFalse(deleted.get_json()["is_active"])
        self.assertTrue(self.service.audit)


class FirestoreProductIdempotencyTests(unittest.TestCase):
    """Uses the real FirestoreSyncService (against a fake Firestore client)
    rather than the hand-rolled mock above, so this exercises the actual
    get_product_by_client_request_id query path, not a re-implementation of it.
    """

    def setUp(self):
        from tests.test_firestore_provider import FakeFirestoreClient
        from app.firestore.service import FirestoreSyncService

        self.client = FakeFirestoreClient()
        self.service = FirestoreSyncService(self.client)
        self.staff = {"id": 1, "name": "Owner", "email": "owner@test", "role": "owner", "shop_id": 1, "is_active": True, "updated_at": datetime(2020, 1, 1, tzinfo=timezone.utc), "password_hash": generate_password_hash("secret")}
        self.client.collections["staff"]["1"] = self.staff
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore", JWT_SECRET_KEY="test-secret")
        self.app.register_blueprint(products_bp)
        with self.app.app_context():
            self.token = issue_token(self.staff)

    def test_repeated_create_with_same_client_request_id_does_not_duplicate(self):
        """Simulates a double-click or a retry after a dropped connection:
        the exact same create-product request arrives twice with the same
        client_request_id. Only one product should ever be created."""
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"name": "Bluetooth Speaker", "category": "Speakers & Audio", "cost_price": 40, "client_request_id": "req-abc-123"}
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff):
            client = self.app.test_client()
            first = client.post("/api/products", headers=headers, json=payload)
            second = client.post("/api/products", headers=headers, json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()["id"], second.get_json()["id"])
        products = [v for v in self.client.collections["products"].values()]
        self.assertEqual(len(products), 1)

    def test_create_without_client_request_id_is_unaffected(self):
        """A client too old to send client_request_id (or a direct API call
        without one) keeps working exactly as before -- no idempotency check
        is attempted when there's nothing to key it on."""
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"name": "USB Cable", "category": "Cables & Chargers", "cost_price": 5}
        with patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.auth._central_staff", side_effect=self.service.get_staff):
            client = self.app.test_client()
            first = client.post("/api/products", headers=headers, json=payload)
            second = client.post("/api/products", headers=headers, json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.get_json()["id"], second.get_json()["id"])


if __name__ == "__main__":
    unittest.main()
