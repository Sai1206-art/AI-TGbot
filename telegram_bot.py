from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from gradio_client import Client, handle_file

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from wallet import WalletStore
from bot_utils import format_balance_summary, mode_cost, mode_instruction


DEFAULT_PROMPT = "Create a video using the person in the reference image. Keep the exact same person, gender, facial features, hairstyle, body proportions, clothing, and overall identity unchanged throughout the entire video. Preserve their appearance and identity consistently in every frame, with only natural movement and camera motion."
DEFAULT_API_NAME = "/generate_video"
DEFAULT_IMAGE_EDIT_PROMPT = "make her nude"
VIDEO_MODE = "video"
IMAGE_EDIT_MODE = "image_edit"
VIDEO_BUTTON = "🎬 Video"
IMAGE_EDIT_BUTTON = "🖼️ Image Edit"
BALANCE_BUTTON = "💳 Balance"
SUPPORT_BUTTON = "🆘 Support"
LANGUAGE_BUTTON = "🌐 Language"
MODE_KEYBOARD = [[VIDEO_BUTTON, IMAGE_EDIT_BUTTON], [BALANCE_BUTTON, SUPPORT_BUTTON, LANGUAGE_BUTTON]]
PACKAGE_CREDITS = (10, 30, 75)
WALLET_DB_PATH = os.getenv("WALLET_DB_PATH", "bot_data.sqlite3")
wallet_store = WalletStore(WALLET_DB_PATH)
_worker_semaphore: asyncio.Semaphore | None = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret_name, secret in os.environ.items():
            normalized_name = secret_name.upper()
            if not secret or len(secret) < 6:
                continue
            if any(
                marker in normalized_name
                for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
            ):
                message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(_RedactingFilter())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome users who send /start."""
    if update.message:
        _ensure_user(update)
        await update.message.reply_text(
            "Welcome! Choose Video or Image Edit, then send me a photo.\n"
            "Image Edit uses the prompt in your photo caption and includes one free generation. Video uses its configured animation prompt and requires paid tokens from the first request.\n"
            "Use /buy to purchase tokens. Video costs more because it uses more compute.\n"
            "You can change the mode any time with /mode, check /balance, or use the Support and Language buttons.",
            reply_markup=ReplyKeyboardMarkup(
                MODE_KEYBOARD,
                resize_keyboard=True,
                one_time_keyboard=False,
            ),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain how users choose the generation mode."""
    if update.message:
        _ensure_user(update)
        await update.message.reply_text(
            "Choose 🎬 Video or 🖼️ Image Edit before sending a photo.\n"
            "You can also use /mode video or /mode image_edit.\n"
            "Image Edit includes one free generation and uses the prompt in your photo caption. Video requires paid tokens from the first request and costs more because it uses more compute."
        )


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    _ensure_user(update)
    support_contact = os.getenv("PAYMENT_SUPPORT_CONTACT", "the bot owner")
    await update.message.reply_text(
        f"Support: contact {support_contact}. Include your Telegram username, command, and any payment charge ID."
    )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    _ensure_user(update)
    requested = (" ".join(context.args) if context.args else "").strip().lower()
    if requested not in {"", "en", "english"}:
        await update.message.reply_text("English is currently the only available language.")
        return
    context.user_data["language"] = "en"
    await update.message.reply_text("Language set to English.")


def _normalise_mode(value: str) -> str | None:
    mode = value.strip().lower().replace("-", "_").replace(" ", "_")
    if mode in {VIDEO_MODE, "🎬_video"}:
        return VIDEO_MODE
    if mode in {IMAGE_EDIT_MODE, "image", "edit", "🖼️_image_edit"}:
        return IMAGE_EDIT_MODE
    return None


def _mode_for_chat(context: ContextTypes.DEFAULT_TYPE) -> str:
    selected_mode = context.chat_data.get("generation_mode")
    return selected_mode or _normalise_mode(
        os.getenv("HF_GENERATION_MODE", VIDEO_MODE)
    ) or VIDEO_MODE


def _user_id(update: Update) -> int | None:
    user = update.effective_user
    return user.id if user else None


def _ensure_user(update: Update) -> int | None:
    user_id = _user_id(update)
    if user_id is not None:
        wallet_store.ensure_user(user_id)
    return user_id


def _buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{credits} credits ({credits} ⭐)",
                    callback_data=f"buy:{credits}",
                )
            ]
            for credits in PACKAGE_CREDITS
        ]
    )


def _buy_message() -> str:
    return "Choose a token package. Telegram will show the Stars invoice before charging you."


def _package_from_payload(payload: str) -> tuple[int, int] | None:
    try:
        prefix, credits_text, stars_prefix, stars_text = payload.split(":")
        credits = int(credits_text)
        stars = int(stars_text)
    except (ValueError, AttributeError):
        return None
    if prefix != "tokens" or stars_prefix != "stars" or credits not in PACKAGE_CREDITS:
        return None
    return credits, stars


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = _ensure_user(update)
    if user_id is None:
        return
    balance = wallet_store.get_balance(user_id)
    free_status = "used" if balance["free_credit_used"] else "available"
    history = wallet_store.get_generation_history(user_id, limit=3)
    history_text = "\n".join(
        f"• {item['mode']} — {item['status']} ({item['created_at'][:19]} UTC)"
        for item in history
    ) or "No generations yet."
    await update.message.reply_text(
        f"{format_balance_summary(balance)}\n"
        f"Paid tokens: {balance['balance']}.\n"
        f"Free Image Edit generation: {free_status}.\n"
        f"Generation history ({balance['generation_count']} total):\n{history_text}"
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = _ensure_user(update)
    if user_id is None:
        return
    history = wallet_store.get_generation_history(user_id, limit=10)
    if not history:
        await update.message.reply_text("No generation history yet.")
        return
    lines = [
        f"{item['created_at'][:19]} UTC — {item['mode']} — {item['status']}"
        for item in history
    ]
    await update.message.reply_text("Recent generations:\n" + "\n".join(lines))


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    _ensure_user(update)
    await update.message.reply_text(_buy_message(), reply_markup=_buy_keyboard())


async def paysupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    _ensure_user(update)
    await support_command(update, context)


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()
    try:
        credits = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await query.message.reply_text("That package is no longer available. Use /buy again.")
        return
    if credits not in PACKAGE_CREDITS:
        await query.message.reply_text("That package is no longer available. Use /buy again.")
        return
    payload = f"tokens:{credits}:stars:{credits}"
    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"{credits} generation tokens",
        description=f"Add {credits} tokens to your media-generation balance.",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(f"{credits} tokens", credits)],
    )
    logger.info("Invoice sent: user_id=%s credits=%s stars=%s", query.from_user.id, credits, credits)


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    package = _package_from_payload(query.invoice_payload)
    if package is None or query.currency != "XTR" or query.total_amount != package[1]:
        await query.answer(ok=False, error_message="This invoice is invalid. Please use /buy again.")
        return
    await query.answer(ok=True)
    logger.info("Pre-checkout approved: user_id=%s payload=%s", query.from_user.id, query.invoice_payload)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.successful_payment:
        return
    payment = update.message.successful_payment
    package = _package_from_payload(payment.invoice_payload)
    if package is None or payment.currency != "XTR" or payment.total_amount != package[1]:
        logger.error("Rejected malformed successful payment: user_id=%s", _user_id(update))
        await update.message.reply_text("Payment received but could not be matched to a package. Contact /paysupport.")
        return
    user_id = _ensure_user(update)
    if user_id is None:
        return
    credits, stars = package
    inserted, balance = wallet_store.add_payment(
        user_id=user_id,
        telegram_payment_charge_id=payment.telegram_payment_charge_id,
        provider_payment_charge_id=payment.provider_payment_charge_id,
        payload=payment.invoice_payload,
        credits=credits,
        stars=stars,
    )
    logger.info(
        "Payment %s: user_id=%s credits=%s balance=%s charge_id=%s",
        "credited" if inserted else "already recorded",
        user_id,
        credits,
        balance["balance"],
        payment.telegram_payment_charge_id,
    )
    await update.message.reply_text(
        f"Payment confirmed. {format_balance_summary(balance)}"
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or display the current mode for this chat."""
    if not update.message:
        return
    _ensure_user(update)

    requested_mode = " ".join(context.args) if context.args else ""
    selected_mode = _normalise_mode(requested_mode) if requested_mode else None
    if not selected_mode:
        current_mode = _mode_for_chat(context)
        await update.message.reply_text(
            f"Current mode: {current_mode}. Choose a button or use "
            "/mode video or /mode image_edit.",
            reply_markup=ReplyKeyboardMarkup(
                MODE_KEYBOARD,
                resize_keyboard=True,
                one_time_keyboard=False,
            ),
        )
        return

    context.chat_data["generation_mode"] = selected_mode
    logger.info("Selected generation mode: %s", selected_mode)
    await update.message.reply_text(
        f"Mode set to {selected_mode}. Send a photo when ready. Cost: {mode_cost(selected_mode)} token(s).\n"
        f"{mode_instruction(selected_mode)}",
        reply_markup=ReplyKeyboardMarkup(
            MODE_KEYBOARD,
            resize_keyboard=True,
            one_time_keyboard=False,
        ),
    )


async def select_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the reply-keyboard mode buttons."""
    if not update.message:
        return
    _ensure_user(update)

    button_modes = {
        VIDEO_BUTTON: VIDEO_MODE,
        IMAGE_EDIT_BUTTON: IMAGE_EDIT_MODE,
    }
    selected_mode = button_modes.get(update.message.text or "")
    if update.message.text == BALANCE_BUTTON:
        await balance_command(update, context)
        return
    if update.message.text == SUPPORT_BUTTON:
        await support_command(update, context)
        return
    if update.message.text == LANGUAGE_BUTTON:
        await language_command(update, context)
        return
    if not selected_mode:
        await update.message.reply_text(
            "Choose 🎬 Video or 🖼️ Image Edit, then send a photo.\n"
            "Image Edit includes one free generation; Video requires paid tokens from the first request."
        )
        return

    context.chat_data["generation_mode"] = selected_mode
    logger.info("Selected generation mode: %s", selected_mode)
    await update.message.reply_text(
        f"Mode set to {selected_mode}. Send a photo when ready. Cost: {mode_cost(selected_mode)} token(s).\n"
        f"{mode_instruction(selected_mode)}",
        reply_markup=ReplyKeyboardMarkup(
            MODE_KEYBOARD,
            resize_keyboard=True,
            one_time_keyboard=False,
        ),
    )


def _replace_image_placeholder(value: Any, image_path: Path) -> Any:
    if isinstance(value, str) and value == "$IMAGE":
        return handle_file(image_path)



    if isinstance(value, list):
        return [_replace_image_placeholder(item, image_path) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_image_placeholder(item, image_path)
            for key, item in value.items()
        }
    return value


def _find_video_path(value: Any) -> Path | None:
    if isinstance(value, (str, os.PathLike)):
        candidate = Path(value)
        if candidate.exists() and candidate.is_file():
            return candidate
        return None
    if isinstance(value, dict):
        for item in value.values():
            result = _find_video_path(item)
            if result:
                return result
    if isinstance(value, (list, tuple)):
        for item in value:
            result = _find_video_path(item)
            if result:
                return result
    return None


def _find_image_path(value: Any) -> Path | None:
    if isinstance(value, (str, os.PathLike)):
        candidate = Path(value)
        if candidate.exists() and candidate.is_file():
            return candidate
        return None
    if isinstance(value, dict):
        for item in value.values():
            result = _find_image_path(item)
            if result:
                return result
    if isinstance(value, (list, tuple)):
        for item in value:
            result = _find_image_path(item)
            if result:
                return result
    return None


def _image_edit_space_id() -> str:
    space_id = os.getenv("HF_IMAGE_EDIT_SPACE_ID") or os.getenv("HF_SPACE_ID")
    if not space_id:
        raise RuntimeError(
            "Missing HF_IMAGE_EDIT_SPACE_ID. Set it to the duplicated image-edit Space ID."
        )
    return space_id


def _image_edit_api_name(client: Client) -> str | None:
    configured_name = os.getenv("HF_IMAGE_EDIT_API_NAME", "").strip()
    if configured_name.lower() in {"", "default", "none"}:
        configured_name = ""

    api_info = client.view_api(return_format="dict", print_info=False) or {}
    named_endpoints = api_info.get("named_endpoints", {})
    if configured_name:
        if configured_name not in named_endpoints:
            available = ", ".join(sorted(named_endpoints)) or "none"
            raise RuntimeError(
                f"HF_IMAGE_EDIT_API_NAME={configured_name!r} is not available on "
                f"{_image_edit_space_id()}. Available named endpoints: {available}."
            )
        return configured_name

    candidates = []
    for name, endpoint in named_endpoints.items():
        labels = {
            str(item.get("label", "")).lower().replace("_", " ")
            for item in endpoint.get("parameters", [])
        }
        has_images = any(
            label == "images" or label.endswith(" images") for label in labels
        )
        has_prompt = any("prompt" in label for label in labels)
        if has_images and has_prompt:
            candidates.append(name)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        preferred = "/infer" if "/infer" in candidates else None
        if preferred:
            return preferred
        raise RuntimeError(
            "Multiple image-edit endpoints were found. Set HF_IMAGE_EDIT_API_NAME to "
            f"one of: {', '.join(sorted(candidates))}."
        )

    raise RuntimeError(
        "The configured image-edit Space has no named endpoint accepting both "
        "images and prompt. Refusing to send a generation request; check the Space "
        "API Documentation and HF_IMAGE_EDIT_SPACE_ID."
    )


def _predict_with_retries(client: Client, *args: Any, **kwargs: Any) -> Any:
    retries = max(0, int(os.getenv("HF_MAX_RETRIES", "2")))
    delay = max(0.0, float(os.getenv("HF_RETRY_DELAY_SECONDS", "2")))
    for attempt in range(retries + 1):
        try:
            return client.predict(*args, **kwargs)
        except Exception:
            if attempt >= retries:
                raise
            wait_seconds = delay * (2**attempt)
            logger.warning("Space request failed; retrying attempt=%s wait_seconds=%s", attempt + 2, wait_seconds)
            time.sleep(wait_seconds)


def _client_with_retries(space_id: str, token: str | None) -> Client:
    retries = max(0, int(os.getenv("HF_MAX_RETRIES", "2")))
    delay = max(0.0, float(os.getenv("HF_RETRY_DELAY_SECONDS", "2")))
    for attempt in range(retries + 1):
        try:
            return Client(space_id, token=token or None)
        except Exception:
            if attempt >= retries:
                raise
            wait_seconds = delay * (2**attempt)
            logger.warning("Space client startup failed; retrying attempt=%s wait_seconds=%s", attempt + 2, wait_seconds)
            time.sleep(wait_seconds)


def _generate_video(image_path: Path) -> Path:
    try:
        from gradio_client import Client
    except ImportError as exc:
        raise RuntimeError(
            "gradio_client is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    space_id = os.getenv("HF_SPACE_ID")
    if not space_id:
        raise RuntimeError("Missing HF_SPACE_ID. Set it to your duplicated Space ID.")

    token = os.getenv("HF_TOKEN")
    client = _client_with_retries(space_id, token)
    api_name = os.getenv("HF_API_NAME", DEFAULT_API_NAME)
    args_json = os.getenv("HF_API_ARGS_JSON")

    if args_json:
        try:
            api_args = json.loads(args_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("HF_API_ARGS_JSON must contain valid JSON.") from exc
        if not isinstance(api_args, list):
            raise RuntimeError("HF_API_ARGS_JSON must be a JSON list of API arguments.")
        api_args = _replace_image_placeholder(api_args, image_path)
    else:
        api_args = [handle_file(image_path)]
    result = _predict_with_retries(client, *api_args, last_image=None, api_name=api_name)
    video_path = _find_video_path(result)
    if not video_path:
        raise RuntimeError(
            "The Space returned no video file. Check HF_API_NAME and HF_API_ARGS_JSON."
        )
    return video_path


def _generate_image_edit(image_path: Path, prompt: str) -> Path:
    space_id = _image_edit_space_id()

    token = os.getenv("HF_TOKEN")
    client = _client_with_retries(space_id, token)

    seed = int(os.getenv("HF_IMAGE_SEED", "0"))
    randomize_seed = os.getenv("HF_IMAGE_RANDOMIZE_SEED", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    true_guidance_scale = float(os.getenv("HF_IMAGE_TRUE_GUIDANCE_SCALE", "1"))
    inference_steps = int(os.getenv("HF_IMAGE_INFERENCE_STEPS", "4"))
    height = int(os.getenv("HF_IMAGE_HEIGHT", "744"))
    width = int(os.getenv("HF_IMAGE_WIDTH", "1024"))
    rewrite_prompt = os.getenv("HF_IMAGE_REWRITE_PROMPT", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    images_per_prompt = int(os.getenv("HF_IMAGE_COUNT", "1"))
    api_name = _image_edit_api_name(client)
    logger.info(
        "Image-edit request: Space=%s endpoint=%s",
        space_id,
        api_name or "default",
    )

    api_args = dict(
        images=[{"image": handle_file(image_path), "caption": None}],
        prompt=prompt,
        seed=seed,
        randomize_seed=randomize_seed,
        true_guidance_scale=true_guidance_scale,
        num_inference_steps=inference_steps,
        height=height,
        width=width,
        rewrite_prompt=rewrite_prompt,
        num_images_per_prompt=images_per_prompt,
    )
    if api_name:
        api_args["api_name"] = api_name
    result = _predict_with_retries(client, **api_args)
    image_output = _find_image_path(result)
    if not image_output:
        raise RuntimeError(
            "The Space returned no image file. Check the image-edit Space settings."
        )
    return image_output


def _get_image_prompt(update: Update) -> str:
    caption = (update.message.caption or "").strip() if update.message else ""
    prompt = caption or os.getenv("HF_IMAGE_PROMPT", DEFAULT_IMAGE_EDIT_PROMPT)
    return prompt[:2000]


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reserve a generation and enqueue it for a durable worker."""
    if not update.message:
        return

    user_id = _ensure_user(update)
    if user_id is None:
        return
    window_seconds = max(1, int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")))
    max_requests = max(1, int(os.getenv("RATE_LIMIT_REQUESTS", "3")))
    allowed = await asyncio.to_thread(
        wallet_store.allow_request,
        user_id,
        max_requests=max_requests,
        window_seconds=window_seconds,
    )
    if not allowed:
        await update.message.reply_text("You are sending requests too quickly. Please wait a moment and try again.")
        return

    mode = _mode_for_chat(context)
    prompt = _get_image_prompt(update) if mode == IMAGE_EDIT_MODE else os.getenv("HF_PROMPT", DEFAULT_PROMPT)
    cost = mode_cost(mode)
    request_key = f"photo:{update.message.chat_id}:{update.message.message_id}"
    reservation = wallet_store.reserve_generation(
        user_id,
        mode=mode,
        prompt=prompt,
        cost=cost,
        allow_free=mode == IMAGE_EDIT_MODE,
        request_key=request_key,
    )
    if reservation is None:
        balance = wallet_store.get_balance(user_id)
        await update.message.reply_text(
            f"You need {cost} paid token(s) for {mode}. Your current balance is {balance['balance']}. Buy 10, 30, or 75 tokens with /buy.",
            reply_markup=_buy_keyboard(),
        )
        return
    if reservation["status"] != "reserved":
        await update.message.reply_text("This photo request was already accepted and is being handled safely.")
        return
    job = wallet_store.enqueue_generation_job(
        request_key=request_key,
        user_id=user_id,
        chat_id=update.message.chat_id,
        message_id=update.message.message_id,
        reservation_id=reservation["reservation_id"],
        kind=reservation["kind"],
        cost=reservation["cost"],
        mode=mode,
        prompt=prompt,
        file_id=update.message.photo[-1].file_id,
    )
    if reservation["kind"] == "free" and reservation["status"] == "reserved":
        await update.message.reply_text("This Image Edit request uses your free generation.")
    await update.message.reply_text(
        f"Queued for processing (job {job['job_id'][:8]}). I’ll send the result when it’s ready."
    )


async def _process_generation_job(application: Application, job: dict[str, Any]) -> None:
    bot = application.bot
    started_at = time.monotonic()
    user_id = job["user_id"]
    mode = job["mode"]
    reservation = {
        "reservation_id": job["reservation_id"],
        "kind": job["kind"],
        "cost": job["cost"],
    }

    try:
        with TemporaryDirectory(prefix="telegram-ai-video-") as temp_dir:
            image_path = Path(temp_dir) / "input.jpg"
            video_copy = Path(temp_dir) / "generated.mp4"
            telegram_file = await bot.get_file(job["file_id"])
            await telegram_file.download_to_drive(custom_path=image_path)

            timeout_seconds = float(os.getenv("HF_TIMEOUT_SECONDS", "900"))
            if mode == IMAGE_EDIT_MODE:
                generated_path = await asyncio.wait_for(
                    asyncio.to_thread(_generate_image_edit, image_path, job["prompt"]),
                    timeout=timeout_seconds,
                )
                with generated_path.open("rb") as image_file:
                    await bot.send_photo(
                        chat_id=job["chat_id"],
                        photo=image_file,
                        caption="Here is your edited image.",
                    )
            else:
                generated_path = await asyncio.wait_for(
                    asyncio.to_thread(_generate_video, image_path),
                    timeout=timeout_seconds,
                )
                if generated_path.resolve() != video_copy.resolve():
                    video_copy.write_bytes(generated_path.read_bytes())

                with video_copy.open("rb") as video_file:
                    await bot.send_video(
                        chat_id=job["chat_id"],
                        video=video_file,
                        caption="Here is your generated video.",
                    )
            wallet_store.complete_generation(user_id, reservation, mode=mode, prompt=job["prompt"])
            wallet_store.finish_generation_job(job["job_id"])
            balance = wallet_store.get_balance(user_id)
            await bot.send_message(chat_id=job["chat_id"], text=format_balance_summary(balance, mode))
            logger.info(
                "Generation job completed: job_id=%s mode=%s attempt=%s latency_seconds=%.2f",
                job["job_id"], mode, job.get("attempts", 1), time.monotonic() - started_at,
            )
    except asyncio.CancelledError:
        wallet_store.release_running_job(job["job_id"])
        raise
    except asyncio.TimeoutError:
        attempts = int(job.get("attempts", 1))
        retry_delay = max(1, int(os.getenv("JOB_RETRY_BASE_SECONDS", "15"))) * (2 ** max(0, attempts - 1))
        status = wallet_store.finish_generation_job(
            job["job_id"], failed=True, error="timeout", retry_delay_seconds=retry_delay,
            max_attempts=int(os.getenv("JOB_MAX_ATTEMPTS", "3")),
        )
        if status == "dead_letter":
            balance = wallet_store.refund_generation(user_id, reservation, mode=mode, error="timeout")
            await bot.send_message(chat_id=job["chat_id"], text="The generation timed out after retries. Your credit was restored. " + format_balance_summary(balance, mode))
        logger.warning(
            "Generation job timeout: job_id=%s status=%s latency_seconds=%.2f",
            job["job_id"], status, time.monotonic() - started_at,
        )
    except Exception:
        attempts = int(job.get("attempts", 1))
        retry_delay = max(1, int(os.getenv("JOB_RETRY_BASE_SECONDS", "15"))) * (2 ** max(0, attempts - 1))
        status = wallet_store.finish_generation_job(
            job["job_id"], failed=True, error="upstream failure", retry_delay_seconds=retry_delay,
            max_attempts=int(os.getenv("JOB_MAX_ATTEMPTS", "3")),
        )
        logger.exception("Generation job failed: job_id=%s status=%s", job["job_id"], status)
        if status == "dead_letter":
            balance = wallet_store.refund_generation(user_id, reservation, mode=mode, error="upstream failure")
            await bot.send_message(chat_id=job["chat_id"], text="I couldn’t complete this after retries. Your credit was restored. " + format_balance_summary(balance, mode))
        logger.warning(
            "Generation job failed: job_id=%s status=%s latency_seconds=%.2f",
            job["job_id"], status, time.monotonic() - started_at,
        )


async def _generation_worker(application: Application) -> None:
    max_attempts = max(1, int(os.getenv("JOB_MAX_ATTEMPTS", "3")))
    poll_seconds = max(1, float(os.getenv("JOB_POLL_SECONDS", "2")))
    while True:
        job = await asyncio.to_thread(
            wallet_store.claim_next_generation_job, max_attempts=max_attempts
        )
        if job:
            if _worker_semaphore is None:
                await _process_generation_job(application, job)
            else:
                async with _worker_semaphore:
                    await _process_generation_job(application, job)
        else:
            metrics = await asyncio.to_thread(wallet_store.get_queue_metrics)
            logger.info("Generation queue health: %s", metrics)
            await asyncio.sleep(poll_seconds)


async def _post_init(application: Application) -> None:
    recovered = await asyncio.to_thread(
        wallet_store.recover_stale_jobs,
        max_age_seconds=int(os.getenv("JOB_RUNNING_STALE_SECONDS", "1800")),
        max_attempts=max(1, int(os.getenv("JOB_MAX_ATTEMPTS", "3"))),
    )
    if recovered:
        logger.warning("Requeued stale running jobs: count=%s", recovered)
    global _worker_semaphore
    _worker_semaphore = asyncio.Semaphore(max(1, int(os.getenv("JOB_GLOBAL_CONCURRENCY", "1"))))
    worker_count = max(1, int(os.getenv("JOB_WORKERS", "1")))
    application.bot_data["generation_workers"] = [
        asyncio.create_task(_generation_worker(application)) for _ in range(worker_count)
    ]


async def _post_shutdown(application: Application) -> None:
    workers = application.bot_data.pop("generation_workers", [])
    for worker in workers:
        worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors without exposing the bot token."""
    error_type = type(context.error).__name__ if context.error else "UnknownError"
    logger.error("Telegram update error: %s", error_type)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN. Set it in Terminal before starting the bot."
        )
    if not os.getenv("HF_SPACE_ID"):
        raise SystemExit(
            "Missing HF_SPACE_ID. Set it to your duplicated Hugging Face Space ID before starting."
        )

    recovered = wallet_store.release_stale_reservations(
        max_age_seconds=int(os.getenv("RESERVATION_MAX_AGE_SECONDS", "1800"))
    )
    if recovered:
        logger.info("Recovered stale generation reservations: count=%s", recovered)

    application = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mode", mode_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("paysupport", paysupport_command))
    application.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:"))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, select_mode)
    )
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_error_handler(handle_error)

    print("Bot is running. Use a service manager or Docker for unattended operation.")
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if webhook_url:
        secret_path = os.getenv("WEBHOOK_PATH", token[-16:])
        secret_token = os.getenv("WEBHOOK_SECRET", "")
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", "8080")),
            url_path=secret_path,
            webhook_url=f"{webhook_url.rstrip('/')}/{secret_path}",
            secret_token=secret_token or None,
            drop_pending_updates=False,
        )
    else:
        logger.warning("WEBHOOK_URL is not set; falling back to polling. Use one instance per bot token.")
        application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
