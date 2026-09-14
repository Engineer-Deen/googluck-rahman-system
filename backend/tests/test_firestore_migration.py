import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "firestore_migration.py"

spec = importlib.util.spec_from_file_location("firestore_migration", MODULE_PATH)
firestore_migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(firestore_migration)


def test_normalize_and_conflict_detection_are_stable():
    original = {"id": 1, "name": "Shop A", "updated_at": "2026-01-01T00:00:00+00:00"}
    same = firestore_migration._normalize_payload(original)
    diff = firestore_migration._normalize_payload({"id": 1, "name": "Shop B", "updated_at": "2026-01-01T00:00:00+00:00"})

    assert firestore_migration._record_decision(same, same) == "skip"
    assert firestore_migration._record_decision(same, diff) == "conflict"
    assert firestore_migration._record_decision(None, same) == "create"
