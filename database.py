import os
import psycopg2
from psycopg2 import pool
from datetime import date

_pool = None

def init_pool():
    global _pool
    _pool = pool.SimpleConnectionPool(1, 10, os.environ.get("DATABASE_URL"))

def get_db():
    return _pool.getconn()

def release_db(conn):
    _pool.putconn(conn)

def init_db():
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            credits INTEGER DEFAULT 0,
            free_cleans_used INTEGER DEFAULT 0,
            total_cleans INTEGER DEFAULT 0,
            is_pro BOOLEAN DEFAULT FALSE,
            last_daily DATE,
            referral_count INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS clean_history (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT,
            file_type TEXT,
            input_method TEXT,
            issues_fixed INTEGER DEFAULT 0,
            fix_report TEXT,
            cleaned_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS stripe_sessions (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT,
            session_id TEXT UNIQUE,
            pack TEXT,
            credits INTEGER,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily DATE;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_count INTEGER DEFAULT 0;")

        conn.commit()
        print("DATABASE READY — CodeClean v3.0")
    finally:
        release_db(conn)

def register_user(tid, username, referrer_id=None):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT telegram_id FROM users WHERE telegram_id=%s", (tid,))
        if cur.fetchone(): return False
        from config import FREE_CLEANS
        cur.execute(
            "INSERT INTO users (telegram_id, username, credits) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (tid, username, FREE_CLEANS))
        if referrer_id and referrer_id != tid:
            cur.execute(
                "UPDATE users SET credits=credits+2, referral_count=referral_count+1 WHERE telegram_id=%s",
                (referrer_id,))
        conn.commit(); return True
    finally:
        release_db(conn)

def get_user(tid):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "SELECT telegram_id, username, credits, free_cleans_used, total_cleans, is_pro, last_daily, referral_count FROM users WHERE telegram_id=%s",
            (tid,))
        return cur.fetchone()
    finally:
        release_db(conn)

def deduct_credit(tid):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT credits FROM users WHERE telegram_id=%s", (tid,))
        row = cur.fetchone()
        if not row or row[0] <= 0: return False
        cur.execute(
            "UPDATE users SET credits=credits-1, total_cleans=total_cleans+1 WHERE telegram_id=%s",
            (tid,))
        conn.commit(); return True
    finally:
        release_db(conn)

def add_credits(tid, amount):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET credits=credits+%s WHERE telegram_id=%s", (amount, tid))
        conn.commit()
    finally:
        release_db(conn)

def log_clean(tid, file_type, input_method, issues_fixed, fix_report):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO clean_history (telegram_id, file_type, input_method, issues_fixed, fix_report) VALUES (%s,%s,%s,%s,%s)",
            (tid, file_type, input_method, issues_fixed, fix_report))
        conn.commit()
    finally:
        release_db(conn)

def get_history(tid, limit=10):
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "SELECT file_type, input_method, issues_fixed, cleaned_at FROM clean_history WHERE telegram_id=%s ORDER BY cleaned_at DESC LIMIT %s",
            (tid, limit))
        return cur.fetchall()
    finally:
        release_db(conn)

def get_stats():
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM users"); total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM clean_history"); total_cleans = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM stripe_sessions WHERE status='completed'"); total_sales = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE is_pro=TRUE"); pro_users = cur.fetchone()[0]
        return total_users, total_cleans, total_sales, pro_users
    finally:
        release_db(conn)
