import os

def clean(val):
    return val.strip().lstrip("=").strip() if val else ""

BOT_TOKEN = clean(os.environ.get("BOT_TOKEN", ""))
OWNER_ID = int(clean(os.environ.get("OWNER_ID", "0")) or "0")
OPENAI_API_KEY = clean(os.environ.get("OPENAI_API_KEY", ""))
STRIPE_SECRET_KEY = clean(os.environ.get("STRIPE_SECRET_KEY", ""))
STRIPE_WEBHOOK_SECRET = clean(os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
DATABASE_URL = clean(os.environ.get("DATABASE_URL", ""))
BOT_USERNAME = clean(os.environ.get("BOT_USERNAME", "CodeCleanAI_bot"))

FREE_CLEANS = 2

CREDIT_PACKS = {
    "starter": {
        "credits": 15,
        "price": 5,
        "label": "Starter — 15 cleans",
    },
    "pro": {
        "credits": 75,
        "price": 20,
        "label": "Pro — 75 cleans",
    },
    "elite": {
        "credits": 200,
        "price": 50,
        "label": "Elite — 200 cleans + Priority",
    },
}

SUPPORTED_EXTENSIONS = [
    ".py", ".js", ".html", ".css", ".json",
    ".ts", ".txt", ".env", ".yaml", ".yml", ".sh", ".md"
]

WELCOME_MESSAGE = """
🧹 CODECLEAN BOT

Fix broken code instantly.

⚡ Fixes code copied from:
• iPhone / iOS Notes
• ChatGPT responses
• Screenshots
• Websites

✔ Fix curly quotes
✔ Fix indentation
✔ Remove invisible characters
✔ AI syntax repair
✔ OCR screenshot reading
✔ GitHub file cleaning
✔ Clean Python / JS / HTML / JSON

Send:
• pasted code
• a code file
• a screenshot of code
• a GitHub file link

FREE TIER: 2 cleans included
""".strip()
