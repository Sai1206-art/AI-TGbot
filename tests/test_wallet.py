import tempfile
import threading
import unittest
from pathlib import Path

from wallet import WalletStore


class WalletStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WalletStore(Path(self.temp_dir.name) / "wallet.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_free_generation_is_single_use_and_refundable_once(self) -> None:
        reservation = self.store.reserve_generation(1, mode="video")
        self.assertIsNotNone(reservation)
        self.assertIsNone(self.store.reserve_generation(1, mode="video"))
        self.store.refund_generation(1, reservation, mode="video", error="test")
        self.store.refund_generation(1, reservation, mode="video", error="duplicate")
        balance = self.store.get_balance(1)
        self.assertTrue(balance["free_available"])
        self.assertEqual(balance["balance"], 0)

    def test_payment_is_idempotent(self) -> None:
        args = dict(
            user_id=2,
            telegram_payment_charge_id="charge-1",
            provider_payment_charge_id="provider-1",
            payload="tokens:10:stars:10",
            credits=10,
            stars=10,
        )
        inserted, first = self.store.add_payment(**args)
        duplicate, second = self.store.add_payment(**args)
        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(first["balance"], 10)
        self.assertEqual(second["balance"], 10)

    def test_concurrent_reservations_cannot_double_spend(self) -> None:
        free = self.store.reserve_generation(3, mode="video")
        self.store.complete_generation(3, free, mode="video")
        self.store.add_payment(
            user_id=3,
            telegram_payment_charge_id="charge-2",
            provider_payment_charge_id=None,
            payload="tokens:1:stars:1",
            credits=1,
            stars=1,
        )
        results = []

        def reserve() -> None:
            results.append(self.store.reserve_generation(3, mode="video"))

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_success_and_failure_are_recorded(self) -> None:
        success = self.store.reserve_generation(4, mode="image_edit", prompt="test")
        self.store.complete_generation(4, success, mode="image_edit", prompt="test")
        self.store.add_payment(
            user_id=4,
            telegram_payment_charge_id="charge-3",
            provider_payment_charge_id=None,
            payload="tokens:1:stars:1",
            credits=1,
            stars=1,
        )
        failure = self.store.reserve_generation(4, mode="video")
        self.store.refund_generation(4, failure, mode="video", error="upstream")
        history = self.store.get_generation_history(4, limit=10)
        self.assertEqual([item["status"] for item in history], ["failed", "succeeded"])


if __name__ == "__main__":
    unittest.main()
