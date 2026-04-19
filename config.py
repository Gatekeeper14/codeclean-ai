import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "CodeCleanBot")

FREE_CLEANS = 2

CREDIT_PACKS = {
    "starter": {"credits": 15, "price": 5, "label": "Starter — 15 cleans"},
    "pro":     {"credits": 75, "price": 20, "label": "Pro — 75 cleans"},
    "elite":   {"credits": 200, "price": 50, "label": "Elite — 200 cleans + Priority"},
}

SUPPORTED_EXTENSIONS = [".py", ".js", ".html", ".css", ".json", ".ts", ".txt", ".env", ".yaml", ".yml", ".sh", ".md"]

WELCOME_MESSAGE = """
🧹 CODECLEAN BOT

Your AI-powered code cleaner.

✅ Fixes curly quotes and smart apostrophes
✅ Fixes indentation errors
✅ Repairs syntax issues with AI
✅ Reads code from screenshots via OCR
✅ Supports Python, JS, HTML, CSS, JSON and more

FREE TIER: {free} cleans included.

Send any code file, paste code as text, or send a screenshot of code to get started.

/credits — check balance
/buy — purchase more cleans
/help — full guide
""".strip().format(free=FREE_CLEANS)
