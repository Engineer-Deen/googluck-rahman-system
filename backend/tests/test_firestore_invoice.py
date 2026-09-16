import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app.firestore.service import FirestoreSyncService
from app.routes.sales import assign_invoice_number
from tests.test_firestore_provider import FakeFirestoreClient


class FirestoreInvoiceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestoreClient()
        self.service = FirestoreSyncService(self.client)
        self.app = Flask(__name__)
        self.app.config.update(GLR_MODE="central", CENTRAL_DATA_PROVIDER="firestore")

    def test_first_and_sequential_allocations_preserve_format(self):
        with self.app.app_context():
            first = self.service.allocate_invoice_number({"created_at": "2026-01-01T00:00:00+00:00"})
            second = self.service.allocate_invoice_number({"created_at": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(first, "INV-2026-1001")
        self.assertEqual(second, "INV-2026-1002")

    def test_central_assign_invoice_uses_firestore_without_sql_query(self):
        sale = SimpleNamespace(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), invoice_number=None)
        with self.app.app_context(), patch("app.firestore.get_firestore_sync_service", return_value=self.service), patch("app.routes.sales.Sale") as sale_model:
            result = assign_invoice_number(sale)
        self.assertEqual(result, "INV-2026-1001")
        sale_model.query.with_entities.assert_not_called()

    def test_local_sqlite_invoice_sequence_remains_available(self):
        with self.app.app_context():
            self.app.config.update(GLR_MODE="local", CENTRAL_DATA_PROVIDER="firestore", SQLALCHEMY_DATABASE_URI="sqlite://")
            from app.extensions import db
            from app.models import InvoiceSequence

            db.init_app(self.app)
            db.create_all()
            sale = SimpleNamespace(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), invoice_number=None)
            with patch("app.firestore.get_firestore_sync_service") as firestore_service:
                result = assign_invoice_number(sale)

            self.assertEqual(self.app.config["GLR_MODE"], "local")
            self.assertEqual(result, "INV-2026-1001")
            firestore_service.assert_not_called()
            self.assertEqual(InvoiceSequence.query.count(), 1)
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
