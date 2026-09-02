import os
import re
import io
import logging
import asyncio
import textwrap
import json
import time
import sqlite3
import random
import string
from functools import wraps
from bs4 import BeautifulSoup
import unidecode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    CallbackQueryHandler,
    PollAnswerHandler,
    filters,
)

# Enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Admin Configuration ---
OWNER_IDS = [1029966856, 5015873377] 
DB_FILE = "authorized_users.json"
SETTINGS_FILE = "bot_settings.json"

def load_authorized_users():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return OWNER_IDS
    return OWNER_IDS

def save_authorized_users():
    with open(DB_FILE, "w") as f:
        json.dump(list(AUTHORIZED_USERS), f)

# --- Settings Management ---
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"protect_content": True}  

def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump(BOT_SETTINGS, f)

AUTHORIZED_USERS = set(load_authorized_users())
BOT_SETTINGS = load_settings()

# --- Security Decorators ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in AUTHORIZED_USERS:
            if update.message:
                await update.message.reply_text("🚫 Access Denied.", protect_content=BOT_SETTINGS["protect_content"])
            elif update.callback_query:
                await update.callback_query.answer("🚫 Access Denied.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def owner_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id not in OWNER_IDS:
            if update.message:
                await update.message.reply_text("❌ Owner only command.", protect_content=BOT_SETTINGS["protect_content"])
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect("quizzes.db")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS quizzes 
                      (id TEXT PRIMARY KEY, name TEXT, timer INTEGER, negative REAL, 
                       promo TEXT, type TEXT, creator TEXT, question_count INTEGER, user_id INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS questions 
                      (quiz_id TEXT, question_text TEXT, options TEXT, correct_id INTEGER, explanation TEXT)''')
    conn.commit()
    conn.close()

# States
QUIZ_NAME, ADD_CONTENT, TIMER, QUIZ_TYPE = range(4)

def generate_quiz_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))


# --- Rich Text Detection and Handling ---

_HTML_TAG = re.compile(r'<[^>]+>')
_MD_PATTERNS = [
    re.compile(r'\*\*.*?\*\*'),
    re.compile(r'__.*?__'),
    re.compile(r'\*.*?\*'),
    re.compile(r'_.*?_'),
    re.compile(r'`.*?`')
]
_LATEX_PATTERNS = [
    re.compile(r'\$.*?\$'),
    re.compile(r'\\\[.*?\\\]'),
    re.compile(r'\\\(.*?\\\)')
]

def _is_rich(text: str) -> bool:
    if not text:
        return False
    if _HTML_TAG.search(text):
        return True
    for pattern in _MD_PATTERNS:
        if pattern.search(text):
            return True
    for pattern in _LATEX_PATTERNS:
        if pattern.search(text):
            return True
    return False

def _detect_mode(text: str) -> str:
    if _HTML_TAG.search(text):
        return 'HTML'
    return 'MarkdownV2'

async def send_rich_or_fallback(context, chat_id, text, mode, bot_settings):
    try:
        return await context.bot.send_message(chat_id, text=text, parse_mode=mode, protect_content=bot_settings["protect_content"])
    except Exception as e:
        logger.warning(f"Rich text sending failed: {e}. Falling back to plain text.")
        try:
            # Fallback formatting stripping
            clean_text = BeautifulSoup(text, "html.parser").get_text()
            clean_text = re.sub(r'[*_`$]', '', clean_text)
            clean_text = unidecode.unidecode(clean_text)
        except Exception:
            clean_text = text
        return await context.bot.send_message(chat_id, text=clean_text, protect_content=bot_settings["protect_content"])

async def enrich_question_dispatch(context, chat_id, i, total_q, txt, opts, expl, cid, timer, bot_settings):
    needs_rich = _is_rich(txt) or any(_is_rich(o) for o in opts) or (expl and _is_rich(expl))
    
    poll_question_text = f"[{i+1}/{total_q}] {txt}"
    needs_split = False
    
    if len(poll_question_text) > 290 or any(len(opt) > 90 for opt in opts):
        needs_split = True
        
    if not needs_rich and not needs_split:
        return await context.bot.send_poll(
            chat_id, poll_question_text, opts, type=Poll.QUIZ, correct_option_id=cid, open_period=timer, 
            is_anonymous=False, explanation=expl[:200] if expl else None, protect_content=bot_settings["protect_content"]
        )
    else:
        poll_question = f"[{i+1}/{total_q}] Choose the correct option:"
        poll_opts = []
        
        if not needs_rich:
            # Native fallback for purely length-based splits
            safe_txt = txt.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '\n\n')
            quote_text = f"<b>Question [{i+1}/{total_q}]</b>\n\n{safe_txt}\n"
            for j, opt in enumerate(opts):
                letter = chr(65 + j)
                safe_opt = opt.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                quote_text += f"\n<b>{letter})</b> {safe_opt}\n"
                poll_opts.append(letter)
            mode = 'HTML'
        else:
            # Full rich text dispatch
            quote_text = f"Question [{i+1}/{total_q}]\n\n{txt}\n"
            for j, opt in enumerate(opts):
                letter = chr(65 + j)
                quote_text += f"\n{letter}) {opt}\n"
                poll_opts.append(letter)
            mode = _detect_mode(quote_text)

        # Pre-formatted Rich Message dispatch
        await send_rich_or_fallback(context, chat_id, quote_text, mode, bot_settings)

        # Process expl fallback to plain text for native poll
        clean_expl = None
        if expl:
            try:
                clean_expl = BeautifulSoup(expl, "html.parser").get_text()
                clean_expl = re.sub(r'[*_`$]', '', clean_expl)
            except Exception:
                clean_expl = expl

        # Actual Telegram Poll with placeholder text
        return await context.bot.send_poll(
            chat_id, poll_question, poll_opts, type=Poll.QUIZ, correct_option_id=cid, open_period=timer, 
            is_anonymous=False, explanation=clean_expl[:200] if clean_expl else None, protect_content=bot_settings["protect_content"]
        )


# --- Text Parsing ---

def apply_unicode_formatting(text: str) -> str:
    """Finds formula patterns (like CH_4) and replaces them with Unicode Math Italics and Subscripts."""
    if not text: 
        return text
    
    sub_digits = {'0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄', 
                  '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉'}
    
    def replace_formula(match):
        word = match.group(0)
        res = ""
        i = 0
        while i < len(word):
            if word[i] == '_' and i + 1 < len(word) and word[i+1].isdigit():
                i += 1
                while i < len(word) and word[i].isdigit():
                    res += sub_digits[word[i]]
                    i += 1
            else:
                c = word[i]
                if 'A' <= c <= 'Z':
                    res += chr(119860 + ord(c) - 65)
                elif 'a' <= c <= 'z':
                    if c == 'h': 
                        res += '\u210E'
                    else: 
                        res += chr(119886 + ord(c) - 97)
                else:
                    res += c
                i += 1
        return res

    text = re.sub(r'\b[0-9]*[A-Za-z]+_\d+[A-Za-z0-9_]*\b', replace_formula, text)
    
    def replace_standalone(match):
        return "".join(sub_digits[d] for d in match.group(1))
        
    text = re.sub(r'_(\d+)', replace_standalone, text)
    
    return text

def parse_markdown_table(table_lines: list[str]) -> tuple[list[str], list[list[str]]]:
    """Parses markdown table strings into headers and row structures."""
    if not table_lines or len(table_lines) < 2:
        return [], []
    
    def clean_row(line: str) -> list[str]:
        cols = line.strip().split('|')
        if not cols: return []
        if cols[0].strip() == '': cols = cols[1:]
        if cols and cols[-1].strip() == '': cols = cols[:-1]
        return [c.strip() for c in cols]

    headers = clean_row(table_lines[0])
    rows = []
    
    for line in table_lines[1:]:
        cols = clean_row(line)
        if all(set(c).issubset({'-', ':', ' '}) for c in cols if c):
            continue
        if len(cols) < len(headers):
            cols.extend([''] * (len(headers) - len(cols)))
        elif len(cols) > len(headers):
            cols = cols[:len(headers)]
        rows.append(cols)
    return headers, rows

def parse_quiz_txt(content: str) -> list[dict]:
    content = apply_unicode_formatting(content)
    content = content.replace("\r\n", "\n")
    raw_blocks = re.split(r'\n\s*\n', content.strip())
    parsed_questions = []
    
    for block in raw_blocks:
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        if not lines: continue
        
        q_lines = []
        table_lines = []
        options = []
        correct_index = 0
        explanation = ""
        
        for line in lines:
            if re.match(r'^(ex|explanation)\s*:\s*', line, re.IGNORECASE) or line.startswith("Ex:"):
                explanation = re.sub(r'^(ex|explanation)\s*:\s*', '', line, flags=re.IGNORECASE).strip()
                if not explanation and line.startswith("Ex:"):
                    explanation = line.replace("Ex:", "").strip()
                continue
            
            opt_match = re.match(r'^[A-F][\)\.]\s*(.*)', line, re.IGNORECASE)
            if opt_match:
                opt_text = opt_match.group(1).strip()
                if "✅" in opt_text:
                    correct_index = len(options)
                    opt_text = opt_text.replace("✅", "").strip()
                options.append(opt_text)
            else:
                if not options:
                    if line.startswith('|') and line.endswith('|'):
                        table_lines.append(line)
                    else:
                        q_lines.append(line)
        
        if (q_lines or table_lines) and options:
            parsed_questions.append({
                "question": "\n".join(q_lines),
                "table_lines": table_lines,
                "options": options,
                "correct_id": correct_index,
                "explanation": explanation[:200]
            })
            
    return parsed_questions

def generate_txt_file(questions):
    output = io.StringIO()
    for q in questions:
        output.write(f"{q['question']}\n")
        
        if 'table_lines' in q and q['table_lines']:
            for line in q['table_lines']:
                output.write(f"{line}\n")
                
        for i, opt in enumerate(q['options']):
            if i == q['correct_id']:
                output.write(f"{chr(65+i)}) {opt} ✅\n")
            else:
                output.write(f"{chr(65+i)}) {opt}\n")
        
        exp = q.get('explanation', '')
        if exp:
            output.write(f"Ex: {exp[:200]}\n")
        output.write("\n")
    output.seek(0)
    return output

def generate_html_file(quiz_name, questions):
    """Generates the interactive HTML version of the quiz."""
    html_qs = []
    for q in questions:
        q_text = q['question'].replace('\n', '<br>')
        
        if q.get('table_lines'):
            headers, rows = parse_markdown_table(q['table_lines'])
            if headers or rows:
                table_html = '<div style="overflow-x:auto; margin: 15px 0;"><table style="width: 100%; border-collapse: collapse; font-size: 14px; text-align: left; background: white; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">'
                if headers:
                    table_html += '<thead style="background-color: #f1f5f9;"><tr>'
                    for h in headers:
                        table_html += f'<th style="border: 1px solid #e2e8f0; padding: 10px; font-weight: 700;">{h}</th>'
                    table_html += '</tr></thead>'
                table_html += '<tbody>'
                for row in rows:
                    table_html += '<tr>'
                    for cell in row:
                        table_html += f'<td style="border: 1px solid #e2e8f0; padding: 10px;">{cell}</td>'
                    table_html += '</tr>'
                table_html += '</tbody></table></div>'
                q_text += table_html

        html_qs.append({
            "q": q_text,
            "options": q['options'],
            "correct": q['correct_id'],
            "exp": q.get('explanation', '').replace('\n', '<br>')
        })

    questions_json_str = json.dumps(html_qs)
    
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
    <title>{quiz_name} - Interactive Quiz</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {{ --primary: #6366f1; --primary-light: #e0e7ff; --secondary: #a855f7; --accent: #f43f5e; --success: #10b981; --warning: #f59e0b; --bg-gradient: linear-gradient(135deg, #6366f1 0%, #a855f7 100%); --glass-bg: rgba(255, 255, 255, 0.95); --text-dark: #0f172a; --text-light: #64748b; --shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1); }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }}
        body {{ background: var(--bg-gradient); background-attachment: fixed; display: flex; justify-content: center; align-items: center; min-height: 100vh; color: var(--text-dark); }}
        .app-window {{ background: var(--glass-bg); width: 100%; max-width: 440px; height: 92vh; border-radius: 32px; overflow: hidden; position: relative; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); display: flex; flex-direction: column; backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.3); }}
        .screen-content {{ padding: 30px 24px; overflow-y: auto; flex: 1; scroll-behavior: smooth; }}
        
        .brand-section {{ text-align: center; padding-top: 20px; }}
        .app-logo {{ width: 90px; height: 90px; background: white; border-radius: 24px; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; box-shadow: var(--shadow); background: linear-gradient(135deg, #fff 0%, #f0f0f0 100%); }}
        .app-logo i {{ font-size: 45px; background: var(--bg-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .app-name {{ font-size: 32px; font-weight: 800; letter-spacing: -1px; margin-bottom: 8px; }}
        .sub-text {{ color: var(--text-light); font-size: 15px; margin-bottom: 40px; }}
        .mode-card {{ display: flex; align-items: center; padding: 22px; border-radius: 24px; background: white; border: 2px solid #f1f5f9; margin-bottom: 18px; cursor: pointer; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); }}
        .mode-card:hover {{ transform: translateY(-3px); border-color: var(--primary); }}
        .mode-card.selected {{ border-color: var(--primary); background: #f5f7ff; box-shadow: 0 10px 20px rgba(99, 102, 241, 0.15); }}
        .icon-box {{ width: 54px; height: 54px; border-radius: 16px; display: flex; align-items: center; justify-content: center; margin-right: 18px; font-size: 22px; flex-shrink: 0; }}
        .icon-exam {{ background: #fff1f2; color: #f43f5e; }}
        .icon-practice {{ background: #ecfdf5; color: #10b981; }}
        .mode-info h4 {{ font-size: 17px; font-weight: 700; margin-bottom: 2px; }}
        .mode-info p {{ font-size: 13px; color: var(--text-light); }}

        .exam-header {{ padding: 12px 20px; background: white; border-bottom: 1px solid #f1f5f9; }}
        .timer-pill {{ background: #1e293b; color: white; padding: 6px 12px; border-radius: 50px; font-weight: 700; font-size: 12px; display: flex; align-items: center; gap: 6px; }}
        .badge {{ font-size: 10px; padding: 4px 12px; border-radius: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; }}
        .badge-exam {{ background: var(--accent); color: white; }}
        .badge-practice {{ background: var(--success); color: white; }}
        .progress-container {{ margin-top: 10px; }}
        .progress-bar {{ height: 6px; background: #f1f5f9; border-radius: 10px; overflow: hidden; }}
        .progress-fill {{ height: 100%; background: var(--bg-gradient); width: 0%; transition: width 0.4s ease; }}
        
        .q-tag {{ color: var(--primary); font-weight: 700; font-size: 11px; margin-bottom: 8px; display: block; }}
        .q-text {{ font-size: 14px; font-weight: 600; line-height: 1.4; color: var(--text-dark); margin-bottom: 15px; }}
        .option-item {{ padding: 10px 14px; border: 2px solid #f1f5f9; border-radius: 14px; margin-bottom: 8px; cursor: pointer; display: flex; align-items: center; gap: 12px; font-size: 13px; font-weight: 600; transition: 0.2s; position: relative; background: white; }}
        .option-item:hover {{ border-color: var(--primary-light); background: #fafbff; }}
        .option-item.selected {{ border-color: var(--primary); background: #f5f7ff; }}
        .option-item .circle {{ width: 26px; height: 26px; border: 2px solid #e2e8f0; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 12px; color: var(--text-light); transition: 0.2s; flex-shrink: 0; }}
        .option-item.selected .circle {{ background: var(--primary); color: white; border-color: var(--primary); }}
        .option-item.correct {{ border-color: var(--success) !important; background: #f0fdf4 !important; }}
        .option-item.correct .circle {{ background: var(--success); color: white; border-color: var(--success); }}
        .option-item.incorrect {{ border-color: var(--accent) !important; background: #fff1f2 !important; }}
        .option-item.incorrect .circle {{ background: var(--accent); color: white; border-color: var(--accent); }}
        .explanation-box {{ margin-top: 12px; background: #f8fafc; border-radius: 12px; padding: 12px; border-left: 5px solid var(--primary); animation: slideIn 0.4s ease; }}
        
        .footer-controls {{ padding: 12px 20px; background: white; display: flex; gap: 12px; border-top: 1px solid #f1f5f9; }}
        .btn-nav {{ padding: 12px; border-radius: 12px; border: 1px solid #e2e8f0; background: white; font-weight: 700; font-size: 13px; color: var(--text-light); cursor: pointer; transition: 0.2s; flex: 1; }}
        .btn-next {{ background: var(--bg-gradient); color: white; border: none; flex: 1.8; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3); }}
        
        .res-card {{ background: white; padding: 20px; border-radius: 24px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); border: 1px solid #f1f5f9; }}
        .res-icon {{ width: 44px; height: 44px; border-radius: 14px; display: flex; align-items: center; justify-content: center; margin-bottom: 12px; color: white; font-size: 20px; }}
        .floating-nav-trigger {{ position: absolute; right: 20px; bottom: 80px; width: 48px; height: 48px; font-size: 16px; background: var(--text-dark); color: white; border-radius: 16px; display: flex; align-items: center; justify-content: center; cursor: pointer; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.3); z-index: 5; }}
        .nav-sheet {{ position: absolute; bottom: 0; left: 0; width: 100%; background: white; border-radius: 32px 32px 0 0; transform: translateY(100%); transition: 0.4s cubic-bezier(0.4, 0, 0.2, 1); z-index: 100; padding: 30px 24px; box-shadow: 0 -20px 25px -5px rgba(0,0,0,0.1); }}
        .nav-sheet.open {{ transform: translateY(0); }}
        .nav-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-top: 20px; }}
        .nav-dot {{ height: 50px; border-radius: 16px; background: #f8fafc; border: 1px solid #e2e8f0; display: flex; align-items: center; justify-content: center; font-weight: 700; cursor: pointer; }}
        .nav-dot.active {{ background: var(--primary); color: white; border-color: var(--primary); }}
        .nav-dot.marked {{ background: var(--warning); color: white; border-color: var(--warning); }}
        #quizScreen, #results, #reviewScreen {{ display: none; height: 100%; flex-direction: column; }}
        
        #quizScreen .screen-content {{ padding: 15px 20px; }}
        #reviewScreen .screen-content {{ padding: 15px 20px; }}

        @keyframes slideIn {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        .res-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 20px; }}
    </style>
</head>
<body>
<div class="app-window">
    <div id="modeSelection" class="screen-content">
        <div class="brand-section">
            <div class="app-logo"><i class="fas fa-graduation-cap"></i></div>
            <h1 class="app-name">{quiz_name}</h1>
            <p class="sub-text">Prepare better. Score higher.</p>
        </div>
        <div class="mode-card" onclick="selectMode('exam', this)">
            <div class="icon-box icon-exam"><i class="fas fa-stopwatch"></i></div>
            <div class="mode-info"><h4>Exam Mode</h4><p>Real-time pressure, final results</p></div>
        </div>
        <div class="mode-card" onclick="selectMode('practice', this)">
            <div class="icon-box icon-practice"><i class="fas fa-lightbulb"></i></div>
            <div class="mode-info"><h4>Practice Mode</h4><p>Instant feedback & explanations</p></div>
        </div>
        <div style="text-align: left; margin-top: 25px;">
            <label style="font-size:14px; font-weight:700; color: var(--text-dark); margin-left: 5px;">Set Duration (minutes)</label>
            <input type="number" id="timerInput" placeholder="Default: 3 mins" style="width:100%; padding:18px; border-radius:18px; border:2px solid #f1f5f9; margin-top:10px; outline: none; font-weight: 600;">
        </div>
        <button id="startBtn" class="btn-nav btn-next" style="margin-top: 40px; width: 100%; padding: 16px;" onclick="startQuiz()" disabled>Unlock Quiz</button>
    </div>
    <div id="quizScreen">
        <div class="exam-header">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div><span id="modeBadge" class="badge"></span><h4 style="margin-top: 4px; font-weight: 800; font-size: 14px;">{quiz_name}</h4></div>
                <div class="timer-pill"><i class="far fa-clock"></i> <span id="timerDisplay">03:00</span></div>
            </div>
            <div class="progress-container">
                <div style="display:flex; justify-content:space-between; font-size:11px; font-weight:700; color:var(--text-light); margin-bottom:6px;"><span id="qHeaderCount"></span><span id="attemptCount">0 Attempted</span></div>
                <div class="progress-bar"><div id="progressFill" class="progress-fill"></div></div>
            </div>
        </div>
        <div class="screen-content"><div id="questionArea"></div></div>
        <div class="floating-nav-trigger" onclick="toggleNav()"><i class="fas fa-th"></i></div>
        <div class="footer-controls">
            <button class="btn-nav" onclick="prevQuestion()"><i class="fas fa-arrow-left"></i></button>
            <button class="btn-nav" onclick="markQuestion()"><i class="fas fa-bookmark"></i></button>
            <button id="nextBtn" class="btn-nav btn-next" onclick="nextQuestion()">Next Question</button>
        </div>
        <div id="navSheet" class="nav-sheet">
            <div style="display:flex; justify-content:space-between; align-items:center;"><h3 style="font-weight:800; font-size: 16px;">Jump to Question</h3><i class="fas fa-times" onclick="toggleNav()" style="cursor:pointer; color:var(--text-light)"></i></div>
            <div class="nav-grid" id="navGrid"></div>
        </div>
    </div>
    <div id="results" class="screen-content" style="text-align: center; padding: 30px 24px;">
        <div class="app-logo" style="margin-bottom: 10px;"><i class="fas fa-trophy" style="color: #f59e0b;"></i></div>
        <h2 style="font-weight: 800;">Great Effort!</h2>
        <p class="sub-text">Here is your performance summary</p>
        <div style="margin: 20px 0;"><div id="scoreText" style="font-size:48px; font-weight:900; color:var(--primary);"></div><div id="percentText" style="font-weight:700; color:var(--success);"></div></div>
        <div class="res-grid">
            <div class="res-card"><div class="res-icon" style="background:var(--success);"><i class="fas fa-check"></i></div><div class="res-val" id="resCorrect"></div><div class="res-label">Correct</div></div>
            <div class="res-card"><div class="res-icon" style="background:var(--accent);"><i class="fas fa-times"></i></div><div class="res-val" id="resWrong"></div><div class="res-label">Wrong</div></div>
            <div class="res-card"><div class="res-icon" style="background:var(--text-light);"><i class="fas fa-minus"></i></div><div class="res-val" id="resUn"></div><div class="res-label">Skipped</div></div>
            <div class="res-card"><div class="res-icon" style="background:var(--warning);"><i class="fas fa-bolt"></i></div><div class="res-val" id="resNeg"></div><div class="res-label">Penalties</div></div>
        </div>
        <button class="btn-nav btn-next" style="width:100%; margin-top:30px; padding: 14px;" onclick="showReview()">Review Analysis</button>
        <button class="btn-nav" style="width:100%; margin-top:12px; border:none; padding: 14px;" onclick="location.reload()">Try Again</button>
    </div>
    <div id="reviewScreen">
        <div class="exam-header" style="display:flex; align-items:center; gap:12px;">
            <button class="btn-nav" onclick="hideReview()" style="flex:none; width:35px; height:35px; padding:0; display:flex; align-items:center; justify-content:center; border-radius:10px;"><i class="fas fa-arrow-left"></i></button>
            <h3 style="font-weight:800; font-size: 15px;">Detailed Review</h3>
        </div>
        <div id="reviewArea" class="screen-content"></div>
    </div>
</div>
<script>
    const questions = {questions_json_str};
    let currentMode = null; let currentIdx = 0; let timeLeft = 180; let timerId = null;
    let answers = new Array(questions.length).fill(null); let marked = new Array(questions.length).fill(false);
    function selectMode(mode, element) {{ currentMode = mode; document.querySelectorAll('.mode-card').forEach(c => c.classList.remove('selected')); element.classList.add('selected'); document.getElementById('startBtn').disabled = false; document.getElementById('startBtn').style.opacity = "1"; }}
    function startQuiz() {{ const val = document.getElementById('timerInput').value; if(val) timeLeft = val * 60; document.getElementById('modeSelection').style.display = 'none'; document.getElementById('quizScreen').style.display = 'flex'; const badge = document.getElementById('modeBadge'); badge.innerText = currentMode + " Mode"; badge.className = `badge badge-${{currentMode}}`; renderQuestion(); renderNav(); startTimer(); }}
    function startTimer() {{ timerId = setInterval(() => {{ timeLeft--; let mins = Math.floor(timeLeft / 60); let secs = timeLeft % 60; document.getElementById('timerDisplay').innerText = `${{mins.toString().padStart(2,'0')}}:${{secs.toString().padStart(2,'0')}}`; if(timeLeft <= 0) endQuiz(); }}, 1000); }}
    function renderQuestion() {{
        const q = questions[currentIdx];
        document.getElementById('qHeaderCount').innerText = `Question ${{currentIdx + 1}}/${{questions.length}}`;
        document.getElementById('attemptCount').innerText = `${{answers.filter(a => a !== null).length}} Attempted`;
        document.getElementById('progressFill').style.width = ((currentIdx + 1) / questions.length * 100) + '%';
        let html = `<span class="q-tag">CONCEPT CHECK</span><div class="q-text">${{q.q}}</div><div class="options-container">`;
        q.options.forEach((opt, i) => {{
            let label = String.fromCharCode(65 + i); let cssClass = "";
            if(answers[currentIdx] !== null) {{ if(currentMode === 'practice') {{ if(i === q.correct) cssClass = "correct"; else if(i === answers[currentIdx]) cssClass = "incorrect"; }} else {{ if(i === answers[currentIdx]) cssClass = "selected"; }} }}
            html += `<div class="option-item ${{cssClass}}" onclick="handleSelection(${{i}})">
                <div class="circle">${{label}}</div><span>${{opt}}</span></div>`;
        }});
        if(currentMode === 'practice' && answers[currentIdx] !== null) {{
            html += `<div class="explanation-box"><div style="font-weight:800; color:var(--primary); font-size:12px; margin-bottom:5px;"><i class="fas fa-info-circle"></i> TEACHER'S NOTE</div><p style="font-size:12px; color:var(--text-light); line-height:1.4;">${{q.exp}}</p></div>`;
        }}
        html += `</div>`; document.getElementById('questionArea').innerHTML = html;
        document.getElementById('nextBtn').innerText = currentIdx === questions.length - 1 ? 'Finish Quiz' : 'Next Question';
    }}
    function handleSelection(idx) {{ if(currentMode === 'practice' && answers[currentIdx] !== null) return; answers[currentIdx] = idx; renderQuestion(); renderNav(); }}
    function nextQuestion() {{ if(currentIdx < questions.length - 1) {{ currentIdx++; renderQuestion(); }} else {{ endQuiz(); }} }}
    function prevQuestion() {{ if(currentIdx > 0) {{ currentIdx--; renderQuestion(); }} }}
    function markQuestion() {{ marked[currentIdx] = !marked[currentIdx]; renderNav(); }}
    function toggleNav() {{ document.getElementById('navSheet').classList.toggle('open'); }}
    function renderNav() {{ const grid = document.getElementById('navGrid'); grid.innerHTML = ''; questions.forEach((_, i) => {{ let status = answers[i] !== null ? 'active' : (marked[i] ? 'marked' : ''); grid.innerHTML += `<div class="nav-dot ${{status}}" onclick="jumpTo(${{i}})">${{i + 1}}</div>`; }}); }}
    function jumpTo(i) {{ currentIdx = i; renderQuestion(); toggleNav(); }}
    function endQuiz() {{
        clearInterval(timerId); let correct = 0, wrong = 0, un = 0;
        answers.forEach((ans, i) => {{ if(ans === null) un++; else if(ans === questions[i].correct) correct++; else wrong++; }});
        const score = correct - (wrong * 0.25);
        document.getElementById('quizScreen').style.display = 'none'; document.getElementById('results').style.display = 'flex';
        document.getElementById('scoreText').innerText = `${{score.toFixed(2)}} / ${{questions.length}}`;
        document.getElementById('percentText').innerText = `${{((correct / questions.length) * 100).toFixed(0)}}% Proficiency`;
        document.getElementById('resCorrect').innerText = correct; document.getElementById('resWrong').innerText = wrong;
        document.getElementById('resUn').innerText = un; document.getElementById('resNeg').innerText = "-" + (wrong * 0.25).toFixed(2);
    }}
    function showReview() {{
        document.getElementById('results').style.display = 'none'; document.getElementById('reviewScreen').style.display = 'flex';
        let html = '';
        questions.forEach((q, qIdx) => {{
            html += `<div style="margin-bottom:20px; background:white; padding:15px; border-radius:16px; border:1px solid #f1f5f9;"><span class="q-tag">QUESTION ${{qIdx + 1}}</span><div style="font-weight:600; font-size:13px; margin-bottom:12px;">${{q.q}}</div>`;
            q.options.forEach((opt, oIdx) => {{
                let status = (oIdx === q.correct) ? "correct" : (answers[qIdx] === oIdx ? "incorrect" : "");
                html += `<div class="option-item ${{status}}" style="cursor:default; padding:8px 12px; margin-bottom:6px;"><div class="circle" style="width:22px; height:22px; font-size:10px;">${{String.fromCharCode(65 + oIdx)}}</div><span style="font-size:12px;">${{opt}}</span></div>`;
            }});
            html += `<div class="explanation-box" style="background:#f1f5f9; border-left-color:var(--text-dark); padding: 10px; margin-top: 10px;"><p style="font-size:11px;">${{q.exp}}</p></div></div>`;
        }});
        document.getElementById('reviewArea').innerHTML = html;
    }}
    function hideReview() {{ document.getElementById('reviewScreen').style.display = 'none'; document.getElementById('results').style.display = 'flex'; }}
</script>
</body>
</html>"""
    return html_template


# --- Management Commands ---
@owner_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_list = "\n".join([f"• `{uid}`" for uid in AUTHORIZED_USERS])
    msg = f"🛠 **Admin Control Panel**\n\n**Authorized Users:**\n{user_list}\n\n`/add <ID>`\n`/remove <ID>`\n`/protect` - Toggle restrict"
    await update.message.reply_text(msg, parse_mode='Markdown', protect_content=BOT_SETTINGS["protect_content"])

@owner_only
async def toggle_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    BOT_SETTINGS["protect_content"] = not BOT_SETTINGS.get("protect_content", True)
    save_settings()
    status = "ENABLED 🔒" if BOT_SETTINGS["protect_content"] else "DISABLED 🔓"
    await update.message.reply_text(f"Protection is now {status}", protect_content=BOT_SETTINGS["protect_content"])

@owner_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        AUTHORIZED_USERS.add(int(context.args[0]))
        save_authorized_users()
        await update.message.reply_text(f"✅ User added.")
    except (IndexError, ValueError): pass

def get_main_menu_keyboard(quiz_id, bot_username):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Start Quiz Now", callback_data=f"start_{quiz_id}")],
        [InlineKeyboardButton("🚀 Start Quiz in Group", url=f"https://t.me/{bot_username}?startgroup={quiz_id}")],
    ])

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        quiz_id = context.args[0]
        if update.effective_chat.type in ["group", "supergroup"]:
            asyncio.create_task(run_quiz_loop(update, context, quiz_id))
            return ConversationHandler.END 
        await update.message.reply_text(f"Ready to start Quiz `{quiz_id}`?", reply_markup=get_main_menu_keyboard(quiz_id, context.bot.username), parse_mode='Markdown', protect_content=BOT_SETTINGS["protect_content"])
    else:
        await update.message.reply_text("👋 **Welcome!** Use /create to make a quiz.", parse_mode='Markdown')
    return ConversationHandler.END

# --- Quiz Control Commands ---
@restricted
async def pause_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.chat_data.get('quiz_state') == 'running':
        context.chat_data['quiz_state'] = 'paused'
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Resume", callback_data="resume_quiz")]])
        await update.message.reply_text("⏸️ Quiz Paused!", reply_markup=keyboard)
    else:
        await update.message.reply_text("No running quiz to pause.")

@restricted
async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.chat_data.get('quiz_state') in ['running', 'paused']:
        context.chat_data['quiz_state'] = 'stopped'
        await update.message.reply_text("⏹️ Quiz Stopped! Fetching results...")
    else:
        await update.message.reply_text("No active quiz to stop.")

# --- Quiz Creation Flow ---
@restricted
async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['temp_questions'] = []
    await update.message.reply_text("✨ **Quiz Creation**\nSend the **Name** of your quiz:", parse_mode='Markdown')
    return QUIZ_NAME

async def save_quiz_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("✅ Name Saved!\n\nSend/forward your **.txt file** only (CSV files strictly prohibited). Send /done when finished.", parse_mode='HTML')
    return ADD_CONTENT

async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.poll:
        p = update.message.poll
        context.user_data['temp_questions'].append({
            "question": apply_unicode_formatting(p.question), "options": [apply_unicode_formatting(o.text) for o in p.options],
            "correct_id": p.correct_option_id or 0, "explanation": apply_unicode_formatting(p.explanation) if p.explanation else ""
        })
        await update.message.reply_text(f"✅ Added Poll! Total: {len(context.user_data['temp_questions'])}", protect_content=BOT_SETTINGS["protect_content"])
        return ADD_CONTENT

    content_str = ""
    if update.message.document:
        fname = update.message.document.file_name.lower()
        if fname.endswith('.csv'):
            await update.message.reply_text("⚠️ **Error:** Only `.txt` inputs are allowed. CSVs have been removed.", protect_content=BOT_SETTINGS["protect_content"])
            return ADD_CONTENT
        if fname.endswith('.txt'):
            file = await update.message.document.get_file()
            byte_data = await file.download_as_bytearray()
            content_str = byte_data.decode('utf-8', errors='ignore')
    elif update.message.text:
        content_str = update.message.text

    if content_str:
        questions = parse_quiz_txt(content_str)
        if questions:
            context.user_data['temp_questions'].extend(questions)
            await update.message.reply_text(f"📥 Successfully imported {len(questions)} questions! Total: {len(context.user_data['temp_questions'])}. Send more or /done.", protect_content=BOT_SETTINGS["protect_content"])
        else:
            await update.message.reply_text("⚠️ No valid questions matched in the `.txt` input.", protect_content=BOT_SETTINGS["protect_content"])

    return ADD_CONTENT

async def ask_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('temp_questions'):
        await update.message.reply_text("❌ Add at least one question first.")
        return ADD_CONTENT
    await update.message.reply_text("⏳ Enter timer (11-100 seconds):")
    return TIMER

async def get_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text.isdigit() or not (11 <= int(text) <= 100):
        return TIMER
    context.user_data['timer'] = int(text)
    await update.message.reply_text("Enter quiz type (free/paid):")
    return QUIZ_TYPE

async def finalize_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz_id = generate_quiz_id()
    quiz_name = context.user_data['name']
    temp_qs = context.user_data['temp_questions']
    user_id = update.effective_user.id
    
    conn = sqlite3.connect("quizzes.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO quizzes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                   (quiz_id, quiz_name, context.user_data['timer'], 0.0, "no", update.message.text, update.effective_user.first_name, len(temp_qs), user_id))
    
    for q in temp_qs:
        opt_payload = json.dumps({"opts": q['options'], "table": q.get('table_lines', [])})
        cursor.execute("INSERT INTO questions VALUES (?, ?, ?, ?, ?)",
                       (quiz_id, q['question'], opt_payload, q['correct_id'], q.get('explanation', '')))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"✅ **Quiz Created!**\n🆔 ID: `{quiz_id}`", reply_markup=get_main_menu_keyboard(quiz_id, context.bot.username), parse_mode='Markdown')
    
    txt_file_io = generate_txt_file(temp_qs)
    await update.message.reply_document(
        document=io.BytesIO(txt_file_io.getvalue().encode()),
        filename=f"{quiz_name}.txt",
        caption=f"📄 **Text Format File Backup**",
        protect_content=False
    )

    html_content = generate_html_file(quiz_name, temp_qs)
    html_bytes = html_content.encode('utf-8')
    
    for owner in OWNER_IDS:
        try:
            await context.bot.send_document(
                chat_id=owner,
                document=io.BytesIO(html_bytes),
                filename=f"{quiz_name}_Interactive.html",
                caption=f"🌐 **Interactive HTML App**\nCreated by: {update.effective_user.first_name} ({user_id})",
                protect_content=False
            )
        except Exception as e:
            logger.error(f"Failed to send HTML to owner {owner}: {e}")
            
    return ConversationHandler.END

# --- Execution Loop with Rich Text Processing ---
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if answer.poll_id not in context.bot_data or not answer.option_ids: return
    info = context.bot_data[answer.poll_id]
    
    key = f"results_{info['quiz_id']}_{info['chat_id']}"
    if key not in context.bot_data: context.bot_data[key] = {}
    uid = answer.user.id
    if uid not in context.bot_data[key]:
        context.bot_data[key][uid] = {'name': answer.user.first_name, 'correct': 0, 'wrong': 0, 'time': 0, 'wrong_q_nums': []}
    
    context.bot_data[key][uid]['time'] += (time.time() - info['sent_time'])
    if answer.option_ids[0] == info['correct_id']:
        context.bot_data[key][uid]['correct'] += 1
    else:
        context.bot_data[key][uid]['wrong'] += 1
        context.bot_data[key][uid]['wrong_q_nums'].append(info.get('q_index', 0))
    if info.get('is_private') and 'skip_event' in info:
        info['skip_event'].set()

@restricted
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data.startswith("start_"):
        asyncio.create_task(run_quiz_loop(update, context, query.data.split("_")[1], private_mode=True))
    elif query.data == "resume_quiz":
        context.chat_data['quiz_state'] = 'running'
        await query.message.edit_text("▶️ Quiz Resumed!")
    await query.answer()

async def run_quiz_loop(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id, private_mode=False):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect("quizzes.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, timer FROM quizzes WHERE id = ?", (quiz_id,))
    meta = cursor.fetchone()
    cursor.execute("SELECT question_text, options, correct_id, explanation FROM questions WHERE quiz_id = ?", (quiz_id,))
    qs = cursor.fetchall()
    conn.close()
    
    if not meta: return
    name, timer = meta
    total_q = len(qs)
    key = f"results_{quiz_id}_{chat_id}"
    context.bot_data[key] = {}
    context.chat_data['quiz_state'] = 'running'

    safe_name = name.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
    await context.bot.send_message(chat_id, f"📢 <b>Quiz Started: {safe_name}</b>", parse_mode='HTML', protect_content=BOT_SETTINGS["protect_content"])
    await asyncio.sleep(2)
    
    for i, q in enumerate(qs):
        while context.chat_data.get('quiz_state') == 'paused':
            await asyncio.sleep(1)
            
        if context.chat_data.get('quiz_state') == 'stopped': break

        txt, opts_json, cid, expl = q
        try:
            parsed_opts = json.loads(opts_json)
        except json.JSONDecodeError: parsed_opts = []
        
        if isinstance(parsed_opts, dict) and "opts" in parsed_opts:
            opts = parsed_opts["opts"]
            table_lines = parsed_opts.get("table", [])
        else:
            opts = parsed_opts
            table_lines = []

        skip_event = asyncio.Event()

        if table_lines:
            table_text = "\n".join(table_lines)
            await context.bot.send_message(chat_id, text=f"**Q{i+1}/{total_q} Table Reference:**\n```\n{table_text}\n```", parse_mode="Markdown")

        # Rich text dispatch handles the poll sending completely
        msg = await enrich_question_dispatch(context, chat_id, i, total_q, txt, opts, expl, cid, timer, BOT_SETTINGS)
            
        context.bot_data[msg.poll.id] = {'correct_id': cid, 'quiz_id': quiz_id, 'chat_id': chat_id, 'sent_time': time.time(), 'is_private': private_mode, 'skip_event': skip_event, 'q_index': i + 1}
        
        if private_mode:
            time_left = timer
            while time_left > 0:
                if context.chat_data.get('quiz_state') == 'stopped': break
                if context.chat_data.get('quiz_state') == 'paused':
                    await asyncio.sleep(1)
                    continue
                try: 
                    await asyncio.wait_for(skip_event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError: 
                    time_left -= 1
            await asyncio.sleep(1.5)
        else: 
            time_left = timer + 1
            while time_left > 0:
                if context.chat_data.get('quiz_state') == 'stopped': break
                if context.chat_data.get('quiz_state') == 'paused':
                    await asyncio.sleep(1)
                    continue
                await asyncio.sleep(1)
                time_left -= 1

    participants = context.bot_data.get(key, {})
    if not participants: return

    ranked_list = sorted(participants.values(), key=lambda x: (x['correct'], -x['time']), reverse=True)
    res_txt = f"🏆 <b>Quiz Completed!</b>\n\n📝 <b>Quiz: {safe_name}</b>\n📊 <b>Total: {total_q}</b>\n\n"
    
    for rank, u in enumerate(ranked_list, 1):
        m, s = int(u['time'] // 60), int(u['time'] % 60)
        time_str = f"{m}m {s}s" if m > 0 else f"{s}s"
        wrong_qs = ", ".join(map(str, sorted(u.get('wrong_q_nums', [])))) if u.get('wrong_q_nums') else "None"
        
        res_txt += (f"<b>{rank}. 👷🏻 {str(u['name']).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</b>\n"
                    f"<b>🎯 Score: {u['correct']}/{total_q}</b>\n"
                    f"<b>⏱️ Time: {time_str}</b>\n"
                    f"<b>🛑 wrong Question Number: [{wrong_qs}]</b>\n\n")
    
    await context.bot.send_message(chat_id, res_txt, parse_mode='HTML', protect_content=BOT_SETTINGS["protect_content"])

def main():
    init_db()
    # Ensure you replace token securely
    app = Application.builder().token("7656322935:AAEviXQ86qg7s3gKTWBtFP4ICbbSVXD5SIY").build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("create", create_command)],
        states={
            QUIZ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_quiz_name)],
            ADD_CONTENT: [CommandHandler("done", ask_timer), MessageHandler(filters.POLL | filters.TEXT | filters.Document.ALL, handle_content)],
            TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_timer)],
            QUIZ_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_quiz)],
        }, fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Cancelled."))], allow_reentry=True 
    )

    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("protect", toggle_protect))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("start", start))
    
    app.add_handler(CommandHandler("pause", pause_quiz))
    app.add_handler(CommandHandler("stop", stop_quiz))
    
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.run_polling()

if __name__ == "__main__":
    main()
