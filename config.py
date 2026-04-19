import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "0").strip().lstrip("="))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BOT_USERNAME = os.environ.get("BOT_USERNAME", "CodeCleanAI_bot").strip().lstrip("=")

FREE_CLEANS = 2

CREDIT_PACKS = {
    "starter": {
        "credits": 15,
        "price": 5,
        "label": "Starter — 15 cleans",
        "url": "https://buy.stripe.com/eVqfZjgLj5Ana8sami5Rm07"
    },
    "pro": {
        "credits": 75,
        "price": 20,
        "label": "Pro — 75 cleans",
        "url": "https://buy.stripe.com/9B63cxbqZ5An0xS7a65Rm08"
    },
    "elite": {
        "credits": 200,
        "price": 50,
        "label": "Elite — 200 cleans + Priority",
        "url": "https://buy.stripe.com/fZu14p1Qp9QDfsM51Y5Rm09"
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
