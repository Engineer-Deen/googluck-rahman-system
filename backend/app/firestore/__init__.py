"""Optional Firestore central sync provider."""

from app.firestore.service import FirestoreSyncService, get_firestore_sync_service

__all__ = ["FirestoreSyncService", "get_firestore_sync_service"]
