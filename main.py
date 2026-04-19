import os, re, base64, asyncio, threading, json
from datetime import datetime, date
from io import BytesIO
import urllib.request
from flask import Flask, request as flask_request, jsonify
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from openai import OpenAI
from database import init_pool, init_db, register_user, get_user, deduct_credit, add_credits, log_clean, get_history, get_stats
from config import (
    BOT_TOKEN, OWNER_ID, OPENAI_API_KEY, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
    BOT_USERNAME, FREE_CLEANS, CREDIT_PACKS, SUPPORTED_EXTENSIONS, WELCOME_MESSAGE
)

try:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    STRIPE_OK = True
except Exception:
    STRIPE_OK = False

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
flask_app = Flask(__name__)

@flask_app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

app = None
loop = None

EXT_MAP = {
    "python": ".py", "javascript": ".js", "html": ".html",
    "css": ".css", "json": ".json", "typescript": ".ts",
    "yaml": ".yaml", "bash": ".sh", "text": ".txt"
}

def is_admin(uid): return uid == OWNER_ID
def uname(update): u = update.effective_user; return u.username or u.first_name or str(u.id)

def detect_language(code_text, filename=""):
    ext = os.path.splitext(filename)[1].lower() if filename else ""
    if ext == ".py" or any(k in code_text for k in ["def ", "import ", "print(", "elif ", "class ", "lambda "]): return "python"
    if ext == ".js" or any(k in code_text for k in ["function ", "const ", "let ", "var ", "console.log", "=>"]): return "javascript"
    if ext == ".ts": return "typescript"
    if ext == ".html" or any(k in code_text for k in ["<html", "<!DOCTYPE", "<div", "<body", "<head"]): return "html"
    if ext == ".css" or (code_text.count("{") > 2 and ":" in code_text and ";" in code_text and "<" not in code_text): return "css"
    if ext == ".json":
        try: json.loads(code_text); return "json"
        except Exception: pass
    if ext in (".yaml", ".yml"): return "yaml"
    if ext == ".sh" or code_text.startswith("#!"): return "bash"
    return "text"

def is_error_message(text):
    error_indicators = [
        "Traceback", "Error:", "Exception:", "SyntaxError", "TypeError",
        "ValueError", "NameError", "AttributeError", "ImportError",
        "IndentationError", "KeyError", "IndexError", "RuntimeError",
        "line ", "File \"", "undefined", "ReferenceError", "NullPointer",
        "Cannot read", "is not defined", "has no attribute"
    ]
    return any(indicator in text for indicator in error_indicators)

def basic_clean(code_text):
    fixes = []
    curly_map = {
        '\u201c': '"', '\u201d': '"',
        '\u2018': "'", '\u2019': "'",
        '\u00ab': '"', '\u00bb': '"',
    }
    curly_count = sum(code_text.count(c) for c in curly_map)
    if curly_count > 0:
        for c, s in curly_map.items():
            code_text = code_text.replace(c, s)
        fixes.append(f"{curly_count} curly quote(s) replaced")

    em_count = code_text.count('\u2014') + code_text.count('\u2013')
    if em_count > 0:
        code_text = code_text.replace('\u2014', '--').replace('\u2013', '-')
        fixes.append(f"{em_count} em dash(es) converted")

    nbsp_count = code_text.count('\u00a0')
    if nbsp_count > 0:
        code_text = code_text.replace('\u00a0', ' ')
        fixes.append(f"{nbsp_count} non-breaking space(s) removed")

    zwsp = ['\u200b', '\u200c', '\u200d', '\ufeff', '\u2060']
    zw_count = sum(code_text.count(z) for z in zwsp)
    if zw_count > 0:
        for z in zwsp: code_text = code_text.replace(z, '')
        fixes.append(f"{zw_count} invisible character(s) removed")

    lines = code_text.split('\n')
    new_lines = []; trail_count = 0
    for line in lines:
        stripped = line.rstrip()
        if stripped != line: trail_count += 1
        new_lines.append(stripped)
    code_text = '\n'.join(new_lines)
    if trail_count > 0:
        fixes.append(f"{trail_count} trailing whitespace(s) removed")

    lines = code_text.split('\n')
    new_lines = []; tab_count = 0
    for line in lines:
        if '\t' in line:
            line = line.replace('\t', '    ')
            tab_count += 1
        new_lines.append(line)
    code_text = '\n'.join(new_lines)
    if tab_count > 0:
        fixes.append(f"{tab_count} tab(s) converted to 4 spaces")

    if code_text.startswith('\ufeff'):
        code_text = code_text[1:]
        fixes.append("BOM character removed")

    return code_text, fixes

def get_preview(original, cleaned, max_lines=3):
    orig_lines = original.split('\n')
    clean_lines = cleaned.split('\n')
    diffs = []
    for i, (o, c) in enumerate(zip(orig_lines, clean_lines)):
        if o != c:
            diffs.append((o.strip(), c.strip()))
        if len(diffs) >= max_lines:
            break
    return diffs

async def ai_syntax_repair(code_text, language):
    if not openai_client: return code_text, [], "AI offline"
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"""You are a code repair specialist.
Fix ONLY syntax errors in the {language} code.
Do NOT change logic, variable names, or structure.
Return JSON with exactly:
- fixed_code: repaired code string
- ai_fixes: array of fix descriptions
- notes: brief explanation
If no errors found return original code with empty ai_fixes.
Respond ONLY with valid JSON. No markdown. No backticks."""},
                {"role": "user", "content": f"Repair this {language} code:\n\n{code_text[:8000]}"}
            ],
            max_tokens=4000, temperature=0
        )
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result.get("fixed_code", code_text), result.get("ai_fixes", []), result.get("notes", "")
    except Exception as e:
        print(f"AI repair error: {e}")
        return code_text, [], "AI repair skipped"

async def ai_diagnose_error(error_text):
    if not openai_client: return "AI offline. Cannot diagnose error."
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": """You are an expert debugging assistant.
Analyze the error or traceback provided.
Return JSON with:
- language: detected language
- error_type: name of the error
- cause: plain English explanation of why this happened
- root_cause_line: the specific line causing the issue if identifiable
- fix: exact fix to apply
- example: corrected code example (short)
Respond ONLY with valid JSON. No markdown. No backticks."""},
                {"role": "user", "content": f"Diagnose this error:\n\n{error_text[:4000]}"}
            ],
            max_tokens=1000, temperature=0
        )
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        language = result.get("language", "Unknown")
        error_type = result.get("error_type", "Unknown Error")
        cause = result.get("cause", "")
        root_line = result.get("root_cause_line", "")
        fix = result.get("fix", "")
        example = result.get("example", "")
        report = (
            f"ERROR DIAGNOSED 🔍\n\n"
            f"Language:    {language}\n"
            f"Error Type:  {error_type}\n\n"
            f"CAUSE:\n{cause}\n"
        )
        if root_line:
            report += f"\nROOT CAUSE LINE:\n{root_line}\n"
        if fix:
            report += f"\nFIX:\n{fix}\n"
        if example:
            report += f"\nEXAMPLE:\n{example}\n"
        report += "\n🧹 Cleaned with @CodeCleanAI_bot"
        return report
    except Exception as e:
        return f"Could not diagnose error: {str(e)}"

async def ai_explain_code(code_text, language):
    if not openai_client: return "AI offline. Cannot explain code."
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": """You are a friendly coding teacher.
Explain code in simple plain English.
Return JSON with:
- summary: one sentence what this code does
- breakdown: step by step explanation
- potential_bugs: list of possible issues
- improvements: list of suggestions
Respond ONLY with valid JSON. No markdown. No backticks."""},
                {"role": "user", "content": f"Explain this {language} code:\n\n{code_text[:4000]}"}
            ],
            max_tokens=1000, temperature=0
        )
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        summary = result.get("summary", "")
        breakdown = result.get("breakdown", "")
        bugs = result.get("potential_bugs", [])
        improvements = result.get("improvements", [])
        report = f"CODE EXPLAINED 📖\n\nLanguage: {language.title()}\n\nSUMMARY:\n{summary}\n\nBREAKDOWN:\n{breakdown}\n"
        if bugs:
            report += f"\nPOTENTIAL BUGS:\n" + "\n".join([f"  • {b}" for b in bugs])
        if improvements:
            report += f"\n\nIMPROVEMENTS:\n" + "\n".join([f"  • {i}" for i in improvements])
        report += "\n\n🧹 Cleaned with @CodeCleanAI_bot"
        return report
    except Exception as e:
        return f"Could not explain code: {str(e)}"

async def ocr_screenshot(image_data):
    if not openai_client: return None, "AI offline"
    try:
        b64 = base64.b64encode(image_data).decode()
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": """You are a code OCR specialist.
Extract ALL code from this image exactly as written.
Return JSON with:
- code: extracted code string
- language: detected programming language
- confidence: percentage 0-100
- ocr_notes: any extraction issues
Respond ONLY with valid JSON. No markdown. No backticks."""},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Extract all code from this screenshot."}
                ]}
            ],
            max_tokens=4000
        )
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result, None
    except Exception as e:
        return None, f"OCR failed: {str(e)}"

def create_checkout_session(uid, username, pack_key):
    if not STRIPE_OK: return None
    pack = CREDIT_PACKS.get(pack_key)
    if not pack: return None
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price_data": {
                "currency": "usd",
                "product_data": {"name": f"CodeClean — {pack['label']}"},
                "unit_amount": pack['price'] * 100
            }, "quantity": 1}],
            mode="payment",
            success_url=f"https://t.me/{BOT_USERNAME}",
            cancel_url=f"https://t.me/{BOT_USERNAME}",
            metadata={
                "telegram_id": str(uid),
                "username": username or "",
                "pack": pack_key,
                "credits": str(pack['credits'])
            }
        )
        from database import get_db, release_db
        conn = get_db(); cur = conn.cursor()
        try:
            cur.execute("INSERT INTO stripe_sessions (telegram_id, session_id, pack, credits, amount) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (uid, session.id, pack_key, pack['credits'], pack['price']))
            conn.commit()
        finally:
            release_db(conn)
        return session.url
    except Exception as e:
        print(f"Stripe error: {e}"); return None

async def process_and_deliver(uid, username, code_text, language, input_method, filename, update, context):
    register_user(uid, username)
    user = get_user(uid)
    if not user:
        await update.message.reply_text("Send /start first.")
        return

    credits = 999 if uid == OWNER_ID else (user[2] if user else 0)

    if credits <= 0:
        kb = []
        for key, pack in CREDIT_PACKS.items():
            url = create_checkout_session(uid, username, key)
            if url:
                kb.append([InlineKeyboardButton(f"{pack['label']} ${pack['price']}", url=url)])
        await update.message.reply_text(
            "NO CREDITS REMAINING\n\nGet more credits to continue cleaning.",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        return

    processing_msg = await update.message.reply_text("🧹 Cleaning your code...")

    original_text = code_text
    cleaned, basic_fixes = basic_clean(code_text)
    ai_fixed, ai_fixes, ai_notes = await ai_syntax_repair(cleaned, language)
    all_fixes = basic_fixes + ai_fixes
    total_issues = len(all_fixes)

    if uid != OWNER_ID:
        deduct_credit(uid)
    user_after = get_user(uid)
    credits_remaining = 999 if uid == OWNER_ID else (user_after[2] if user_after else 0)

    previews = get_preview(original_text, ai_fixed)
    preview_text = ""
    if previews:
        preview_text = "\n\nPREVIEW:\n"
        for before, after in previews:
            if before and after and before != after:
                preview_text += f"Before: {before[:40]}\nAfter:  {after[:40]}\n\n"

    fix_lines = "\n".join([f"  • {f}" for f in all_fixes]) if all_fixes else "  • No issues found — code was already clean"
    ai_note_text = f"\n\nAI NOTES:\n{ai_notes}" if ai_notes and ai_fixes else ""

    report = (
        f"FILE CLEANED ✅\n\n"
        f"Language:     {language.title()}\n"
        f"Input:        {input_method}\n"
        f"Issues fixed: {total_issues}\n\n"
        f"FIXES APPLIED:\n{fix_lines}"
        f"{ai_note_text}"
        f"{preview_text}\n"
        f"Credits remaining: {credits_remaining}\n\n"
        f"🧹 Cleaned with @CodeCleanAI_bot"
    )

    log_clean(uid, language, input_method, total_issues, report)

    ext = EXT_MAP.get(language, ".txt")
    base = os.path.splitext(filename)[0] if filename else "clean_code"
    if any(x in base for x in ["pasted", "screenshot", "github", "clean_code"]):
        clean_filename = f"clean_code{ext}"
    else:
        clean_filename = f"clean_{base}{ext}"

    clean_buf = BytesIO(ai_fixed.encode('utf-8'))
    clean_buf.name = clean_filename

    try:
        await processing_msg.delete()
    except Exception:
        pass

    await update.message.reply_document(
        document=clean_buf,
        filename=clean_filename,
        caption=report
    )

    if 0 < credits_remaining <= 2 and uid != OWNER_ID:
        kb = []
        for key, pack in CREDIT_PACKS.items():
            url = create_checkout_session(uid, username, key)
            if url:
                kb.append([InlineKeyboardButton(f"{pack['label']} ${pack['price']}", url=url)])
        await update.message.reply_text(
            f"⚠️ Only {credits_remaining} credit(s) left.",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    register_user(uid, name)
    user = get_user(uid)
    credits = 999 if uid == OWNER_ID else (user[2] if user else FREE_CLEANS)
    label = "Unlimited (Admin)" if uid == OWNER_ID else str(credits)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
    kb = []
    for key, pack in CREDIT_PACKS.items():
        url = create_checkout_session(uid, name, key)
        if url:
            kb.append([InlineKeyboardButton(f"{pack['label']} ${pack['price']}", url=url)])
    kb.append([InlineKeyboardButton("My Credits", callback_data="credits")])
    kb.append([InlineKeyboardButton("Invite a Developer — Earn Credits", url=f"https://t.me/share/url?url={ref_link}&text=Fix+broken+code+instantly")])
    await update.message.reply_text(
        f"{WELCOME_MESSAGE}\n\nYour credits: {label}",
        reply_markup=InlineKeyboardMarkup(kb))

async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if not user:
        await update.message.reply_text("Send /start first.")
        return
    credits = 999 if uid == OWNER_ID else user[2]
    total_cleans = user[4]
    label = "Unlimited (Admin)" if uid == OWNER_ID else str(credits)
    kb = []
    for key, pack in CREDIT_PACKS.items():
        url = create_checkout_session(uid, str(uid), key)
        if url:
            kb.append([InlineKeyboardButton(f"{pack['label']} ${pack['price']}", url=url)])
    await update.message.reply_text(
        f"YOUR CREDITS\n\nCredits remaining: {label}\nTotal cleans done: {total_cleans}",
        reply_markup=InlineKeyboardMarkup(kb))

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    kb = []
    for key, pack in CREDIT_PACKS.items():
        url = create_checkout_session(uid, name, key)
        if url:
            kb.append([InlineKeyboardButton(f"{pack['label']} ${pack['price']}", url=url)])
    await update.message.reply_text(
        "BUY CREDITS\n\nChoose your pack:\n\nAll plans include AI syntax repair and OCR screenshot support.",
        reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    register_user(uid, name)
    from database import get_db, release_db
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT last_daily FROM users WHERE telegram_id=%s", (uid,))
        row = cur.fetchone()
        last = row[0] if row and row[0] else None
        today = date.today()
        if last and last >= today:
            await update.message.reply_text("DAILY BONUS\n\nAlready claimed today.\n\nCome back tomorrow for +1 free credit.")
            return
        cur.execute("UPDATE users SET credits=credits+1, last_daily=%s WHERE telegram_id=%s", (today, uid))
        conn.commit()
    finally:
        release_db(conn)
    await update.message.reply_text("DAILY BONUS ✅\n\n+1 free credit added.\n\nCome back tomorrow for another.\n\n🧹 @CodeCleanAI_bot")

async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    from database import get_db, release_db
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT referral_count FROM users WHERE telegram_id=%s", (uid,))
        row = cur.fetchone()
        referral_count = row[0] if row and row[0] else 0
    finally:
        release_db(conn)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
    await update.message.reply_text(
        f"REFERRAL SYSTEM\n\nYour invite link:\n{ref_link}\n\nReferrals: {referral_count}\n\nEach developer who signs up via your link earns you +2 credits.\n\nShare with developers who copy code from iPhone or ChatGPT.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Share Link", url=f"https://t.me/share/url?url={ref_link}&text=Fix+broken+code+instantly+%40CodeCleanAI_bot")
        ]]))

async def cmd_explain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    register_user(uid, name)
    user = get_user(uid)
    credits = 999 if uid == OWNER_ID else (user[2] if user else 0)
    if credits <= 0:
        await update.message.reply_text("No credits remaining. Use /buy to get more.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /explain <paste your code here>\n\nOr reply to a message with /explain")
        return
    code_text = " ".join(args)
    language = detect_language(code_text)
    processing_msg = await update.message.reply_text("📖 Explaining your code...")
    explanation = await ai_explain_code(code_text, language)
    if uid != OWNER_ID:
        deduct_credit(uid)
    try:
        await processing_msg.delete()
    except Exception:
        pass
    await update.message.reply_text(explanation)

async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /grant <uid> <credits>")
        return
    fan_id = int(args[0]); credits = int(args[1])
    add_credits(fan_id, credits)
    await update.message.reply_text(f"Granted {credits} credits to {fan_id}")
    try:
        await context.bot.send_message(fan_id,
            f"CREDITS ADDED ✅\n\n{credits} cleans added to your account.\n\n🧹 CodeClean Bot")
    except Exception:
        pass

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    history = get_history(uid)
    if not history:
        await update.message.reply_text("No cleans yet.\n\nSend a file, paste code, or send a screenshot to start.")
        return
    text = "YOUR CLEAN HISTORY\n\n"
    for file_type, input_method, issues, cleaned_at in history:
        text += f"{cleaned_at.strftime('%d %b %H:%M')} — {file_type} ({input_method}) — {issues} fixes\n"
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CODECLEAN BOT — HELP\n\n"
        "HOW TO USE:\n\n"
        "1. Paste code directly\n"
        "2. Send a code file (.py .js .html etc)\n"
        "3. Send a screenshot of code\n"
        "4. Send a GitHub file link\n"
        "5. Paste an error message\n"
        "6. Use /explain to understand code\n\n"
        "COMMANDS:\n"
        "/start — start and check credits\n"
        "/credits — check balance\n"
        "/buy — purchase credits\n"
        "/daily — claim daily free credit\n"
        "/refer — get your referral link\n"
        "/explain — explain what code does\n"
        "/history — recent clean history\n"
        "/help — this message\n\n"
        "CREDITS:\n"
        f"• Free tier: {FREE_CLEANS} cleans\n"
        "• Text/file clean: 1 credit\n"
        "• Screenshot OCR: 2 credits\n"
        "• /explain: 1 credit\n\n"
        "🧹 @CodeCleanAI_bot")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    total_users, total_cleans, total_sales, pro_users = get_stats()
    await update.message.reply_text(
        f"CODECLEAN STATS\n\n"
        f"Total users:  {total_users:,}\n"
        f"Total cleans: {total_cleans:,}\n"
        f"Total sales:  {total_sales}\n"
        f"Pro users:    {pro_users}")

async def credits_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    user = get_user(uid)
    credits = 999 if uid == OWNER_ID else (user[2] if user else 0)
    total = user[4] if user else 0
    label = "Unlimited (Admin)" if uid == OWNER_ID else str(credits)
    await q.message.reply_text(f"YOUR CREDITS\n\nCredits: {label}\nTotal cleans: {total}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    register_user(uid, name)
    doc = update.message.document
    if not doc: return
    filename = doc.file_name or "file.txt"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        await update.message.reply_text(f"Unsupported file type: {ext}\n\nSupported: {', '.join(SUPPORTED_EXTENSIONS)}")
        return
    try:
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        code_text = buf.getvalue().decode('utf-8', errors='replace')
    except Exception as e:
        await update.message.reply_text(f"Could not read file: {str(e)}")
        return
    language = detect_language(code_text, filename)
    await update.message.reply_text(f"File received: {filename}\nLanguage: {language.title()}\nCleaning now...")
    await process_and_deliver(uid, name, code_text, language, "file upload", filename, update, context)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    register_user(uid, name)
    user = get_user(uid)
    credits = 999 if uid == OWNER_ID else (user[2] if user else 0)
    if credits < 2:
        kb = []
        for key, pack in CREDIT_PACKS.items():
            url = create_checkout_session(uid, name, key)
            if url:
                kb.append([InlineKeyboardButton(f"{pack['label']} ${pack['price']}", url=url)])
        await update.message.reply_text(
            "Screenshot OCR uses 2 credits.\n\nGet more credits:",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        return
    processing_msg = await update.message.reply_text("👁 Reading code from screenshot...")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    image_data = buf.getvalue()
    result, error = await ocr_screenshot(image_data)
    try: await processing_msg.delete()
    except Exception: pass
    if error or not result:
        await update.message.reply_text(f"Could not read screenshot.\n\n{error or 'Try a clearer image.'}")
        return
    code_text = result.get("code", "")
    language = result.get("language", "text")
    confidence = result.get("confidence", 0)
    if not code_text.strip():
        await update.message.reply_text("No code found in screenshot. Try a clearer image.")
        return
    await update.message.reply_text(f"CODE EXTRACTED\n\nLanguage: {language.title()}\nConfidence: {confidence}%\nCleaning now...")
    if uid != OWNER_ID:
        deduct_credit(uid)
        deduct_credit(uid)
    await process_and_deliver(uid, name, code_text, language, "screenshot OCR", "screenshot", update, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    text = update.message.text or ""
    if text.startswith("/"): return
    register_user(uid, name)

    args = context.args if hasattr(context, 'args') and context.args else []
    if args and args[0].startswith("ref"):
        referrer_id = int(args[0][3:])
        if referrer_id != uid:
            from database import get_db, release_db
            conn = get_db(); cur = conn.cursor()
            try:
                cur.execute("SELECT telegram_id FROM users WHERE telegram_id=%s", (uid,))
                if not cur.fetchone():
                    cur.execute("UPDATE users SET credits=credits+2, referral_count=referral_count+1 WHERE telegram_id=%s", (referrer_id,))
                    conn.commit()
                    try:
                        await context.bot.send_message(referrer_id, "REFERRAL BONUS\n\n+2 credits added.\n\nA developer joined via your link.\n\n🧹 @CodeCleanAI_bot")
                    except Exception: pass
            finally:
                release_db(conn)

    if "github.com" in text and "/blob/" in text:
        raw_url = text.strip().replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        try:
            await update.message.reply_text("Downloading from GitHub...")
            with urllib.request.urlopen(raw_url) as r:
                code_text = r.read().decode('utf-8', errors='replace')
            filename = raw_url.split("/")[-1]
            language = detect_language(code_text, filename)
            await update.message.reply_text(f"FILE DOWNLOADED\n\nFile: {filename}\nLanguage: {language.title()}\nLines: {len(code_text.splitlines())}\n\nCleaning now...")
            await process_and_deliver(uid, name, code_text, language, "GitHub link", filename, update, context)
        except Exception as e:
            await update.message.reply_text(f"Could not download GitHub file.\n\n{str(e)}")
        return

    if is_error_message(text):
        user = get_user(uid)
        credits = 999 if uid == OWNER_ID else (user[2] if user else 0)
        if credits <= 0:
            kb = []
            for key, pack in CREDIT_PACKS.items():
                url = create_checkout_session(uid, name, key)
                if url:
                    kb.append([InlineKeyboardButton(f"{pack['label']} ${pack['price']}", url=url)])
            await update.message.reply_text("No credits remaining.", reply_markup=InlineKeyboardMarkup(kb) if kb else None)
            return
        processing_msg = await update.message.reply_text("🔍 Diagnosing error...")
        diagnosis = await ai_diagnose_error(text)
        if uid != OWNER_ID:
            deduct_credit(uid)
        try: await processing_msg.delete()
        except Exception: pass
        await update.message.reply_text(diagnosis)
        return

    code_indicators = ["def ", "function ", "import ", "const ", "var ", "let ", "<html",
                       "class ", "if (", "for (", "while (", "print(", "console.log",
                       "<?php", "SELECT ", "CREATE ", "#!/"]
    is_code = any(i in text for i in code_indicators) or len(text.split('\n')) > 3
    if not is_code:
        await update.message.reply_text(
            "Send me code to clean.\n\n"
            "• Paste code directly\n"
            "• Send a .py .js .html file\n"
            "• Send a screenshot of code\n"
            "• Send a GitHub file link\n"
            "• Paste an error message\n\n"
            "/daily — claim free daily credit\n"
            "/refer — earn credits by inviting developers\n"
            "/help — full guide")
        return
    language = detect_language(text)
    ext = EXT_MAP.get(language, ".txt")
    await process_and_deliver(uid, name, text, language, "pasted text", f"clean_code{ext}", update, context)
@flask_app.route("/")
def health():
    return jsonify({"status": "ONLINE", "product": "CodeClean Bot", "version": "3.0"})

@flask_app.route("/stripe_webhook", methods=["POST"])
def stripe_webhook():
    payload = flask_request.data
    sig_header = flask_request.headers.get("Stripe-Signature")
    if not STRIPE_OK: return "stripe not available", 400
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return str(e), 400
    if event["type"] == "checkout.session.completed":
        asyncio.run_coroutine_threadsafe(
            handle_stripe_payment(event["data"]["object"]), loop)
    return "ok"

async def handle_stripe_payment(session_data):
    uid = int(session_data.get("metadata", {}).get("telegram_id", 0))
    pack_key = session_data.get("metadata", {}).get("pack", "")
    session_id = session_data.get("id", "")
    if not uid: return
    from database import get_db, release_db
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE stripe_sessions SET status='completed' WHERE session_id=%s", (session_id,))
        conn.commit()
    finally:
        release_db(conn)
    pack = CREDIT_PACKS.get(pack_key)
    if not pack:
        pack = {"credits": 15, "label": "Starter — 15 cleans"}
    credits = pack["credits"]
    add_credits(uid, credits)
    try:
        await app.bot.send_message(uid,
            f"CREDITS DELIVERED ✅\n\n"
            f"Pack: {pack['label']}\n"
            f"Credits added: {credits}\n\n"
            f"Send any code file, paste code, or send a screenshot to start cleaning.\n\n"
            f"🧹 CodeClean Bot — Fix broken code instantly.")
    except Exception as e:
        print(f"Credit delivery error: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

async def post_init(application):
    global app, loop
    app = application
    loop = asyncio.get_event_loop()

def main():
    init_pool()
    init_db()
    print("=" * 50)
    print("CODECLEAN BOT v3.0")
    print("AI Code Cleaner + OCR + Debug + Referral")
    print("Status: ONLINE")
    print("=" * 50)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"Webhook server on port {os.environ.get('PORT', 8080)}")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("credits", cmd_credits))
    application.add_handler(CommandHandler("buy", cmd_buy))
    application.add_handler(CommandHandler("daily", cmd_daily))
    application.add_handler(CommandHandler("refer", cmd_refer))
    application.add_handler(CommandHandler("explain", cmd_explain))
    application.add_handler(CommandHandler("grant", cmd_grant))
    application.add_handler(CommandHandler("history", cmd_history))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("stats", cmd_stats))

    application.add_handler(CallbackQueryHandler(credits_cb, pattern="^credits"))

    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
