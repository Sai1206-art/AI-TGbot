# Telegram AI Video Demo

This is a very small demo bot. A user chooses Video or Image Edit, sends a photo, and receives the generated result from your Hugging Face Space.

It uses webhook delivery when `WEBHOOK_URL` is configured, SQLite wallets/jobs, and Telegram Stars payments. Every user gets one free Image Edit generation; Video requires paid tokens from the first request and costs three tokens by default.

## Important: protect your token

Your BotFather token is the bot's password. Do not put it in this file, send it in chat, or show it in screenshots. The token visible in the setup screenshot should be revoked and replaced in BotFather before using this project:

1. Open the BotFather chat.
2. Send `/revoke` and follow the prompts, or use `/token` to generate a replacement token.
3. Keep the replacement token private.

## Run it on your Mac

### 1. Install Python

Install Python 3.11 or newer from python.org if Python is not already installed.

### 2. Open Terminal in this folder

In Finder, open the `telegram-ai-video-demo` folder. Right-click the folder, choose **Services**, then choose **New Terminal at Folder** if that option is available.

If that option is not available, open Terminal and type `cd ` including the space, then drag this folder into the Terminal window and press Return.

### 3. Create a private project area

Copy and run these commands one at a time:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 4. Add your token safely

Replace `PASTE_YOUR_NEW_TOKEN_HERE` with the replacement token from BotFather, then run:

```bash
export TELEGRAM_BOT_TOKEN="PASTE_YOUR_NEW_TOKEN_HERE"
```

This keeps the token out of the Python file. You will need to run this command again whenever you open a new Terminal window.

Set the duplicated Space and its API settings:

```bash
export HF_SPACE_ID="your-username/your-private-space"
export HF_TOKEN="hf_your_read_token"
export HF_API_NAME="/generate_video"
export HF_PROMPT="Animate this image with natural, cinematic motion."
```

The bot now defaults to Video and lets each chat switch modes from the reply keyboard or with `/mode video` and `/mode image_edit`. `HF_GENERATION_MODE` can still set the initial default for chats that have not selected a mode.

To use the duplicated Qwen image-edit Space, keep the edit prompt in the photo caption:

```bash
export HF_GENERATION_MODE="image_edit"
export HF_IMAGE_EDIT_SPACE_ID="your-username/your-qwen-image-edit-space"
export HF_IMAGE_PROMPT="Create a polished, natural-looking variation of this image."
export WALLET_DB_PATH="bot_data.sqlite3"
export PAYMENT_SUPPORT_CONTACT="@your_support_username"
```

Optional resilience settings:

```bash
export HF_MAX_RETRIES="2"
export HF_RETRY_DELAY_SECONDS="2"
export RESERVATION_MAX_AGE_SECONDS="1800"
export RATE_LIMIT_REQUESTS="3"
export RATE_LIMIT_WINDOW_SECONDS="60"
export JOB_MAX_ATTEMPTS="3"
export JOB_POLL_SECONDS="2"

For Railway or another public host, configure webhook delivery so Telegram sends updates to exactly one active instance per bot token:

```bash
export WEBHOOK_URL="https://your-public-service-domain"
export WEBHOOK_SECRET="a-long-random-secret"
export PORT="8080"
```

When `WEBHOOK_URL` is set, the bot starts a webhook server. Without it, the bot falls back to polling for local development; never run more than one polling instance for the same token. Generation requests are reserved atomically, written to SQLite, processed by a bounded retry worker, and moved to `dead_letter` after the configured attempts. Dead-letter jobs refund their reservation exactly once.
```

The image-edit wrapper sends the downloaded photo inside the recorded `images` payload, plus the caption prompt, seed/randomization, true guidance scale, inference steps, dimensions, prompt-rewrite setting, and image count. It reads the image-edit Space API Documentation at startup and selects the one named endpoint that accepts both `images` and `prompt`. Set `HF_IMAGE_EDIT_API_NAME` only when the Space has more than one matching endpoint. Optional overrides include `HF_IMAGE_SEED`, `HF_IMAGE_RANDOMIZE_SEED`, `HF_IMAGE_TRUE_GUIDANCE_SCALE`, `HF_IMAGE_INFERENCE_STEPS`, `HF_IMAGE_HEIGHT`, `HF_IMAGE_WIDTH`, `HF_IMAGE_REWRITE_PROMPT`, and `HF_IMAGE_COUNT`.

The image-edit Space is intentionally separate from the video Space. If `HF_IMAGE_EDIT_SPACE_ID` is missing, the bot falls back to `HF_SPACE_ID`, but it refuses to generate unless that Space exposes an endpoint accepting both `images` and `prompt`. This prevents accidentally spending credits on the video Space.

The default call sends the image path and prompt as the first two API inputs. If the Space's **Use via API** panel shows additional inputs, set `HF_API_ARGS_JSON` to the full argument list and use `$IMAGE` where the downloaded image should go. For example:

```bash
export HF_API_ARGS_JSON='["$IMAGE", "Animate this image", 25, 6, 12345]'
```

### 5. Start the bot

```bash
python telegram_bot.py
```

You should see `Bot is running`. Leave this Terminal window open while testing.

### 6. Test in Telegram

1. Open your bot using the username BotFather gave you.
2. Press **Start** or send `/start`.
3. Choose **Video** or **Image Edit**.
4. For Image Edit, put your edit prompt in the photo caption; for Video, send the photo without a caption.
5. The bot should reply `Processing…` and send the selected result.

### Tokens and Telegram Stars

Each Telegram user receives one free Image Edit generation. Video never uses the free credit and costs three paid tokens by default; Image Edit costs one paid token after the free generation. Set `IMAGE_EDIT_COST` and `VIDEO_COST` to change the per-mode costs. After a purchase and after each successful generation, the bot shows the remaining paid balance and mode-specific affordable generations. Use `/balance` to see the free-credit status, paid balance, and recent history; `/history` shows up to ten recent requests; `/buy` chooses a 10-, 30-, or 75-token package paid with Telegram Stars (XTR). `/support` and `/paysupport` tell users where to request help, and `/language` records the current language selection.

The bot approves valid pre-checkout queries, credits tokens only after Telegram sends a successful payment update, and stores the Telegram charge ID in SQLite for duplicate-payment protection. The mode-specific free credit or exact paid token cost is reserved atomically before any generation, logged in `wallet_events`, and automatically refunded exactly once if the Hugging Face Space fails or times out. Successful and failed requests are stored in `generation_history`; stale reservations are recovered at startup after `RESERVATION_MAX_AGE_SECONDS`.

The SQLite database defaults to `bot_data.sqlite3` in the project folder. Set `WALLET_DB_PATH` to move it. Package amounts currently use a simple 1 Star = 1 token demo price; review your GPU cost and change the package pricing before launch.

Manual payment test plan:

1. New user sends `/start`, selects Image Edit, and sends one photo: it succeeds as the free generation.
2. New user selects Video with zero paid balance: the bot blocks it and shows the Buy Tokens menu.
3. The user buys the 10-token package with Telegram Stars, then sends a video photo: payment confirmation increases the balance and the generation deducts three tokens.

To stop the bot, click the Terminal window and press `Control+C`.

## Automated checks

Run the wallet and concurrency scenarios with:

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

The checklist in `TEST_CHECKLIST.md` covers free usage, payment idempotency, insufficient balance, concurrent requests, failure refunds, restart recovery, and startup configuration errors.

## Unattended deployment

Copy `.env.example` to `.env` and fill in the secrets without committing the file. For Docker, review `Dockerfile` and `docker-compose.example.yml`, then run `docker compose -f docker-compose.example.yml up -d --build`. The SQLite database is persisted in `./data`.

For a Linux VM, copy `telegram-ai-video.service.example` to `/etc/systemd/system/telegram-ai-video.service`, create `/etc/telegram-ai-video-bot.env` from `.env.example`, adjust `WorkingDirectory` and `ExecStart`, then run `systemctl enable --now telegram-ai-video`. Create a dedicated `telegrambot` user and keep the database on persistent storage.

The rotated Telegram token cannot be verified from this local checkout without the secret. Startup validates that `TELEGRAM_BOT_TOKEN` and `HF_SPACE_ID` are present; after setting them, run the automated checks and start once to confirm polling reaches `Bot is running`.

## Space API settings

The Space's API name and argument order must match the values shown in its **Use via API** panel. Keep `HF_TOKEN` and `TELEGRAM_BOT_TOKEN` in the shell environment or a secret manager; do not put either token in this file.
