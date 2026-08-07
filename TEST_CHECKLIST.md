# Test Checklist

Run the automated wallet scenarios with:

```bash
python -m unittest discover -s tests -v
```

Covered scenarios:

- First generation receives exactly one free reservation.
- A failed generation refunds the reservation exactly once.
- Successful payments credit tokens only once for a repeated Telegram charge ID.
- Concurrent requests cannot spend the same free credit or paid token twice.
- Successful and failed requests appear in generation history.

Manual Telegram checks:

1. Start with a fresh `WALLET_DB_PATH`; `/balance` shows one free generation available.
2. Send a photo; `/balance` shows the free generation used and `/history` shows success.
3. Send another photo with zero paid balance; the bot presents `/buy` packages.
4. Complete a Stars payment; replaying the same successful-payment update must not add credits again.
5. Force a Space timeout/failure; the next `/balance` shows the token/free credit restored and a failed history entry.
6. Restart the process during a request; stale reservations older than `RESERVATION_MAX_AGE_SECONDS` are recovered on startup.
7. Start without `TELEGRAM_BOT_TOKEN` or `HF_SPACE_ID`; startup exits with a clear configuration error.
