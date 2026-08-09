# Test Checklist

Run the automated wallet scenarios with:

```bash
python -m unittest discover -s tests -v
```

Covered scenarios:

- Image Edit receives exactly one free reservation; Video never receives a free reservation.
- Video reservations charge the configured per-mode cost atomically.
- A failed generation refunds the reservation exactly once.
- Successful payments credit tokens only once for a repeated Telegram charge ID.
- Concurrent requests cannot spend the same free credit or paid token twice.
- Successful and failed requests appear in generation history.
- Rate limits are enforced from SQLite, not process memory, so they survive restarts.
- Only one job per user runs at a time; jobs from the same user remain ordered.
- Jobs left running by a crashed worker are requeued after the lease timeout.

Manual Telegram checks:

1. Start with a fresh `WALLET_DB_PATH`; `/balance` shows one free Image Edit generation available.
2. Select Video and send a photo with zero paid balance; the bot blocks it and presents `/buy` packages.
3. Select Image Edit and send a photo; `/balance` shows the free generation used and `/history` shows success.
4. Complete a Stars payment; replaying the same successful-payment update must not add credits again.
5. Force a Video Space timeout/failure; the next `/balance` shows the full video cost restored and a failed history entry.
6. Restart the process during a request; stale reservations older than `RESERVATION_MAX_AGE_SECONDS` are recovered on startup.
7. Start without `TELEGRAM_BOT_TOKEN` or `HF_SPACE_ID`; startup exits with a clear configuration error.
