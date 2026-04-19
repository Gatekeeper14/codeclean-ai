import os, re, base64, asyncio, threading, json, io
from datetime import datetime
from io import BytesIO
from flask import Flask, request as flask_request, jsonify
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
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

def is_admin(uid): return uid == OWNER_ID
def uname(update): u = update.effective_user; return u.username or u.first_name or str(u.id)

def detect_language(code_text, filename=""):
    ext = os.path.splitext(filename)[1].lower() if filename else ""
    if ext == ".py" or "def " in code_text or "import " in code_text: return "python"
    if ext == ".js" or "function " in code_text or "const " in code_text: return "javascript"
    if ext == ".html" or "<html" in code_text or "<!DOCTYPE" in code_text: return "html"
    if ext == ".css" or "{" in code_text and ":" in code_text and ";" in code_text: return "css"
    if ext == ".json":
        try: json.loads(code_text); return "json"
        except Exception: return "json"
    if ext in (".yaml", ".yml"): return "yaml"
    if ext == ".ts": return "typescript"
    if ext == ".sh": return "bash"
    return "text"

def basic_clean(code_text):
    fixes = []
    original = code_text

    curly_open = ['\u201c', '\u2018', '\u2019', '\u201d']
    straight = ['"', "'", "'", '"']
    curly_count = 0
    for c, s in zip(curly_open, straight):
        count = code_text.count(c)
        if count > 0:
            code_text = code_text.replace(c, s)
            curly_count += count
    if curly_count > 0:
        fixes.append(f"{curly_count} curly quote(s) replaced with straight quotes")

    em_count = code_text.count('\u2014') + code_text.count('\u2013')
    if em_count > 0:
        code_text = code_text.replace('\u2014', '--').replace('\u2013', '-')
        fixes.append(f"{em_count} em dash(es) converted")

    nbsp_count = code_text.count('\u00a0')
    if nbsp_count > 0:
        code_text = code_text.replace('\u00a0', ' ')
        fixes.append(f"{nbsp_count} non-breaking space(s) removed")

    zwsp = ['\u200b', '\u200c', '\u200d', '\ufeff']
    zw_count = sum(code_text.count(z) for z in zwsp)
    if zw_count > 0:
        for z in zwsp: code_text = code_text.replace(z, '')
        fixes.append(f"{zw_count} zero-width character(s) removed")

    lines = code_text.split('\n')
    new_lines = []
    trail_count = 0
    for line in lines:
        stripped = line.rstrip()
        if stripped != line: trail_count += 1
        new_lines.append(stripped)
    code_text = '\n'.join(new_lines)
    if trail_count > 0:
        fixes.append(f"{trail_count} line(s) trailing whitespace removed")

    lines = code_text.split('\n')
    new_lines = []
    tab_count = 0
    for line in lines:
        if '\t' in line:
            line = line.replace('\t', '    ')
            tab_count += 1
        new_lines.append(line)
    code_text = '\n'.join(new_lines)
    if tab_count > 0:
        fixes.append(f"{tab_count} tab(s) converted to 4 spaces")

    return code_text, fixes

async def ai_syntax_repair(code_text, language):
    if not openai_client: return code_text, [], "AI offline — basic clean only"
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"""You are a code repair specialist. 
Fix ONLY syntax errors in the {language} code provided.
Do NOT change logic, variable names, or structure.
Return a JSON object with exactly these keys:
- fixed_code: the repaired code as a string
- ai_fixes: array of strings describing each fix made
- notes: brief explanation of what was wrong
If no syntax errors found, return the original code unchanged with empty ai_fixes array.
Respond ONLY with valid JSON. No markdown. No backticks."""},
                {"role": "user", "content": f"Repair this {language} code:\n\n{code_text[:8000]}"}
            ],
            max_tokens=4000,
            temperature=0
        )
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        fixed = result.get("fixed_code", code_text)
        ai_fixes = result.get("ai_fixes", [])
        notes = result.get("notes", "")
        return fixed, ai_fixes, notes
    except Exception as e:
        print(f"AI repair error: {e}")
        return code_text, [], "AI repair skipped"

async def ocr_screenshot(image_data):
    if not openai_client: return None, "AI offline — cannot read screenshot"
    try:
        b64 = base64.b64encode(image_data).decode()
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": """You are a code OCR specialist.
Extract ALL code from this image exactly as written.
Return a JSON object with:
- code: the extracted code as a string
- language: detected programming language
- confidence: percentage 0-100
- ocr_notes: any issues with extraction
Respond ONLY with valid JSON. No markdown. No backticks."""},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Extract the code from this screenshot."}
                ]}
            ],
            max_tokens=4000
        )
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        code = result.get("code", "")
        language = result.get("language", "text")
        confidence = result.get("confidence", 0)
        notes = result.get("ocr_notes", "")
        return {"code": code, "language": language, "confidence": confidence, "notes": notes}, None
    except Exception as e:
        print(f"OCR error: {e}")
        return None, f"OCR failed: {str(e)}"

def create_checkout(uid, username, pack_key):
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
            metadata={"telegram_id": str(uid), "username": username or "", "pack": pack_key, "credits": str(pack['credits'])}
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
    user = get_user(uid)
    if not user:
        await update.message.reply_text("Please send /start first.")
        return
    credits = user[2]
    if credits <= 0:
        await update.message.reply_text(
            "NO CREDITS REMAINING\n\nYour free cleans are used up.\n\nGet more credits to continue.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Starter $5 — 15 cleans", callback_data="buy:starter")],
                [InlineKeyboardButton("Pro $20 — 75 cleans", callback_data="buy:pro")],
                [InlineKeyboardButton("Elite $50 — 200 cleans", callback_data="buy:elite")],
            ]))
        return

    processing_msg = await update.message.reply_text("🧹 Cleaning your code...")

    cleaned, basic_fixes = basic_clean(code_text)
    ai_fixed, ai_fixes, ai_notes = await ai_syntax_repair(cleaned, language)
    all_fixes = basic_fixes + ai_fixes
    total_issues = len(all_fixes)

    deduct_credit(uid)
    user_after = get_user(uid)
    credits_remaining = user_after[2] if user_after else 0

    fix_lines = "\n".join([f"  • {f}" for f in all_fixes]) if all_fixes else "  • No issues found — code was clean"
    ai_note_text = f"\nAI NOTES:\n{ai_notes}" if ai_notes and ai_fixes else ""
    report = (
        f"FILE CLEANED ✅\n\n"
        f"Language:     {language.title()}\n"
        f"Input:        {input_method}\n"
        f"Issues fixed: {total_issues}\n\n"
        f"FIXES APPLIED:\n{fix_lines}"
        f"{ai_note_text}\n\n"
        f"Credits remaining: {credits_remaining}"
    )

    log_clean(uid, language, input_method, total_issues, report)

    ext_map = {"python": ".py", "javascript": ".js", "html": ".html", "css": ".css",
               "json": ".json", "typescript": ".ts", "yaml": ".yaml", "bash": ".sh", "text": ".txt"}
    ext = ext_map.get(language, ".txt")
    clean_filename = f"clean_{filename}" if filename else f"cleaned_code{ext}"

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

    if credits_remaining <= 2 and credits_remaining > 0:
        await update.message.reply_text(
            f"⚠️ Low credits — {credits_remaining} remaining.\n\nTop up to keep cleaning.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Buy Credits", callback_data="buy:starter")]
            ]))

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    is_new = register_user(uid, name)
    user = get_user(uid)
    credits = user[2] if user else FREE_CLEANS
    await update.message.reply_text(
        f"{WELCOME_MESSAGE}\n\nYour credits: {credits}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Check Credits", callback_data="credits")],
            [InlineKeyboardButton("Buy Credits", callback_data="buy:starter")],
        ]))

async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if not user:
        await update.message.reply_text("Send /start first.")
        return
    credits = user[2]
    total_cleans = user[4]
    await update.message.reply_text(
        f"YOUR CREDITS\n\nCredits remaining: {credits}\nTotal cleans done: {total_cleans}\n\nGet more credits below.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Starter $5 — 15 cleans", callback_data="buy:starter")],
            [InlineKeyboardButton("Pro $20 — 75 cleans", callback_data="buy:pro")],
            [InlineKeyboardButton("Elite $50 — 200 cleans", callback_data="buy:elite")],
        ]))

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BUY CREDITS\n\nChoose your pack:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Starter $5 — 15 cleans", callback_data="buy:starter")],
            [InlineKeyboardButton("Pro $20 — 75 cleans", callback_data="buy:pro")],
            [InlineKeyboardButton("Elite $50 — 200 cleans", callback_data="buy:elite")],
        ]))

async def buy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    name = q.from_user.username or str(uid)
    pack_key = q.data.split(":")[1]
    pack = CREDIT_PACKS.get(pack_key)
    if not pack: return
    url = create_checkout(uid, name, pack_key)
    if url:
        await q.message.reply_text(
            f"CHECKOUT\n\n{pack['label']}\nPrice: ${pack['price']}\n\nCredits delivered instantly after payment.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Pay ${pack['price']}", url=url)]]))
    else:
        await q.message.reply_text("Payment system offline. Try again later.")

async def credits_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    user = get_user(uid)
    credits = user[2] if user else 0
    total = user[4] if user else 0
    await q.message.reply_text(f"YOUR CREDITS\n\nCredits: {credits}\nTotal cleans: {total}")

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    history = get_history(uid)
    if not history:
        await update.message.reply_text("No cleans yet. Send a file or screenshot to get started.")
        return
    text = "YOUR CLEAN HISTORY\n\n"
    for file_type, input_method, issues, cleaned_at in history:
        text += f"{cleaned_at.strftime('%d %b %H:%M')} — {file_type} ({input_method}) — {issues} fixes\n"
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CODECLEAN BOT — HELP\n\n"
        "HOW TO USE:\n\n"
        "1. Send any code file (.py .js .html etc)\n"
        "2. Or paste code directly as a message\n"
        "3. Or send a SCREENSHOT of code\n\n"
        "Bot will:\n"
        "• Fix curly quotes and smart apostrophes\n"
        "• Fix indentation and tab issues\n"
        "• Repair syntax errors with AI\n"
        "• Read code from screenshots via OCR\n"
        "• Send back a clean working file\n\n"
        "CREDITS:\n"
        f"• Free tier: {FREE_CLEANS} cleans\n"
        "• Text/file clean: 1 credit\n"
        "• Screenshot OCR clean: 2 credits\n\n"
        "/credits — check balance\n"
        "/buy — purchase credits\n"
        "/history — your clean history")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    total_users, total_cleans, total_sales, pro_users = get_stats()
    await update.message.reply_text(
        f"CODECLEAN STATS\n\n"
        f"Total users:  {total_users:,}\n"
        f"Total cleans: {total_cleans:,}\n"
        f"Total sales:  {total_sales}\n"
        f"Pro users:    {pro_users}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    register_user(uid, name)
    doc = update.message.document
    if not doc: return
    filename = doc.file_name or "file.txt"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        await update.message.reply_text(
            f"Unsupported file type: {ext}\n\nSupported: {', '.join(SUPPORTED_EXTENSIONS)}")
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
    await process_and_deliver(uid, name, code_text, language, "file", filename, update, context)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    register_user(uid, name)
    user = get_user(uid)
    credits = user[2] if user else 0
    if credits < 2:
        await update.message.reply_text(
            "Screenshot OCR uses 2 credits.\n\nYou need at least 2 credits.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Buy Credits", callback_data="buy:starter")]]))
        return
    processing_msg = await update.message.reply_text("👁 Reading code from screenshot...")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    image_data = buf.getvalue()
    result, error = await ocr_screenshot(image_data)
    if error or not result:
        try: await processing_msg.delete()
        except Exception: pass
        await update.message.reply_text(f"Could not read screenshot.\n\n{error or 'Try a clearer image.'}")
        return
    code_text = result.get("code", "")
    language = result.get("language", "text")
    confidence = result.get("confidence", 0)
    ocr_notes = result.get("notes", "")
    if not code_text.strip():
        try: await processing_msg.delete()
        except Exception: pass
        await update.message.reply_text("No code found in screenshot. Try a clearer image.")
        return
    try: await processing_msg.delete()
    except Exception: pass
    ocr_info = f"Screenshot OCR — {confidence}% confidence"
    if ocr_notes: ocr_info += f"\nOCR Notes: {ocr_notes}"
    await update.message.reply_text(f"CODE EXTRACTED\n\n{ocr_info}\nLanguage: {language.title()}\nLines: {len(code_text.splitlines())}\n\nCleaning now...")
    deduct_credit(uid)
    await process_and_deliver(uid, name, code_text, language, "screenshot OCR", f"from_screenshot.{language[:2]}", update, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = uname(update)
    text = update.message.text or ""
    if text.startswith("/"): return
    register_user(uid, name)
    code_indicators = ["def ", "function ", "import ", "const ", "var ", "let ", "<html", "<?php",
                       "class ", "if (", "for (", "while (", "{", "}", "=>", "->", "print(", "console.log"]
    is_code = any(indicator in text for indicator in code_indicators) or len(text.split('\n')) > 3
    if not is_code:
        await update.message.reply_text(
            "Send me a code file, paste code directly, or send a screenshot of code.\n\n"
            "/help — see all options\n/credits — check your balance")
        return
    language = detect_language(text)
    await process_and_deliver(uid, name, text, language, "pasted text", f"pasted.{language[:2]}", update, context)
@flask_app.route("/")
def health():
    return jsonify({"status": "ONLINE", "product": "CodeClean Bot", "version": "1.0"})

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
    username = session_data.get("metadata", {}).get("username", "user")
    if not uid or not pack_key: return
    from database import get_db, release_db
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE stripe_sessions SET status='completed' WHERE session_id=%s", (session_id,))
        conn.commit()
    finally:
        release_db(conn)
    pack = CREDIT_PACKS.get(pack_key)
    if not pack: return
    credits = pack["credits"]
    add_credits(uid, credits)
    try:
        await app.bot.send_message(uid,
            f"CREDITS DELIVERED\n\n"
            f"Pack: {pack['label']}\n"
            f"Credits added: {credits}\n\n"
            f"Send any code file, paste code, or send a screenshot to start cleaning.\n\n"
            f"CodeClean Bot. Built for developers.")
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
    print("CODECLEAN BOT v1.0")
    print("AI Code Cleaner + OCR")
    print("Status: ONLINE")
    print("=" * 50)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"Webhook server running on port {os.environ.get('PORT', 8080)}")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("credits", cmd_credits))
    application.add_handler(CommandHandler("buy", cmd_buy))
    application.add_handler(CommandHandler("history", cmd_history))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("stats", cmd_stats))

    application.add_handler(CallbackQueryHandler(buy_cb, pattern="^buy:"))
    application.add_handler(CallbackQueryHandler(credits_cb, pattern="^credits"))

    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
