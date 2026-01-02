import logging
import csv
import io
import asyncio
from typing import Dict, List, Tuple, Optional
from telegram import Update, Poll
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ---------------- CONFIGURATION ---------------- #
# Replace with your BotFather token
TOKEN = "YOUR_BOT_TOKEN_HERE"
# ----------------------------------------------- #

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Required canonical headers (human-readable)
REQUIRED_HEADERS = [
    "Question",
    "Option A",
    "Option B",
    "Option C",
    "Option D",
    "Answer",
    "Description",
]


def _normalize_header(h: str) -> str:
    """Normalize header text to a compact alphanumeric lower-case key."""
    return "".join(ch.lower() for ch in h if ch.isalnum())


REQUIRED_NORMALS = {_normalize_header(h) for h in REQUIRED_HEADERS}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot start message"""
    await update.message.reply_text(
        "👋 Welcome! Send me a .csv file (or paste CSV text) to create quiz polls.\n\n"
        "Expected header format (order doesn't matter):\n"
        "`Question,Option A,Option B,Option C,Option D,Answer,Description`\n\n"
        "Rules:\n"
        "1. Answer must be A, B, C, or D\n"
        "2. Description <= 240 chars",
        parse_mode="Markdown",
    )


async def process_csv_content(
    update: Update, context: ContextTypes.DEFAULT_TYPE, csv_file_content: str
):
    """
    Parse CSV content and send Telegram quiz polls.
    This function is robust to BOM, delimiters, header case/spacing differences.
    """
    # Normalize BOM and ensure it's a string stream
    f_stream = io.StringIO(csv_file_content)
    sample = f_stream.read(4096)
    f_stream.seek(0)

    # Try to sniff dialect (delimiter) if possible
    dialect = None
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(sample)
    except Exception:
        dialect = csv.excel  # fallback to default comma-delimited

    # Read header row to validate/match columns
    reader = csv.reader(f_stream, dialect)
    try:
        headers = next(reader)
    except StopIteration:
        await update.message.reply_text("❌ Error: CSV appears to be empty.")
        return

    # Normalize header names and build mapping from required normal -> original header index
    header_norms = [_normalize_header(h) for h in headers]
    header_map: Dict[str, int] = {hn: idx for idx, hn in enumerate(header_norms)}

    missing = REQUIRED_NORMALS - set(header_norms)
    if missing:
        # Provide helpful message about which canonical fields are missing
        human_missing = []
        for m in missing:
            # find corresponding human header name if possible
            for rh in REQUIRED_HEADERS:
                if _normalize_header(rh) == m:
                    human_missing.append(rh)
                    break
        await update.message.reply_text(
            "❌ Error: CSV headers missing or mismatched.\n"
            f"Expected headers (case-insensitive): {REQUIRED_HEADERS}\n"
            f"Missing: {human_missing}\n\n"
            f"Found headers: {headers}"
        )
        return

    # Rewind and use DictReader but fix fieldnames to the exact header strings present
    f_stream.seek(0)
    dict_reader = csv.DictReader(f_stream, fieldnames=headers, dialect=dialect)

    # Skip the header row that's already read by DictReader (it treats first row as data if we pass fieldnames)
    next(dict_reader, None)

    count = 0
    await update.message.reply_text("⏳ Processing quiz... Please wait.")

    # Helper to get value by required canonical name
    def get_from_row(row: Dict[str, str], req_normal: str) -> str:
        idx = header_map[req_normal]
        orig_header = headers[idx]
        return (row.get(orig_header) or "").strip()

    for row_number, row in enumerate(dict_reader, start=1):
        # Extract values robustly
        try:
            question = get_from_row(row, _normalize_header("Question"))
            options = [
                get_from_row(row, _normalize_header("Option A")),
                get_from_row(row, _normalize_header("Option B")),
                get_from_row(row, _normalize_header("Option C")),
                get_from_row(row, _normalize_header("Option D")),
            ]
            answer_key = get_from_row(row, _normalize_header("Answer")).upper()
            explanation = get_from_row(row, _normalize_header("Description"))
        except Exception as e:
            logger.exception("Row parsing error")
            await update.message.reply_text(
                f"⚠️ Skipping row {row_number}: unable to parse columns. Error: {e}"
            )
            continue

        # Basic validations
        if not question:
            await update.message.reply_text(f"⚠️ Skipping empty question at row {row_number}.")
            continue

        if any(opt == "" for opt in options):
            await update.message.reply_text(
                f"⚠️ Skipping question (missing options) at row {row_number}: {question[:40]}..."
            )
            continue

        mapper = {"A": 0, "B": 1, "C": 2, "D": 3}
        if answer_key not in mapper:
            await update.message.reply_text(
                f"⚠️ Skipping question (invalid answer '{answer_key}') at row {row_number}: {question[:40]}..."
            )
            continue

        correct_option_id = mapper[answer_key]

        # Trim explanation to allowed length for Telegram quiz explanation
        if len(explanation) > 240:
            explanation = explanation[:237] + "..."

        # Send poll (quiz)
        try:
            await context.bot.send_poll(
                chat_id=update.effective_chat.id,
                question=question,
                options=options,
                type=Poll.QUIZ,
                correct_option_id=correct_option_id,
                explanation=explanation,
                is_anonymous=False,
            )
            count += 1
            # small delay to reduce risk of hitting rate limits
            await asyncio.sleep(1.2)
        except Exception as e:
            logger.exception("Error sending poll")
            await update.message.reply_text(
                f"❌ Error sending poll for question at row {row_number}: {question[:60]}...\nError: {e}"
            )

    await update.message.reply_text(f"✅ Done! Total {count} quizzes generated.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming document files (CSV)."""
    document = update.message.document

    if not document or not document.file_name:
        await update.message.reply_text("❌ No document found in the message.")
        return

    # Accept .csv extension case-insensitively
    if not document.file_name.lower().endswith(".csv"):
        await update.message.reply_text("❌ Please send a file with .csv extension.")
        return

    # Download file
    try:
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        logger.exception("Failed to download file")
        await update.message.reply_text(f"❌ Failed to download the file: {e}")
        return

    # Try decode robustly: prefer utf-8-sig to drop BOM
    content = None
    for enc in ("utf-8-sig", "utf-8", "latin1"):
        try:
            content = file_bytes.decode(enc)
            break
        except Exception:
            continue

    if content is None:
        # last resort with replacement to avoid crashing
        content = file_bytes.decode("utf-8", errors="replace")

    # Pass content to CSV processor
    await process_csv_content(update, context, content)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pasted CSV text directly."""
    text = update.message.text or ""
    # quick header presence test (case-insensitive)
    if "question" in text.lower() and "option a" in text.lower():
        await process_csv_content(update, context, text)
    else:
        await update.message.reply_text(
            "⚠️ This doesn't look like CSV content. Use /start for expected format or send a .csv file."
        )


if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    # Accept any document and check extension inside handler (more reliable across Telegram clients)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    print("🤖 Bot is Running...")
    application.run_polling()