from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.whatsapp import WhatsAppNotifier, mask_whatsapp_phone, normalize_alert_types, normalize_whatsapp_phone


class WhatsAppAlertTests(unittest.TestCase):
    def _db(self) -> tuple[tempfile.TemporaryDirectory[str], Database, Settings]:
        tmp = tempfile.TemporaryDirectory()
        settings = Settings(database_path=Path(tmp.name) / "test.db")
        db = Database(settings.database_path)
        db.init()
        return tmp, db, settings

    def test_phone_normalization_and_masking(self) -> None:
        self.assertEqual(normalize_whatsapp_phone("98765 43210", default_country_code="91"), "+919876543210")
        self.assertEqual(normalize_whatsapp_phone("+1 (415) 555-0100"), "+14155550100")
        self.assertEqual(mask_whatsapp_phone("+919876543210"), "+91****3210")
        self.assertEqual(normalize_alert_types(["fresh_buy", "unknown"]), ["fresh_buy"])

    def test_user_can_subscribe_and_unsubscribe(self) -> None:
        tmp, db, settings = self._db()
        self.addCleanup(tmp.cleanup)
        user = db.create_user("sudarshan", "hash")

        subscribed = db.update_user_whatsapp_subscription(
            int(user["id"]),
            phone="9876543210",
            enabled=True,
            alert_types=["fresh_buy"],
            default_country_code=settings.whatsapp_default_country_code,
        )

        self.assertTrue(subscribed["whatsapp"]["subscribed"])
        self.assertEqual(subscribed["whatsapp"]["phone_masked"], "+91****3210")
        self.assertEqual(subscribed["whatsapp"]["alert_types"], ["fresh_buy"])
        self.assertEqual(len(db.subscribed_whatsapp_users("fresh_buy")), 1)

        unsubscribed = db.update_user_whatsapp_subscription(int(user["id"]), enabled=False)

        self.assertFalse(unsubscribed["whatsapp"]["subscribed"])
        self.assertEqual(db.subscribed_whatsapp_users("fresh_buy"), [])

    def test_recent_alert_cooldown_uses_sent_events_only(self) -> None:
        tmp, db, _settings = self._db()
        self.addCleanup(tmp.cleanup)
        user = db.create_user("sudarshan", "hash")
        since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

        db.record_whatsapp_alert(
            user_id=int(user["id"]),
            phone="+919876543210",
            alert_type="fresh_buy",
            symbol="RELIANCE",
            status="FAILED",
            reason="provider failed",
        )
        self.assertFalse(db.recent_whatsapp_alert(int(user["id"]), "fresh_buy", "RELIANCE", since))

        db.record_whatsapp_alert(
            user_id=int(user["id"]),
            phone="+919876543210",
            alert_type="fresh_buy",
            symbol="RELIANCE",
            status="SENT",
        )
        self.assertTrue(db.recent_whatsapp_alert(int(user["id"]), "fresh_buy", "RELIANCE", since))

    def test_notifier_posts_meta_cloud_api_text_payload(self) -> None:
        _tmp, _db, base = self._db()
        self.addCleanup(_tmp.cleanup)
        settings = replace(
            base,
            whatsapp_alerts_enabled=True,
            whatsapp_api_base_url="https://graph.facebook.com/v23.0",
            whatsapp_phone_number_id="12345",
            whatsapp_access_token="token",
        )
        notifier = WhatsAppNotifier(settings)

        class Response:
            status_code = 200
            text = "{}"

            @staticmethod
            def json() -> dict[str, object]:
                return {"messages": [{"id": "wamid.unit"}]}

        with patch("app.whatsapp.httpx.post", return_value=Response()) as post:
            result = notifier.send_text("+919876543210", "Hello")

        self.assertTrue(result.ok)
        self.assertEqual(result.provider_message_id, "wamid.unit")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://graph.facebook.com/v23.0/12345/messages")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token")
        self.assertEqual(kwargs["json"]["messaging_product"], "whatsapp")
        self.assertEqual(kwargs["json"]["to"], "919876543210")
        self.assertEqual(kwargs["json"]["text"]["body"], "Hello")


if __name__ == "__main__":
    unittest.main()
