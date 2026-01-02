from flask import Flask
from threading import Thread
import os

# --- WEB SERVER KEEP-ALIVE ---
app = Flask('')

@app.route('/')
def home():
    return "I am alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
# -----------------------------
import logging
import csv
import io
import asyncio
from telegram import Update, Poll
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# ---------------- CONFIGURATION ---------------- #
# Yaha apna BotFather wala Token replace karein
TOKEN = "8596285106:AAGol_SLrEZlKSIf5wHNpOgY8UYCso2u-s0" 
# ----------------------------------------------- #

# Logging setup (Error tracking ke liye)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot start hone par ye message dikhayega"""
    await update.message.reply_text(
        "👋 Welcome! Mujhe CSV file bheje ya CSV text paste karein.\n\n"
        "Format hona chahiye:\n"
        "`Question,Option A,Option B,Option C,Option D,Answer,Description`\n\n"
        "Rules:\n"
        "1. Answer must be A, B, C, or D\n"
        "2. Description < 240 chars",
        parse_mode="Markdown"
    )

async def process_csv_content(update: Update, context: ContextTypes.DEFAULT_TYPE, csv_file_content: str):
    """CSV content ko parse karke Polls bhejne ka logic"""
    
    # CSV file ko read karna
    f = io.StringIO(csv_file_content)
    reader = csv.DictReader(f)
    
    # Headers check karna
    required_headers = ['Question', 'Option A', 'Option B', 'Option C', 'Option D', 'Answer', 'Description']
    if reader.fieldnames != required_headers:
        await update.message.reply_text(
            f"❌ Error: CSV Headers match nahi ho rahe.\nExpected: {required_headers}\nGot: {reader.fieldnames}"
        )
        return

    count = 0
    await update.message.reply_text("⏳ Processing quiz... Please wait.")

    for row in reader:
        question = row['Question'].strip()
        options = [
            row['Option A'].strip(),
            row['Option B'].strip(),
            row['Option C'].strip(),
            row['Option D'].strip()
        ]
        answer_key = row['Answer'].strip().upper()
        explanation = row['Description'].strip()

        # --- VALIDATION ---
        # 1. Answer A/B/C/D check
        mapper = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
        if answer_key not in mapper:
            await update.message.reply_text(f"⚠️ Skipping Question: '{question[:20]}...' (Answer {answer_key} valid nahi hai)")
            continue
        
        correct_option_id = mapper[answer_key]

        # 2. Description length check
        if len(explanation) > 240:
            explanation = explanation[:237] + "..." # Truncate kar diya taaki error na aaye

        # --- SEND POLL ---
        try:
            await context.bot.send_poll(
                chat_id=update.effective_chat.id,
                question=question,
                options=options,
                type=Poll.QUIZ,
                correct_option_id=correct_option_id,
                explanation=explanation,
                is_anonymous=False # Name dikhega kisne answer diya (Optional)
            )
            count += 1
            # Rate limit avoid karne ke liye thoda wait
            await asyncio.sleep(1) 

        except Exception as e:
            logging.error(f"Error sending poll: {e}")
            await update.message.reply_text(f"❌ Error in question: {question}\nError: {str(e)}")

    await update.message.reply_text(f"✅ Done! Total {count} quizzes generated.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Jab user .csv file bhejta hai"""
    document = update.message.document
    
    # Check if file is CSV
    if not document.file_name.endswith('.csv'):
        await update.message.reply_text("❌ Kripya sirf .csv file bhejein.")
        return

    file = await context.bot.get_file(document.file_id)
    
    # File download karke memory me read karein
    file_byte_array = await file.download_as_bytearray()
    content = file_byte_array.decode('utf-8')
    
    await process_csv_content(update, context, content)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Jab user direct CSV code paste karta hai"""
    text = update.message.text
    # Simple check agar text me CSV headers hain
    if "Question,Option A" in text:
        await process_csv_content(update, context, text)
    else:
        await update.message.reply_text("⚠️ Ye valid CSV format nahi lag raha. Start command use karein help ke liye.")

if __name__ == '__main__':
    application = ApplicationBuilder().token(TOKEN).build()
    
    # Handlers add karna
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.Document.MimeType("text/csv"), handle_document))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    
    print("🤖 Bot is Running...")
    application.run_polling()
    
