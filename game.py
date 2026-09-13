import json
import asyncio
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import smtplib
import threading
import webbrowser
import urllib.parse
import urllib.request
import time
from datetime import datetime
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import discord
except ImportError:
    discord = None

DATA_FILE = Path(__file__).with_name("users.json")
PROFILE_IMAGES_FILE = Path(__file__).with_name("profile_images.json")
GROUPS_FILE = Path(__file__).with_name("groups.json")
MESSAGES_FILE = Path(__file__).with_name("messages.json")
DATABASE_FILE = Path(__file__).with_name("bank.sqlite3")
IP_LOG_FILE = Path(__file__).with_name("ip_logs.jsonl")
MAX_MESSAGE_FILE_SIZE = 250 * 1024 * 1024
RECOVERY_TOKEN_TTL = 30 * 60
DATABASE_LOCK = threading.RLock()
THEMES = {
    "purple": ("Purple", "#32194f", "#eee8f5"),
    "blue": ("Ocean blue", "#164e78", "#e3f1fb"),
    "green": ("Forest green", "#176044", "#e4f4ec"),
    "red": ("Ruby red", "#8f2638", "#fbe8ec"),
    "wine": ("Wine red", "#641c32", "#f5e3e8"),
    "orange": ("Orange", "#a44916", "#fff0e1"),
    "yellow": ("Golden yellow", "#806000", "#fff8d9"),
    "pink": ("Pink", "#a33d70", "#fbe8f2"),
    "teal": ("Teal", "#11686a", "#e1f5f3"),
    "cyan": ("Cyan", "#126277", "#e2f5fa"),
    "indigo": ("Indigo", "#35418c", "#e9eafd"),
    "lavender": ("Lavender purple", "#76529b", "#f0eafa"),
    "plum": ("Plum purple", "#6b285f", "#f6e7f2"),
    "violet": ("Violet purple", "#542a87", "#eee5fb"),
    "orchid": ("Orchid purple", "#914e9f", "#f8eafa"),
    "brown": ("Brown", "#70452d", "#f6ece5"),
    "black": ("Charcoal", "#24242b", "#eeeeF2"),
    "cool_black": ("Cool black", "#17202b", "#e7edf3"),
}
COUNTRIES = {
    "United States", "Canada", "Mexico", "Brazil", "Argentina", "United Kingdom",
    "Ireland", "France", "Germany", "Spain", "Italy", "Portugal", "Netherlands",
    "Belgium", "Switzerland", "Austria", "Poland", "Czechia", "Sweden", "Norway",
    "Denmark", "Finland", "Ukraine", "Romania", "Greece", "Turkey", "South Africa",
    "Egypt", "Nigeria", "India", "China", "Japan", "South Korea", "Australia",
    "New Zealand", "Other",
}


def current_date():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored_password):
    if not stored_password.startswith("scrypt$"):
        return hmac.compare_digest(stored_password, password), True
    try:
        _, salt_hex, digest_hex = stored_password.split("$", 2)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=16384, r=8, p=1)
    except (ValueError, TypeError):
        return False, False
    return hmac.compare_digest(actual, expected), False


class User:
    def __init__(self, first_name, last_name, password, balance=0.0,
                 history=None, email="", discord_id="", transfer_requests=None,
                 notifications=None, auto_deposit_group_id="", profile_username="",
                 profile_icon="●", profile_image="", theme="purple", country="",
                 show_country=False, telephone="", friend_emails=None,
                 terms_accepted_at=""):
        self.first_name = first_name
        self.last_name = last_name
        self.password = password
        self.balance = balance
        self.email = email
        self.discord_id = discord_id
        self.transfer_requests = transfer_requests or []
        self.notifications = notifications or []
        self.auto_deposit_group_id = auto_deposit_group_id
        self.profile_username = profile_username or self.full_name()
        self.profile_icon = profile_icon or "●"
        self.profile_image = profile_image
        self.theme = theme or "purple"
        self.country = country
        self.show_country = bool(show_country)
        self.telephone = telephone
        self.friend_emails = friend_emails or []
        self.terms_accepted_at = terms_accepted_at
        self.history = history or [
            f"{current_date()} - Account created with balance: ${balance:.2f}"
        ]

    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        self.balance += amount
        self.history.append(f"{current_date()} - Deposited: +${amount:.2f}")

    def withdraw(self, amount):
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        if amount > self.balance:
            raise ValueError("Insufficient balance.")
        self.balance -= amount
        self.history.append(f"{current_date()} - Withdrawn: -${amount:.2f}")

    def to_dict(self):
        return {"first_name": self.first_name, "last_name": self.last_name,
                "password": self.password, "balance": self.balance,
            "history": self.history, "email": self.email,
                "discord_id": self.discord_id,
                "transfer_requests": self.transfer_requests,
                "notifications": self.notifications,
                "auto_deposit_group_id": self.auto_deposit_group_id,
                "profile_username": self.profile_username,
                "profile_icon": self.profile_icon,
                "theme": self.theme,
                "country": self.country,
                "show_country": self.show_country,
                "telephone": self.telephone,
                "friend_emails": self.friend_emails,
                "terms_accepted_at": self.terms_accepted_at}

    @classmethod
    def from_dict(cls, data):
        return cls(data["first_name"], data["last_name"], data["password"],
                   data["balance"], data["history"], data.get("email", ""),
                   data.get("discord_id", ""), data.get("transfer_requests", []),
                   data.get("notifications", []), data.get("auto_deposit_group_id", ""),
                   data.get("profile_username", ""), data.get("profile_icon", "●"),
                   data.get("profile_image", ""), data.get("theme", "purple"),
                   data.get("country", ""), data.get("show_country", False),
                   data.get("telephone", ""), data.get("friend_emails", []),
                   data.get("terms_accepted_at", ""))


def database_connection():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profile_images (
            email TEXT PRIMARY KEY,
            image TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups_data (
            group_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages_data (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT NOT NULL
        );
    """)
    return connection


def legacy_json(path, default):
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        with path.open() as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_users(users):
    with DATABASE_LOCK, database_connection() as connection:
        connection.execute("DELETE FROM users")
        connection.executemany(
            "INSERT INTO users (email, payload) VALUES (?, ?)",
            [(user.email.lower(), json.dumps(user.to_dict())) for user in users],
        )


def load_users():
    with DATABASE_LOCK, database_connection() as connection:
        rows = connection.execute("SELECT payload FROM users").fetchall()
        if not rows:
            legacy = legacy_json(DATA_FILE, [])
            if legacy:
                connection.executemany(
                    "INSERT INTO users (email, payload) VALUES (?, ?)",
                    [(item.get("email", "").lower(), json.dumps(item))
                     for item in legacy if item.get("email")],
                )
                rows = [(json.dumps(item),) for item in legacy if item.get("email")]
        return [User.from_dict(json.loads(row[0])) for row in rows]


def save_profile_images(images):
    with DATABASE_LOCK, database_connection() as connection:
        connection.execute("DELETE FROM profile_images")
        connection.executemany(
            "INSERT INTO profile_images (email, image) VALUES (?, ?)",
            [(email.lower(), image) for email, image in images.items()],
        )


def load_profile_images():
    with DATABASE_LOCK, database_connection() as connection:
        rows = connection.execute("SELECT email, image FROM profile_images").fetchall()
        if not rows:
            legacy = legacy_json(PROFILE_IMAGES_FILE, {})
            if legacy:
                connection.executemany(
                    "INSERT INTO profile_images (email, image) VALUES (?, ?)",
                    [(email.lower(), image) for email, image in legacy.items()],
                )
                return legacy
        return dict(rows)


def save_groups(groups):
    with DATABASE_LOCK, database_connection() as connection:
        connection.execute("DELETE FROM groups_data")
        connection.executemany(
            "INSERT INTO groups_data (group_id, payload) VALUES (?, ?)",
            [(group["id"], json.dumps(group)) for group in groups],
        )


def load_groups():
    with DATABASE_LOCK, database_connection() as connection:
        rows = connection.execute("SELECT group_id, payload FROM groups_data").fetchall()
        if not rows:
            legacy = legacy_json(GROUPS_FILE, [])
            if legacy:
                connection.executemany(
                    "INSERT INTO groups_data (group_id, payload) VALUES (?, ?)",
                    [(group["id"], json.dumps(group)) for group in legacy],
                )
                return legacy
        return [json.loads(payload) for _, payload in rows]


def save_messages(messages):
    with DATABASE_LOCK, database_connection() as connection:
        connection.execute("DELETE FROM messages_data")
        connection.executemany(
            "INSERT INTO messages_data (payload) VALUES (?)",
            [(json.dumps(message),) for message in messages],
        )


def load_messages():
    with DATABASE_LOCK, database_connection() as connection:
        rows = connection.execute("SELECT payload FROM messages_data ORDER BY message_id").fetchall()
        if not rows:
            legacy = legacy_json(MESSAGES_FILE, [])
            if legacy:
                connection.executemany(
                    "INSERT INTO messages_data (payload) VALUES (?)",
                    [(json.dumps(message),) for message in legacy],
                )
                return legacy
        return [json.loads(row[0]) for row in rows]


users = load_users()
if not DATA_FILE.exists():
    save_users(users)
legacy_passwords_found = False
for account in users:
    if not account.password.startswith("scrypt$"):
        account.password = hash_password(account.password)
        legacy_passwords_found = True
if legacy_passwords_found:
    save_users(users)
profile_images = load_profile_images()
profile_images_changed = False
for account in users:
    if account.profile_image and account.email not in profile_images:
        profile_images[account.email] = account.profile_image
        profile_images_changed = True
    account.profile_image = profile_images.get(account.email, "")
if profile_images_changed or not PROFILE_IMAGES_FILE.exists():
    save_profile_images(profile_images)
    save_users(users)
groups = load_groups()
if not GROUPS_FILE.exists():
    save_groups(groups)
messages = load_messages()
if not MESSAGES_FILE.exists():
    save_messages(messages)
oauth_states = {}
recovery_tokens = {}
sessions = {}


def create_session(user):
    token = secrets.token_urlsafe(32)
    sessions[token] = user.email.lower()
    return token


def session_cookie(token):
    secure = "; Secure" if os.getenv("COOKIE_SECURE") == "1" else ""
    return f"bank_session={token}; HttpOnly; Path=/; SameSite=Lax{secure}"


def session_user(handler):
    token = handler.cookie_value("bank_session")
    email = sessions.get(token, "")
    return find_user_by_email(email) if email else None


discord_loop = None
discord_client = None


def start_discord_bot():
    global discord_loop, discord_client
    if discord is None:
        print("Discord notifications disabled: install discord.py to enable them.")
        return
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token or not os.getenv("DISCORD_USER_ID"):
        return

    async def on_ready():
        print(f"Discord bot connected as {discord_client.user}")

    async def send_dm(message):
        user = await discord_client.fetch_user(int(os.environ["DISCORD_USER_ID"]))
        await user.send(message)

    def run_bot():
        global discord_loop
        discord_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(discord_loop)
        try:
            discord_loop.run_until_complete(discord_client.start(token))
        finally:
            discord_loop.close()

    discord_client = discord.Client(intents=discord.Intents.none())
    discord_client.event(on_ready)
    discord_client.send_dm = send_dm
    threading.Thread(target=run_bot, daemon=True).start()


def notify_discord(message):
    if not discord_loop or not discord_client or not discord_client.is_ready():
        return
    asyncio.run_coroutine_threadsafe(discord_client.send_dm(message), discord_loop)
def public_user(user):
    return {"name": user.full_name(), "first_name": user.first_name,
            "last_name": user.last_name, "email": user.email,
            "display_name": user.profile_username, "profile_icon": user.profile_icon,
            "profile_image": user.profile_image,
            "theme": user.theme,
            "country": user.country if user.show_country else "",
            "show_country": user.show_country,
            "discord_id": user.discord_id,
            "balance": round(user.balance, 2),
            "history": list(reversed(user.history)),
            "transfer_requests": [request for request in user.transfer_requests
                                   if request.get("status") == "pending"],
            "groups": [public_group(group, user) for group in groups
                        if group_member(group, user.email)],
            "notifications": list(reversed(user.notifications[-30:])),
            "unread_notifications": sum(1 for item in user.notifications
                                         if not item.get("read")),
            "auto_deposit_group_id": user.auto_deposit_group_id,
            "friends": [public_profile(friend, user) for friend in users
                        if friend.email.lower() in user.friend_emails]}


def public_profile(user, viewer=None):
    return {"display_name": user.profile_username, "profile_icon": user.profile_icon,
            "profile_image": user.profile_image,
            "country": user.country if user.show_country else "",
            "email": user.email,
            "is_friend": bool(viewer and user.email.lower() in viewer.friend_emails)}


def find_user_by_email(email):
    normalized = email.strip().lower()
    return next((user for user in users if user.email.lower() == normalized), None)


def group_member(group, email):
    return next((member for member in group["members"]
                 if member["email"].lower() == email.lower()), None)


def group_for_user(group_id, user):
    group = next((item for item in groups if item["id"] == group_id), None)
    if not group or not group_member(group, user.email):
        return None
    return group


def can_manage_group(group, user):
    member = group_member(group, user.email)
    return member and member["role"] in ("creator", "subcreator")


def add_notification(user, title, message):
    user.notifications.append({"id": secrets.token_urlsafe(10),
                                "title": title, "message": message,
                                "created_at": current_date(), "read": False})
    user.notifications = user.notifications[-100:]


def notify_group(group, title, message, exclude_email=""):
    for member in group["members"]:
        if member["email"].lower() != exclude_email.lower():
            recipient = find_user_by_email(member["email"])
            if recipient:
                add_notification(recipient, title, message)


def add_group_history(group, message):
    group.setdefault("history", []).append(f"{current_date()} - {message}")
    group["history"] = group["history"][-200:]


def deposit_income(user, amount, source):
    group = next((item for item in groups
                  if item["id"] == user.auto_deposit_group_id
                  and group_member(item, user.email)), None)
    if not group:
        user.deposit(amount)
        return None
    group["balance"] = round(group["balance"] + amount, 2)
    user.history.append(f"{current_date()} - Auto-deposited into {group['name']}: +${amount:.2f}")
    add_group_history(group, f"{visible_name(user)} auto-deposited ${amount:.2f}")
    notify_group(group, "Automatic group deposit",
                 f"{visible_name(user)} auto-deposited ${amount:.2f} into {group['name']}.", user.email)
    return group


def public_group(group, user=None):
    member = group_member(group, user.email) if user else None
    visible_members = []
    for item in group["members"]:
        account = find_user_by_email(item["email"])
        visible_members.append({"email": item["email"],
                                "name": account.profile_username if account else item["name"],
                                "icon": account.profile_icon if account else "●",
                                "image": account.profile_image if account else "",
                                "role": item["role"]})
    return {"id": group["id"], "name": group["name"],
            "balance": round(group["balance"], 2),
            "group_image": group.get("group_image", ""),
            "creator_email": group["creator_email"],
            "members": visible_members,
            "history": list(reversed(group.get("history", []))),
            "join_requests": group["join_requests"] if member and
            member["role"] in ("creator", "subcreator") else [],
            "my_role": member["role"] if member else ""}


def find_user(name):
    return next((user for user in users if user.full_name().lower() == name.lower()), None)


def visible_name(user):
    return user.profile_username


def find_user_login(identifier):
    normalized = identifier.strip().lower()
    return next((user for user in users
                 if user.full_name().lower() == normalized
                 or user.email.lower() == normalized), None)


def find_user_by_discord_id(discord_id):
    return next((user for user in users if user.discord_id == discord_id), None)


def discord_login_url(state):
    params = urllib.parse.urlencode({
        "client_id": os.environ["DISCORD_CLIENT_ID"],
        "redirect_uri": os.environ["DISCORD_REDIRECT_URI"],
        "response_type": "code",
        "scope": "identify email",
        "state": state,
    })
    return f"https://discord.com/oauth2/authorize?{params}"


def discord_token(code):
    payload = urllib.parse.urlencode({
        "client_id": os.environ["DISCORD_CLIENT_ID"],
        "client_secret": os.environ["DISCORD_CLIENT_SECRET"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": os.environ["DISCORD_REDIRECT_URI"],
    }).encode()
    request = urllib.request.Request(
        "https://discord.com/api/oauth2/token", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(request, timeout=10).read())


def discord_profile(access_token):
    request = urllib.request.Request(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}"})
    return json.loads(urllib.request.urlopen(request, timeout=10).read())


def valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


def send_email(recipient, subject, body):
    if not recipient:
        return False
    host = os.getenv("SMTP_HOST")
    if not host:
        print(f"Email queued for {recipient}: {subject}")
        return False
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "bank@localhost"))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as smtp:
            smtp.starttls()
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(message)
        return True
    except (smtplib.SMTPException, OSError, KeyError) as error:
        print(f"Email could not be sent to {recipient}: {error}")
        return False


PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Northstar Bank</title>
<style>
:root{--ink:#17231f;--muted:#6d7b74;--cream:#f5f3ed;--paper:#fffdf8;--green:#1d5948;--mint:#d7ebe0;--coral:#d96f57;--line:#dfe4dc}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:15px Georgia,serif}.shell{min-height:100vh;display:grid;grid-template-columns:245px 1fr}.rail{background:var(--green);color:#f5f1e7;padding:28px 22px;display:flex;flex-direction:column}.brand{font-size:22px;letter-spacing:.03em;margin-bottom:75px}.brand b{color:#b8dfc6}.nav{display:grid;gap:9px}.nav button,.logout{border:0;background:transparent;color:#d9e9df;text-align:left;padding:13px 14px;border-radius:10px;font:inherit;cursor:pointer}.nav button.active,.nav button:hover{background:#31725c;color:white}.logout{margin-top:auto;border:1px solid #61927b;text-align:center}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:34px}.eyebrow{font:11px Arial,sans-serif;text-transform:uppercase;letter-spacing:.18em;color:var(--muted)}h1{font-size:36px;font-weight:normal;margin:8px 0 0}.avatar{width:42px;height:42px;border-radius:50%;background:var(--coral);color:white;display:grid;place-items:center;font:bold 15px Arial}main{max-width:1120px;width:100%;padding:34px 5vw 50px;margin:auto}.balance{background:var(--green);color:white;border-radius:16px;padding:26px 30px;display:flex;justify-content:space-between;align-items:end;box-shadow:0 12px 28px #1d59481c}.balance small{display:block;color:#b8dfc6;font:12px Arial;margin-bottom:12px}.amount{font-size:43px}.badge{background:#d7ebe01f;border:1px solid #93c6aa;color:#c5e6d0;padding:10px 15px;border-radius:30px;font:12px Arial}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:20px}.panel{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:22px}.panel h2{font-size:20px;font-weight:normal;margin:0 0 18px}.actions{display:flex;gap:10px}.actions button{flex:1;border:0;padding:13px;border-radius:8px;background:var(--mint);color:var(--green);font-weight:bold;cursor:pointer}.actions button:last-child{background:#f7ddd5;color:#974735}.history{margin-top:20px}.transaction{display:flex;justify-content:space-between;padding:14px 0;border-bottom:1px solid var(--line);font-size:14px}.transaction:last-child{border:0}.transaction span{color:var(--muted);font:12px Arial}.positive{color:var(--green)}.negative{color:var(--coral)}.login{max-width:430px;margin:12vh auto;background:var(--paper);padding:36px;border-radius:16px;border:1px solid var(--line);box-shadow:0 15px 40px #17231f12}.login h1{margin-bottom:8px}.login p{color:var(--muted);line-height:1.5}.field{margin:17px 0}.field label{display:block;font:11px Arial;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin-bottom:7px}.field input{width:100%;padding:13px;border:1px solid var(--line);border-radius:8px;background:white;font:16px Georgia}.primary{width:100%;padding:14px;border:0;border-radius:8px;background:var(--green);color:white;font:bold 15px Arial;cursor:pointer}.error{color:var(--coral);min-height:20px;font:13px Arial;margin:12px 0}.toast{position:fixed;right:24px;bottom:24px;background:var(--ink);color:white;padding:13px 17px;border-radius:8px;font:13px Arial;display:none}@media(max-width:700px){.shell{display:block}.rail{padding:18px;display:block}.brand{margin:0 0 15px}.nav{display:flex}.nav button{padding:9px 10px}.logout{float:right;margin-top:-38px;padding:8px 12px;width:auto}.top{margin-top:24px}.grid{grid-template-columns:1fr}.balance{align-items:start;gap:18px;flex-direction:column}.amount{font-size:36px}}
</style><style>
:root{--ink:#17231f;--muted:#68766f;--cream:#edf1eb;--paper:#fbfcf8;--green:#123f39;--mint:#d6eee2;--coral:#e4775e;--line:#d3ded6;--gold:#e9bd72}
body{background:var(--cream);background-image:linear-gradient(120deg,#edf1eb 0%,#f7f4ec 54%,#e8f0e9 100%);font-family:Georgia,serif}
.shell{grid-template-columns:238px 1fr}.rail{background:var(--green);padding:30px 20px;position:relative;overflow:hidden}.rail:after{content:"";position:absolute;width:180px;height:180px;border:1px solid #9ad1b540;border-radius:50%;right:-90px;bottom:80px;box-shadow:0 0 0 22px #9ad1b512,0 0 0 44px #9ad1b509}.brand{font-size:23px;font-weight:bold;letter-spacing:.02em;margin-bottom:76px;position:relative;z-index:1}.brand b{color:var(--gold)}.nav{gap:6px;position:relative;z-index:1}.nav button,.logout{padding:12px 13px;border-radius:7px;transition:background .2s,transform .2s}.nav button.active,.nav button:hover{background:#2c675c;transform:translateX(3px)}.logout{position:relative;z-index:1}
main{max-width:1160px;padding:42px 6vw 60px}.top{margin-bottom:30px}.eyebrow{color:#8a6950;font-size:10px;font-weight:bold;letter-spacing:.22em}.top h1{font-size:42px;letter-spacing:-.02em}.avatar{background:var(--coral);box-shadow:0 0 0 5px #e4775e28;width:46px;height:46px}
.balance{position:relative;overflow:hidden;background:var(--green);border-radius:10px;padding:32px 34px;box-shadow:0 18px 35px #123f3925}.balance:after{content:"";position:absolute;width:260px;height:260px;border:1px solid #c3e9d333;border-radius:50%;right:-80px;top:-140px;box-shadow:0 0 0 18px #c3e9d314,0 0 0 36px #c3e9d308}.balance>*{position:relative;z-index:1}.amount{font-size:49px;font-weight:normal}.badge{background:#e9bd7220;border-color:#e9bd72;color:#f7dba5;border-radius:5px}
.grid{gap:20px;margin-top:20px}.panel{border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 20px #2542380a}.panel h2{font-size:21px}.actions button{border-radius:6px;padding:14px;transition:transform .2s,box-shadow .2s}.actions button:hover{transform:translateY(-2px);box-shadow:0 6px 12px #25423818}.transaction{padding:15px 0}.history{margin-top:20px}
.login{background:var(--paper);border-radius:10px;border:1px solid var(--line);box-shadow:0 20px 55px #25423818;padding:40px}.login h1{font-size:39px}.login:before{content:"";display:block;width:42px;height:5px;background:var(--coral);border-radius:3px;margin-bottom:25px}.field input{border-radius:6px;border-color:#cbd8cf}.primary{border-radius:6px;background:var(--coral);transition:background .2s,transform .2s}.primary:hover{background:#c96650;transform:translateY(-1px)}.discord-login{display:flex;align-items:center;justify-content:center;width:100%;min-height:58px;padding:17px 20px;border-radius:8px;background:#5865f2;color:white;font:bold 17px Arial;text-decoration:none;box-shadow:0 8px 18px #5865f233;transition:background .2s,transform .2s}.discord-login:hover{background:#4752c4;transform:translateY(-2px)}.link{padding:0;border:0;background:transparent;color:var(--green);font:inherit;text-decoration:underline;cursor:pointer}.toast{border-radius:6px;background:var(--green);box-shadow:0 10px 25px #123f3930}
@media(max-width:700px){.rail{padding:20px 16px}.brand{margin-bottom:16px}.nav button.active,.nav button:hover{transform:none}.top h1{font-size:34px}.balance{padding:26px 24px}.amount{font-size:40px}.login{margin:7vh 18px;padding:30px}}
</style><style>
:root{--bg-top:#a7c7dc;--bg-mid:#8db567;--bg-low:#7b4a2f;--grass:#5d9f4e;--grass-dark:#356b35;--grass-light:#8cc86d;--dirt:#845434;--dirt-dark:#56381e;--panel:#efe3b7;--panel-dark:#6a7d59;--panel-shadow:rgba(57,69,47,0.32);--button:#90c76d;--button-dark:#4d7a3a;--button-red:#d06a4c;--button-red-dark:#6f3127;--gold:#f0d764;--text:#253924;--muted:#607052;--blue:#5865f2;--blue-dark:#3846a6}
*{box-sizing:border-box;image-rendering:pixelated;image-rendering:crisp-edges}
html,body{margin:0;padding:0}
body{background:linear-gradient(#a7c7dc 0 52%,#8db567 52% 58%,#7b4a2f 58% 100%);font-family:'Press Start 2P',monospace;font-size:12px;letter-spacing:0;line-height:1.8;color:var(--text);position:relative}
body::before{content:"";position:fixed;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,rgba(255,255,255,.04) 0 2px,transparent 2px 8px),repeating-linear-gradient(90deg,rgba(0,0,0,.015) 0 2px,transparent 2px 10px);mix-blend-mode:multiply;opacity:.9}
.shell{grid-template-columns:250px 1fr}.rail{background:var(--grass-dark);padding:20px 16px;border-right:5px solid #2d4d30;box-shadow:inset -3px 0 #6a9b52}.rail:after{display:none}.brand{font-family:'Press Start 2P',monospace;font-size:16px;line-height:1.8;margin-bottom:46px;text-shadow:3px 3px rgba(37,74,42,.8);color:#f3f0d8}.brand b{color:var(--gold)}.nav{gap:10px}.nav button,.logout{border:4px solid #2d4d30;border-radius:0;background:#5d8f4a;color:#f5f1cf;padding:12px 10px;box-shadow:inset 3px 3px #8ec26d,inset -3px -3px #356b35;font:10px 'Press Start 2P',monospace;line-height:1.7}.nav button.active,.nav button:hover{background:#7aae5a;color:#fff;transform:none}.logout{background:#b45a40;margin-top:auto;box-shadow:inset 3px 3px #cf7b5a,inset -3px -3px #7d3427}
main{max-width:1100px;padding:30px 4vw 48px}.eyebrow{color:#3e573c;font-family:'Press Start 2P',monospace;font-size:10px;letter-spacing:0}.top h1{font-family:'Press Start 2P',monospace;font-size:24px;line-height:1.8;text-shadow:2px 2px rgba(240,232,190,.5)}.avatar{border-radius:0;border:4px solid #6e3328;background:#cf734c;box-shadow:4px 4px rgba(116,49,38,.8);width:48px;height:48px;font:11px 'Press Start 2P',monospace}
.balance{border:4px solid #2d4d30;border-radius:0;background:#3b6f37;padding:22px 24px;box-shadow:inset 4px 4px #5d8f4a,inset -4px -4px #2d4d30,6px 6px rgba(76,99,74,.5)}.balance:after{display:none}.balance small{font:9px 'Press Start 2P',monospace;color:#def3b4}.amount{font:34px 'Press Start 2P',monospace;color:#f0d764;text-shadow:3px 3px rgba(37,74,42,.8)}.badge{border:3px solid #e7c652;border-radius:0;background:#754d2a;color:#f8df8b;padding:10px 12px;font:9px 'Press Start 2P',monospace}
.grid{gap:18px;margin-top:22px}.panel{background:var(--panel);border:4px solid var(--panel-dark);border-radius:0;box-shadow:5px 5px var(--panel-shadow);padding:18px}.panel h2{font:14px 'Press Start 2P',monospace;color:#356b35;line-height:1.7;margin:0 0 16px}.actions{gap:12px}.actions button,.primary{border:4px solid #2d4d30;border-radius:0;background:var(--button);color:#20351e;padding:14px 12px;box-shadow:inset 3px 3px #b4dd8d,inset -3px -3px #4b7c43;font:bold 10px 'Press Start 2P',monospace;line-height:1.6}.actions button:last-child{background:var(--button-red);color:#fff0d8;box-shadow:inset 3px 3px #e89069,inset -3px -3px #743126}.actions button:hover,.primary:hover{transform:translate(1px,1px);box-shadow:inset 2px 2px #b4dd8d,inset -2px -2px #4b7c43}.transaction{border-bottom:3px solid rgba(180,182,147,0.6);padding:12px 0}.transaction span{font:9px 'Press Start 2P',monospace;color:#607052}.transaction strong{font:10px 'Press Start 2P',monospace}.positive{color:#356b35}.negative{color:#a84636}.history{margin-top:22px}
.login{background:var(--panel);border:4px solid var(--panel-dark);border-radius:0;box-shadow:7px 7px rgba(58,73,49,0.28);padding:30px}.login:before{width:48px;height:8px;background:var(--button-red);border-radius:0;box-shadow:3px 0 #d97f5f;margin-bottom:20px}.login h1{font:24px 'Press Start 2P',monospace;line-height:1.8;margin:0}.login p{font:9px 'Press Start 2P',monospace;line-height:2;color:#607052}.field label{font:9px 'Press Start 2P',monospace;color:#356b35}.field input{border:3px solid #778964;border-radius:0;background:#f5f0d2;padding:14px;font:10px 'Press Start 2P',monospace;outline:none;min-height:48px}.field input:focus{border-color:#4a6f45}.discord-login{min-height:56px;border:4px solid #2a3a87;border-radius:0;background:var(--blue);box-shadow:inset 3px 3px #929dff,inset -3px -3px #343e9c,4px 4px rgba(70,86,138,.8);font:10px 'Press Start 2P',monospace}.link{font:9px 'Press Start 2P',monospace;color:#356b35}.toast{border:4px solid #2d4d30;border-radius:0;background:#356b35;box-shadow:4px 4px rgba(83,107,64,.6);font:9px 'Press Start 2P',monospace;color:#f5f0d3}
@media(max-width:700px){.shell{grid-template-columns:1fr}.rail{border-right:0;border-bottom:6px solid #2d4d30;padding:16px 12px}.brand{font-size:13px;margin-bottom:14px}.nav{display:flex;flex-wrap:wrap;gap:6px}.nav button,.logout{font-size:8px;padding:8px 6px}.top h1{font-size:16px}.balance{padding:18px 20px}.amount{font-size:24px}.panel{padding:15px}.login{margin:4vh 12px;padding:22px}} 
*{font-family:"Trebuchet MS",Arial,sans-serif!important}
</style><style>
:root{--bank-purple:#32194f;--bank-purple-dark:#241137;--bank-purple-mid:#56337a;--bank-lilac:#eee8f5;--bank-lilac-dark:#d9cde7;--bank-ink:#211b2b;--bank-muted:#766e82;--bank-white:#fff;--bank-green:#21745a;--bank-red:#bd4f67}
*{image-rendering:auto}
html,body{min-height:100%;background:#000;color:var(--bank-ink);font-family:"Trebuchet MS",Arial,sans-serif!important}
body{font-size:15px;line-height:1.5;letter-spacing:0}
body::before{display:none}
.shell{min-height:100vh;display:grid;grid-template-columns:248px 1fr}
.rail{background:var(--bank-purple);color:#f8f5fb;padding:30px 20px;border:0;box-shadow:none}
.brand{font:700 22px "Trebuchet MS",Arial,sans-serif!important;line-height:1.2;margin:0 0 68px;color:#fff}
.brand b{color:#c8a8e6}
.nav{gap:8px}
.nav button,.logout{border:0;border-radius:8px;background:transparent;color:#d8cde4;padding:12px 14px;box-shadow:none;font:600 14px "Trebuchet MS",Arial,sans-serif!important;line-height:1.4;text-align:left;cursor:pointer}
.nav button.active,.nav button:hover{background:var(--bank-purple-mid);color:#fff;transform:none}
.logout{margin-top:auto;border:1px solid #735995;text-align:center;color:#fff}
main{max-width:1180px;width:100%;padding:44px 6vw 64px;margin:auto}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}
.eyebrow{color:var(--bank-muted);font:700 11px "Trebuchet MS",Arial,sans-serif!important;letter-spacing:.12em;text-transform:uppercase}
.top h1{font:600 34px "Trebuchet MS",Arial,sans-serif!important;line-height:1.2;margin:7px 0 0;color:var(--bank-ink);text-shadow:none}
.avatar{width:48px;height:48px;border:0;border-radius:50%;background:#c58ee4;color:#32194f;box-shadow:none;font:700 15px "Trebuchet MS",Arial,sans-serif!important}
.balance{position:relative;overflow:hidden;display:flex;justify-content:space-between;align-items:flex-end;background:var(--bank-purple);color:#fff;border:0;border-radius:16px;padding:28px 30px;box-shadow:0 14px 30px #32194f25}
.balance:after{content:"";display:block;position:absolute;width:240px;height:240px;border:1px solid #d9c1f333;border-radius:50%;right:-70px;top:-150px;box-shadow:0 0 0 18px #d9c1f314,0 0 0 36px #d9c1f308}
.balance>*{position:relative;z-index:1}
.balance small{display:block;color:#d9c9e8;font:700 11px "Trebuchet MS",Arial,sans-serif!important;letter-spacing:.1em;margin-bottom:9px}
.amount{font:600 42px "Trebuchet MS",Arial,sans-serif!important;color:#fff;text-shadow:none}
.badge{background:#ffffff18;border:1px solid #d8c3ed;color:#eee5f8;border-radius:20px;padding:8px 13px;font:600 12px "Trebuchet MS",Arial,sans-serif!important}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}
.panel{background:var(--bank-white);border:1px solid #e6dfed;border-radius:12px;box-shadow:0 8px 24px #32194f0c;padding:22px}
.panel h2{font:600 20px "Trebuchet MS",Arial,sans-serif!important;line-height:1.3;color:var(--bank-ink);margin:0 0 18px}
.actions{display:flex;gap:10px}
.actions button,.primary{border:0;border-radius:8px;background:var(--bank-lilac);color:var(--bank-purple);padding:13px 14px;box-shadow:none;font:700 14px "Trebuchet MS",Arial,sans-serif!important;line-height:1.4;cursor:pointer}
.actions button:last-child{background:#f9e8ed;color:var(--bank-red);box-shadow:none}
.actions button:hover,.primary:hover{transform:translateY(-1px);box-shadow:0 5px 12px #32194f18}
.transaction{border-bottom:1px solid #eee9f2;padding:13px 0}.group-card{border:1px solid #e4ddec;border-radius:10px;padding:16px;margin:12px 0}.group-card h3{margin:0 0 5px}.group-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.group-actions button{border:0;border-radius:6px;padding:8px 10px;background:var(--bank-purple);color:#fff;cursor:pointer}.group-actions button.secondary{background:var(--bank-lilac)}.group-request{background:#faf7fc;border-radius:6px;padding:10px;margin-top:8px}.group-member{display:flex;justify-content:space-between;gap:24px;border-top:1px solid #eee9f2;padding:10px 0}.group-member span:first-child{margin-right:18px}.group-member span:last-child{font-size:15px;font-weight:700;color:#fff}.group-member .role-creator{color:#4b8df8}.group-member .role-subcreator{color:#66a6ff}.group-member .role-member{color:#fff}
.notice-wrap{position:relative}.notice-button{border:0;border-radius:50%;background:#f2edf7;color:var(--bank-purple);width:42px;height:42px;font-size:20px;cursor:pointer}.notice-count{position:absolute;right:-3px;top:-5px;background:var(--bank-red);color:#fff;border-radius:12px;padding:1px 6px;font-size:11px;font-weight:700}.notice-panel{display:none;position:absolute;right:0;top:52px;width:320px;max-height:360px;overflow:auto;background:#fff;border:1px solid #e4ddec;border-radius:10px;box-shadow:0 12px 30px #32194f25;z-index:5;padding:14px}.notice-panel.open{display:block}.notice-item{border-bottom:1px solid #eee9f2;padding:10px 4px;cursor:pointer}.notice-item:last-child{border-bottom:0}.notice-item.unread{background:#faf7fc}.notice-item strong{display:block;color:var(--bank-ink)}.notice-item small{color:var(--bank-muted)}
.transaction span{font:400 13px "Trebuchet MS",Arial,sans-serif!important;color:var(--bank-muted)}
.transaction strong{font:600 14px "Trebuchet MS",Arial,sans-serif!important}
.positive{color:var(--bank-green)}.negative{color:var(--bank-red)}.history{margin-top:20px}
.login{max-width:480px;background:var(--bank-white);border:1px solid #e4ddec;border-radius:16px;box-shadow:0 20px 55px #32194f18;padding:38px}
.login:before{width:44px;height:5px;background:#a56dcc;border-radius:3px;box-shadow:none;margin-bottom:23px}
.login h1{font:600 34px "Trebuchet MS",Arial,sans-serif!important;line-height:1.2;color:var(--bank-ink);margin:0}
.login p{font:400 14px "Trebuchet MS",Arial,sans-serif!important;line-height:1.6;color:var(--bank-muted)}
.field label{font:700 13px "Trebuchet MS",Arial,sans-serif!important;color:var(--bank-ink)}
.field input{border:1px solid #d9d0e2;border-radius:8px;background:#fcfbfd;padding:12px 14px;color:var(--bank-ink);font:400 15px "Trebuchet MS",Arial,sans-serif!important;outline:none;min-height:44px}
.field input:focus{border-color:#8e5ab1;box-shadow:0 0 0 3px #8e5ab120}
.primary{background:var(--bank-purple);color:#fff;width:100%;margin-top:5px}
.link{padding:0;border:0;background:transparent;color:var(--bank-purple-mid);font:600 13px "Trebuchet MS",Arial,sans-serif!important;text-decoration:underline;cursor:pointer}
.toast{border:0;border-radius:8px;background:var(--bank-purple);box-shadow:0 10px 25px #32194f30;font:600 13px "Trebuchet MS",Arial,sans-serif!important;color:#fff}
.credit{position:fixed;right:14px;bottom:10px;color:#aaa;font:12px "Trebuchet MS",Arial,sans-serif!important;opacity:.8;z-index:10}
@media(max-width:700px){.shell{grid-template-columns:1fr}.rail{padding:18px 16px;border:0}.brand{font-size:20px;margin-bottom:18px}.nav{display:flex;flex-wrap:wrap;gap:5px}.nav button,.logout{font-size:13px;padding:9px 10px}.logout{margin-top:12px}.top h1{font-size:28px}.balance{padding:24px 22px;display:block}.badge{display:inline-block;margin-top:14px}.amount{font-size:36px}.grid{grid-template-columns:1fr}.panel{padding:18px}.login{margin:6vh 16px;padding:28px}}
</style></head><body><div id="app"></div><div class="toast" id="toast"></div><div class="credit">made by KamykOG</div><script>
let user=null;const app=document.getElementById('app');function initials(n){return n.split(' ').map(x=>x[0]).join('')}
function showLogin(message=''){app.innerHTML=`<section class="login"><div class="eyebrow">Northstar Bank</div><h1>Welcome back.</h1><p>Your money, clearly organised.</p><form id="login"><div class="field"><label>Full name or email</label><input name="name" placeholder="First and last name or email" required></div><div class="field"><label>Password</label><input name="password" type="password" placeholder="Your password" required></div><div class="error">${message}</div><button type="submit" class="primary">Open my account</button></form><p><button type="button" class="link" onclick="showRegister(event)">Create a new account</button></p></section>`;document.getElementById('login').onsubmit=login}
function showRegister(event){if(event)event.preventDefault();app.innerHTML=`<section class="login"><div class="eyebrow">Northstar Bank</div><h1>Create your account.</h1><p>Start with a fresh everyday checking account.</p><div style="max-height:190px;overflow:auto;margin:16px 0;padding:12px;border:1px solid #e4ddec;border-radius:8px;background:#faf8fc;color:var(--bank-muted);font-size:12px;line-height:1.5"><strong style="display:block;color:var(--bank-ink);font-size:14px;margin-bottom:6px">Community rules and data policy</strong><p>No nudity, bullying, harassment, hate, threats, scams, or illegal activity. Use respectful language and do not misuse other people’s personal data.</p><p>To provide the bank and social features, Northstar may store and process your name, email, password, balances, deposits, withdrawals, transfer requests, messages, groups, group activity, country, friends, profile information, and notification history. Your optional telephone number is stored privately. We do not show private data to other users unless a feature explicitly says it is public.</p></div><form id="register"><div class="field"><label>First name</label><input name="first_name" required></div><div class="field"><label>Last name</label><input name="last_name" required></div><div class="field"><label>Email</label><input name="email" type="email" placeholder="you@example.com" required></div><div class="field"><label>Password</label><input name="password" type="password" minlength="6" required></div><label style="display:flex;gap:8px;align-items:flex-start;margin:14px 0;color:var(--bank-ink);font-size:13px"><input name="accept_terms" type="checkbox" value="yes" required> I accept the community rules and data policy.</label><div class="error"></div><button type="submit" class="primary">Register account</button></form><p><button type="button" class="link" onclick="showLogin()">Already have an account? Log in</button></p></section>`;document.getElementById('register').onsubmit=register}
async function register(e){e.preventDefault();let body=Object.fromEntries(new FormData(e.target));if(!body.first_name||!body.last_name||!body.email||!body.password){e.target.querySelector('.error').textContent='Please complete all required fields.';return}let r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let data=await r.json();if(!r.ok){e.target.querySelector('.error').textContent=data.error;return}user=data;localStorage.setItem('bank_user',user.name);document.cookie='bank_user='+encodeURIComponent(user.name)+'; path=/; SameSite=Lax';render()}
async function login(e){e.preventDefault();let body=Object.fromEntries(new FormData(e.target));if(!body.name||!body.password){showLogin('Please enter your name and password.');return}let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let data=await r.json();if(!r.ok)return showLogin(data.error);user=data;localStorage.setItem('bank_user', user.name);document.cookie='bank_user='+encodeURIComponent(user.name)+'; path=/; SameSite=Lax';render()}
function connectDiscord(){if(!user){const saved=localStorage.getItem('bank_user');if(saved){user={name:saved};}else{return showLogin('Please log in first.');}}window.location.href='/auth/discord?user='+encodeURIComponent(user.name)}
function render(){let incoming=user.transfer_requests.filter(x=>x.recipient_email===user.email);let outgoing=user.transfer_requests.filter(x=>x.sender_email===user.email);app.innerHTML=`<div class="shell"><aside class="rail"><div class="brand">northstar <b>●</b></div><nav class="nav"><button class="active">Overview</button><button>Payments</button><button>Statements</button></nav><button class="logout" onclick="logout()">Log out</button></aside><main><header class="top"><div><div class="eyebrow">Personal account</div><h1>Good day, ${user.first_name}.</h1></div><div class="avatar">${initials(user.name)}</div></header><section class="balance"><div><small>AVAILABLE BALANCE</small><div class="amount">$${user.balance.toFixed(2)}</div></div><div class="badge">● Account in good standing</div></section><div class="grid"><section class="panel"><h2>Move money</h2><div class="actions"><button onclick="move('deposit')">＋ Deposit</button><button onclick="move('withdraw')">− Withdraw</button></div><button class="primary" onclick="requestTransfer()">Request a transfer</button></section><section class="panel"><h2>Account details</h2><div class="transaction"><span>Account holder</span><strong>${user.name}</strong></div><div class="transaction"><span>Email</span><strong>${user.email||'Not added'}</strong></div><button class="link" onclick="addEmail()">${user.email?'Change email':'Add email'}</button></section></div><section class="panel history"><h2>Transfer requests</h2>${incoming.map(request=>request.status==='pending'?`<div class="transaction"><strong>${request.sender_name}</strong> wants to send $${Number(request.amount).toFixed(2)}<br><button onclick="respondTransfer('${request.id}','accept')">Accept deposit</button><button onclick="respondTransfer('${request.id}','reject')">Decline</button></div>`:'').join('')||'<div class="transaction"><span>No incoming requests.</span></div>'}${outgoing.filter(request=>request.status==='pending').map(request=>`<div class="transaction"><span>Waiting for ${request.recipient_name} to accept $${Number(request.amount).toFixed(2)}</span></div>`).join('')}</section><section class="panel history"><h2>Recent activity</h2>${user.history.slice(0,6).map(item=>{let positive=item.includes('Deposited')||item.includes('created');let parts=item.split(' - ');return `<div class="transaction"><span>${parts[0]}<br>${parts[1]}</span><strong class="${positive?'positive':'negative'}">${parts[1].includes('Withdrawn')?'-':'+'}${parts[1].split('$')[1]?'$'+parts[1].split('$')[1]:''}</strong></div>`}).join('')}</section></main></div>`}
async function addEmail(){let email=prompt('Enter your email:',user.email||'');if(email===null)return;let r=await fetch('/api/profile/email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,email})});let data=await r.json();if(!r.ok)return toast(data.error);user=data;render();toast('Email saved')}
async function move(type){let amount=prompt(type==='deposit'?'How much would you like to deposit?':'How much would you like to withdraw?');if(amount===null)return;let r=await fetch('/api/transaction',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,type,amount})});let data=await r.json();if(!r.ok)return toast(data.error);user=data;render();toast('Transaction completed')}
async function requestTransfer(){let old=document.getElementById('transfer-modal');if(old)old.remove();let modal=document.createElement('div');modal.id='transfer-modal';modal.className='profile-modal';modal.innerHTML='<div class="profile-window"><button class="profile-close">×</button><h2>Send money</h2><p>Choose a friend or group member, or search by email, name, or username.</p><input class="settings-input" id="transfer-search" placeholder="Search people"><div id="transfer-people" class="transfer-people"></div><input class="settings-input" id="transfer-amount" type="number" min="0.01" step="0.01" placeholder="Amount"><button class="primary" id="transfer-submit" disabled>Send request</button></div>';document.body.append(modal);modal.querySelector('.profile-close').onclick=()=>modal.remove();let people=[];let seen=new Set();(user.friends||[]).forEach(person=>{if(!seen.has(person.email)){seen.add(person.email);people.push(person)}});(user.groups||[]).forEach(group=>group.members.forEach(member=>{if(member.email!==user.email&&!seen.has(member.email)){seen.add(member.email);people.push({email:member.email,display_name:member.name,profile_icon:member.icon||'●',country:''})}}));let selected='';let results=modal.querySelector('#transfer-people');let search=modal.querySelector('#transfer-search');let amount=modal.querySelector('#transfer-amount');let submit=modal.querySelector('#transfer-submit');function draw(list){results.innerHTML=list.map(person=>`<button class="transfer-person ${selected===person.email?'selected':''}" data-email="${person.email}">${person.profile_icon||'●'} <strong>${person.display_name}</strong> <small>${person.email}</small></button>`).join('')||'<p>No people found.</p>';results.querySelectorAll('button').forEach(button=>button.onclick=()=>{selected=button.dataset.email;draw(list);submit.disabled=!selected||!amount.value})}draw(people);search.oninput=async()=>{let query=search.value.trim().toLowerCase();let local=people.filter(person=>(person.display_name+' '+person.email).toLowerCase().includes(query));if(query.length>=2){let response=await fetch('/api/friends/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,query})});if(response.ok){let data=await response.json();data.results.forEach(person=>{if(!people.some(item=>item.email===person.email))people.push(person)});local=people.filter(person=>(person.display_name+' '+person.email).toLowerCase().includes(query))}}draw(local)};amount.oninput=()=>{submit.disabled=!selected||!amount.value};submit.onclick=async()=>{let response=await fetch('/api/transfer/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,email:selected,amount:amount.value})});let data=await response.json();if(!response.ok)return toast(data.error);modal.remove();user=data;render();toast('Transfer request sent')}}
async function respondTransfer(id,action){let r=await fetch('/api/transfer/respond',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,id,action})});let data=await r.json();if(!r.ok)return toast(data.error);user=data;render();toast(action==='accept'?'Deposit accepted':'Request declined')}
function toast(message){let el=document.getElementById('toast');el.textContent=message;el.style.display='block';setTimeout(()=>el.style.display='none',2500)}function logout(){user=null;localStorage.removeItem('bank_user');document.cookie='bank_user=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';showLogin()}showLogin();
function groupAction(action,id){if(action==='join')return groupRequest(id);let amount=prompt('Amount:');if(amount===null)return;let body={group_id:id,name:user.name,amount};if(action==='transfer')body.email=prompt('Member email:');return groupPost(action,body)}
async function groupRequest(id){let response=await fetch('/api/groups/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:id,name:user.name})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;render();toast('Join request sent')}
async function groupPost(action,body){let response=await fetch('/api/groups/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let data=await response.json();if(!response.ok)return toast(data.error);user=data;render();toast('Group updated')}
async function deleteGroup(group){if(!confirm('Delete '+group.name+'? This cannot be undone.'))return;let response=await fetch('/api/groups/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:group.id,name:user.name})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;render();toast('Group deleted')}
async function createGroup(){let group_name=prompt('Group name:');if(!group_name)return;let response=await fetch('/api/groups/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,group_name})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;render();toast('Group created')}
async function loadGroups(){let response=await fetch('/api/groups');let list=await response.json();let panel=document.getElementById('groups-panel');if(!panel)return;panel.innerHTML='<h2>Groups</h2><button class="primary" onclick="createGroup()">Create a group</button>'+list.map(group=>{let manager=group.my_role==='creator'||group.my_role==='subcreator';let controls=group.my_role?`<div class="group-actions"><button onclick="groupAction('deposit','${group.id}')">Deposit to group</button>${manager?`<button onclick="groupAction('withdraw','${group.id}')">Withdraw</button><button onclick="groupAction('transfer','${group.id}')">Transfer to member</button>`:''}</div>`:`<div class="group-actions"><button onclick="groupAction('join','${group.id}')">Request to join</button></div>`;let requests=manager?(group.join_requests||[]).map(request=>`<div class="group-request">${request.name} wants to join <button onclick="groupPost('respond',{group_id:'${group.id}',name:user.name,email:'${request.email}',action:'accept'})">Accept</button><button onclick="groupPost('respond',{group_id:'${group.id}',name:user.name,email:'${request.email}',action:'reject'})">Reject</button></div>`).join(''):'';let image=group.group_image?`<img class="group-picture" src="${group.group_image}" alt="Group profile picture">`:'<span class="group-picture-fallback">●</span>';return `<div class="group-card"><div class="group-heading">${image}<div><h3>${group.name}</h3><div>Group balance: $${Number(group.balance).toFixed(2)} · ${group.members.length} member(s)</div></div></div>${group.my_role==='creator'?`<button class="link" onclick="uploadGroupImage(${JSON.stringify(group).replace(/"/g,'&quot;')})">Change group picture</button>`:''}${controls}${requests}</div>`}).join('')||'<p>No groups yet.</p>'}
function uploadGroupImage(group){let input=document.createElement('input');input.type='file';input.accept='image/png,image/jpeg,image/gif,image/webp';input.onchange=()=>{let file=input.files[0];if(!file)return;if(file.size>300000)return toast('Choose an image under 300 KB');let reader=new FileReader();reader.onload=async()=>{let response=await fetch('/api/groups/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,group_id:group.id,group_image:reader.result})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;render();toast('Group picture saved')};reader.readAsDataURL(file)};input.click()}
async function searchPeople(){let query=document.getElementById('people-search-input').value.trim();let response=await fetch('/api/friends/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,query})});let data=await response.json();if(!response.ok)return toast(data.error);let results=document.getElementById('people-results');results.innerHTML=data.results.map(person=>`<div class="person-result">${person.profile_image?`<img src="${person.profile_image}" alt="Profile picture">`:`<span class="member-icon">${person.profile_icon||'●'}</span>`}<div><strong>${person.display_name}</strong>${person.country?`<small>${person.country}</small>`:''}</div>${person.is_friend?'<span class="friend-status">Friends</span>':`<button onclick="addFriend('${person.email}')">Add friend</button>`}</div>`).join('')||'<p>No people found.</p>'}
async function addFriend(email){let response=await fetch('/api/friends/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,email})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;searchPeople();toast('Friend added')}
let activeMessageFriend='';let activeMessageGroup='';
async function openMessages(email){activeMessageFriend=email;activeMessageGroup='';showMessagesTab();await loadMessages()}
async function openGroupMessages(id){activeMessageGroup=id;activeMessageFriend='';showMessagesTab();await loadMessages()}
async function loadMessages(){let panel=document.getElementById('messages-panel');if(!panel)return;let target=activeMessageGroup?'group='+encodeURIComponent(activeMessageGroup):'friend='+encodeURIComponent(activeMessageFriend);let response=await fetch('/api/messages?'+target);let data=await response.json();if(!response.ok)return toast(data.error);let conversation=document.getElementById('message-list');conversation.innerHTML=data.messages.map(message=>`<div class="message-row ${message.sender_email===user.email?'sent':'received'}"><small>${message.sender_name} · ${message.created_at}</small>${message.type==='photo'?`<img class="message-media" src="${message.content}" alt="Photo">`:message.type==='video'?`<video class="message-media" src="${message.content}" controls></video>`:`<div>${message.type==='sticker'?'🏷️ ':''}${message.content}</div>`}</div>`).join('')||'<p>No messages yet.</p>';conversation.scrollTop=conversation.scrollHeight}
async function sendMessage(type='text',content=null,fileName=''){if(!activeMessageFriend&&!activeMessageGroup)return toast('Choose a chat first');if(content===null){content=document.getElementById('message-input').value.trim();if(!content)return}let response=await fetch('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,friend_email:activeMessageFriend,group_id:activeMessageGroup,type,content,file_name:fileName})});let data=await response.json();if(!response.ok)return toast(data.error);document.getElementById('message-input').value='';loadMessages()}
function sendEmoji(value){document.getElementById('message-input').value+=value;document.getElementById('message-input').focus()}
function sendSticker(value){sendMessage('sticker',value)}
function chooseMessageFile(){let input=document.createElement('input');input.type='file';input.accept='image/*,video/*';input.onchange=()=>{let file=input.files[0];if(!file)return;if(file.size>250*1024*1024)return toast('Files must be 250 MB or smaller');let type=file.type.startsWith('video/')?'video':'photo';let reader=new FileReader();reader.onload=()=>sendMessage(type,reader.result,file.name);reader.readAsDataURL(file)};input.click()}
function showMessagesTab(){document.querySelectorAll('main > *:not(.top)').forEach(item=>{item.style.display=item.id==='messages-panel'?'block':'none'});document.querySelectorAll('.nav button').forEach(item=>item.classList.toggle('active',item.dataset.tab==='messages'))}
function setupMessagesTab(){let nav=document.querySelector('.nav');if(!nav||nav.querySelector('[data-tab="messages"]'))return;let button=document.createElement('button');button.textContent='Messages';button.dataset.tab='messages';button.onclick=()=>{showMessagesTab();if(!activeMessageFriend&&user.friends?.[0])openMessages(user.friends[0].email)};nav.insertBefore(button,nav.children[1])}
function setupMessagePanel(){let panel=document.getElementById('messages-panel');if(!panel)return;if(!document.getElementById('message-style')){let style=document.createElement('style');style.id='message-style';style.textContent='#messages-panel{background:linear-gradient(145deg,#fff 0%,#fbf9fd 100%)}#messages-panel>h2{font-size:24px;margin-bottom:4px}.message-friends,.message-groups{display:flex;flex-wrap:nowrap;gap:8px;overflow-x:auto;padding:8px 0 12px;margin:0}.message-friends:before,.message-groups:before{flex:0 0 58px;align-self:center;color:var(--bank-muted);font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}.message-friends:before{content:"Friends"}.message-groups:before{content:"Groups"}.message-friends button,.message-groups button{display:inline-flex;align-items:center;gap:7px;flex:0 0 auto;min-height:38px;padding:8px 12px;border:1px solid #e4ddec;border-radius:20px;background:#fff;color:var(--bank-ink);box-shadow:0 3px 10px #32194f0c;font-weight:600;white-space:nowrap}.message-friends button:hover,.message-groups button:hover{border-color:var(--bank-purple-mid);background:var(--bank-lilac);transform:translateY(-1px)}.message-groups button img{width:24px;height:24px}.message-list{height:min(48vh,420px);min-height:260px;margin:12px 0 16px;padding:18px;background:#f7f4fa;border:1px solid #e8e0ef;border-radius:14px;box-shadow:inset 0 1px 2px #32194f08}.message-list>p{display:grid;place-items:center;height:100%;margin:0;color:var(--bank-muted);font-size:13px}.message-row{max-width:min(78%,520px);padding:10px 13px;margin:9px 0;border:1px solid #e8e0ef;border-radius:14px 14px 14px 4px;background:#fff;box-shadow:0 3px 10px #32194f0a;line-height:1.45}.message-row.sent{border-color:transparent;border-radius:14px 14px 4px 14px;background:var(--bank-purple);color:#fff;box-shadow:0 5px 14px #32194f20}.message-row small{margin-bottom:3px;color:var(--bank-muted);font-size:10px}.message-row.sent small{color:#e8dcf2}.message-tools{align-items:center;padding:8px;border:1px solid #e4ddec;border-radius:12px;background:#fff;box-shadow:0 5px 16px #32194f0c}.message-tools button{min-width:38px;height:38px;padding:8px;border-radius:8px}.message-tools button[onclick="sendMessage()"]{min-width:72px;background:var(--bank-purple);color:#fff}.message-tools input{height:38px;border:0;background:#faf8fc;border-radius:8px;outline:none}.message-tools input:focus{box-shadow:0 0 0 2px #8e5ab140}@media(max-width:700px){.message-list{height:52vh;min-height:220px;padding:12px}.message-row{max-width:88%}.message-tools{flex-wrap:wrap}.message-tools input{order:-1;flex-basis:100%}.message-tools button[onclick="sendMessage()"]{margin-left:auto}}';document.head.append(style)}let friends=panel.querySelector('.message-friends');if(friends)friends.innerHTML=(user.friends||[]).map(friend=>`<button onclick="openMessages('${friend.email}')">${friend.profile_icon||'●'} ${friend.display_name}</button>`).join('')||'<p>Add friends to start messaging.</p>';let groups=panel.querySelector('.message-groups');if(!groups){groups=document.createElement('div');groups.className='message-groups';panel.insertBefore(groups,panel.querySelector('.message-list'))}groups.innerHTML=(user.groups||[]).map(group=>`<button onclick="openGroupMessages('${group.id}')">${group.group_image?`<img src="${group.group_image}">`:'●'} ${group.name}</button>`).join('')||'<p>No group chats yet.</p>'}
function showSearchTab(){document.querySelectorAll('main > *:not(.top)').forEach(item=>{item.style.display=item.id==='search-panel'?'block':'none'});document.querySelectorAll('.nav button').forEach(item=>item.classList.toggle('active',item.dataset.tab==='search'));document.getElementById('people-search-input')?.focus()}
function renderNotifications(){let panel=document.getElementById('notice-panel');if(!panel)return;let count=document.querySelector('.notice-count');if(count){count.textContent=user.unread_notifications||'';count.style.display=user.unread_notifications?'block':'none'}panel.innerHTML='<strong>Notifications</strong>'+((user.notifications||[]).map(item=>`<div class="notice-item ${item.read?'':'unread'}" onclick="readNotification('${item.id}')"><strong>${item.title}</strong><span>${item.message}</span><small>${item.created_at}</small></div>`).join('')||'<p>No notifications yet.</p>')}
function toggleNotifications(){let panel=document.getElementById('notice-panel');if(!panel)return;panel.classList.toggle('open');renderNotifications();if(panel.classList.contains('open'))requestPhoneNotifications()}
async function requestPhoneNotifications(){if('Notification' in window&&Notification.permission==='default')await Notification.requestPermission()}
async function readNotification(id){let response=await fetch('/api/notifications/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,id})});if(response.ok){user=await response.json();renderNotifications();render()}}
async function refreshNotifications(){let response=await fetch('/api/notifications');if(!response.ok)return;let data=await response.json();let known=new Set((user.notifications||[]).map(item=>item.id));data.notifications.filter(item=>!known.has(item.id)&&!item.read).forEach(item=>{if('Notification' in window&&Notification.permission==='granted')new Notification(item.title,{body:item.message});});user.notifications=data.notifications;user.unread_notifications=data.unread;renderNotifications()}
function setupNotifications(){let header=document.querySelector('.top');if(!header||document.querySelector('.notice-wrap'))return;let wrap=document.createElement('div');wrap.className='notice-wrap';wrap.innerHTML=`<button class="notice-button" onclick="toggleNotifications()" aria-label="Notifications">&#128276;</button><span class="notice-count"></span><div class="notice-panel" id="notice-panel"></div>`;let bell=wrap.querySelector('.notice-button');bell.style.color='#32194f';bell.style.background='#e8dcf2';header.insertBefore(wrap,header.querySelector('.avatar'));renderNotifications();requestPhoneNotifications()}
function showGroupTab(){document.querySelectorAll('main > *:not(.top)').forEach(item=>{item.style.display=item.id==='groups-panel'||item.id==='auto-deposit-panel'?'block':'none'});document.querySelectorAll('.nav button').forEach(item=>item.classList.toggle('active',item.dataset.tab==='groups'))}
function showOverviewTab(){document.querySelectorAll('main > *:not(.top)').forEach(item=>{item.style.display=item.id==='groups-panel'||item.id==='auto-deposit-panel'||item.id==='search-panel'||item.id==='messages-panel'?'none':''});document.querySelectorAll('.nav button').forEach(item=>item.classList.toggle('active',item.dataset.tab==='overview'))}
function setupSearchTab(){let nav=document.querySelector('.nav');if(!nav||nav.querySelector('[data-tab="search"]'))return;let searchButton=document.createElement('button');searchButton.textContent='Search';searchButton.dataset.tab='search';searchButton.onclick=showSearchTab;nav.insertBefore(searchButton,nav.children[1])}
function setupGroupTab(){if(!document.getElementById('switch-style')){let style=document.createElement('style');style.id='switch-style';style.textContent='.auto-deposit{display:flex;align-items:center;justify-content:space-between;gap:16px}.auto-deposit select{min-width:180px;padding:9px;border:1px solid #d9d0e2;border-radius:7px;background:#fff}.switch{position:relative;display:inline-block;width:46px;height:26px;flex:0 0 auto}.switch input{opacity:0;width:0;height:0}.slider{position:absolute;inset:0;border-radius:20px;background:#cfc7d8;cursor:pointer;transition:.2s}.slider:before{content:"";position:absolute;width:20px;height:20px;left:3px;top:3px;border-radius:50%;background:#fff;box-shadow:0 1px 3px #32194f35;transition:.2s}.switch input:checked+.slider{background:#21745a}.switch input:checked+.slider:before{transform:translateX(20px)}';document.head.append(style)}let nav=document.querySelector('.nav');if(!nav||nav.querySelector('[data-tab="groups"]'))return;let groupsButton=document.createElement('button');groupsButton.textContent='Groups';groupsButton.dataset.tab='groups';groupsButton.onclick=showGroupTab;nav.insertBefore(groupsButton,nav.children[1]);let overview=nav.querySelector('.active');if(overview){overview.dataset.tab='overview';overview.onclick=showOverviewTab}}
function openProfileSettings(){let display_name=prompt('Public username:',user.display_name||user.name);if(display_name===null)return;let profile_icon=prompt('Icon: ● ◆ ✦ ★ ◈',user.profile_icon||'●');if(profile_icon===null)return;let response=fetch('/api/profile/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,display_name,profile_icon})}).then(result=>result.json());response.then(data=>{if(data.error)return toast(data.error);user=data;render();toast('Profile settings saved')})}
function showSettingsTab(tab='privacy'){let old=document.getElementById('settings-modal');if(old)old.remove();let modal=document.createElement('div');modal.id='settings-modal';modal.className='settings-modal';modal.innerHTML=`<div class="settings-window"><button class="profile-close" onclick="this.closest('.settings-modal').remove()">×</button><h2>Settings</h2><nav class="settings-tabs"><button data-settings="privacy" onclick="renderSettings('privacy')">Privacy</button><button data-settings="appearance" onclick="renderSettings('appearance')">Appearance</button><button data-settings="history" onclick="renderSettings('history')">History</button></nav><div id="settings-content"></div></div>`;document.body.append(modal);renderSettings(tab)}
function applyTheme(theme){let themes={purple:['#32194f','#56337a','#eee8f5','#211b2b','#766e82','#fff'],blue:['#164e78','#246b9f','#e3f1fb','#183044','#587083','#fff'],green:['#176044','#27815d','#e4f4ec','#17352a','#5e786c','#fff'],red:['#8f2638','#b33c51','#fbe8ec','#3e1b24','#80616a','#fff'],wine:['#641c32','#8d304c','#f5e3e8','#351520','#80606b','#fff'],orange:['#a44916','#d16a28','#fff0e1','#422616','#806b5c','#fff'],yellow:['#806000','#a17c00','#fff8d9','#42370b','#807958','#fff'],pink:['#a33d70','#c75b91','#fbe8f2','#431d32','#806579','#fff'],teal:['#11686a','#218d8d','#e1f5f3','#163a3a','#5d7979','#fff'],cyan:['#126277','#218aa3','#e2f5fa','#163842','#5c7780','#fff'],indigo:['#35418c','#5664bd','#e9eafd','#202648','#646b8c','#fff'],lavender:['#76529b','#9870c2','#f0eafa','#30203d','#766982','#fff'],plum:['#6b285f','#93417f','#f6e7f2','#35192f','#806477','#fff'],violet:['#542a87','#7545b1','#eee5fb','#2d1d45','#746887','#fff'],orchid:['#914e9f','#b56bc2','#f8eafa','#3c2142','#806b82','#fff'],brown:['#70452d','#976044','#f6ece5','#39251c','#7c6c63','#fff'],black:['#24242b','#4a4a55','#eeeeF2','#1c1c21','#6b6b76','#fff'],cool_black:['#17202b','#34485e','#e7edf3','#17202b','#657484','#fff']};let colors=themes[theme]||themes.purple;let root=document.documentElement;['--bank-purple','--bank-purple-mid','--bank-lilac','--bank-ink','--bank-muted','--bank-white'].forEach((name,index)=>root.style.setProperty(name,colors[index]))}
async function saveSettingsUsername(){let display_name=document.getElementById('settings-username').value.trim();let response=await fetch('/api/profile/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,display_name,profile_icon:user.profile_icon||'●',profile_image:user.profile_image||'',theme:user.theme||'purple'})});let data=await response.json();let help=document.getElementById('username-help');if(!response.ok){if(help)help.textContent=data.error;return}user=data;if(help)help.textContent='Username saved.';render();showSettingsTab('privacy')}
async function saveTheme(theme){let response=await fetch('/api/profile/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,display_name:user.display_name||user.name,profile_icon:user.profile_icon||'●',profile_image:user.profile_image||'',theme})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;applyTheme(theme);renderSettings('appearance');toast('Theme saved')}
async function savePrivacySettings(){let response=await fetch('/api/profile/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,display_name:document.getElementById('settings-username').value.trim(),profile_icon:user.profile_icon||'●',profile_image:user.profile_image||'',theme:user.theme||'purple',country:document.getElementById('settings-country').value,show_country:document.getElementById('settings-show-country').checked,telephone:document.getElementById('settings-telephone').value.trim()})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;render();showSettingsTab('privacy');toast('Privacy settings saved')}
function renderSettings(tab){document.querySelectorAll('.settings-tabs button').forEach(button=>button.classList.toggle('active',button.dataset.settings===tab));let content=document.getElementById('settings-content');if(!content)return;if(tab==='privacy'){let countries=['','United States','Canada','Mexico','Brazil','Argentina','United Kingdom','Ireland','France','Germany','Spain','Italy','Portugal','Netherlands','Belgium','Switzerland','Austria','Poland','Czechia','Sweden','Norway','Denmark','Finland','Ukraine','Romania','Greece','Turkey','South Africa','Egypt','Nigeria','India','China','Japan','South Korea','Australia','New Zealand','Other'];content.innerHTML=`<h3>Privacy</h3><p>Your real first and last name stay private from group members.</p><label>Public username</label><input class="settings-input" id="settings-username" value="${user.display_name||user.name}" maxlength="30"><label>Country</label><select class="settings-input" id="settings-country">${countries.map(country=>`<option value="${country}" ${country===user.country?'selected':''}>${country||'Choose country'}</option>`).join('')}</select><label class="privacy-switch"><input id="settings-show-country" type="checkbox" ${user.show_country?'checked':''}> Show my country on my public profile</label><label>Telephone number <small>(optional)</small></label><input class="settings-input" id="settings-telephone" type="tel" value="${user.telephone||''}" placeholder="+1 555 123 4567"><button class="settings-action" onclick="savePrivacySettings()">Save privacy settings</button><p id="username-help">Your telephone number stays private.</p>`}else if(tab==='appearance'){let themes={purple:['Purple','#32194f'],blue:['Ocean blue','#164e78'],green:['Forest green','#176044'],red:['Ruby red','#8f2638'],orange:['Orange','#a44916'],yellow:['Golden yellow','#806000'],pink:['Pink','#a33d70'],teal:['Teal','#11686a'],cyan:['Cyan','#126277'],indigo:['Indigo','#35418c'],brown:['Brown','#70452d'],black:['Charcoal','#24242b']};content.innerHTML='<h3>Appearance</h3><p>Choose a color theme for your account.</p><div class="theme-grid">'+Object.entries(themes).map(([key,value])=>`<button class="theme-choice ${user.theme===key?'selected':''}" onclick="saveTheme('${key}')"><span style="background:${value[1]}"></span>${value[0]}</button>`).join('')+'</div>'}else{let items=[...(user.history||[])];(user.groups||[]).forEach(group=>items.push(...(group.history||[]).map(item=>`[${group.name}] ${item}`)));items.sort().reverse();content.innerHTML='<h3>Complete history</h3><div class="settings-history">'+(items.map(item=>`<div>${item}</div>`).join('')||'<p>No history yet.</p>')+'</div>'}}
function showProfile(){let old=document.getElementById('profile-modal');if(old)old.remove();let modal=document.createElement('div');modal.id='profile-modal';modal.className='profile-modal';modal.innerHTML=`<div class="profile-window"><button class="profile-close" onclick="document.getElementById('profile-modal').remove()">×</button><div class="profile-preview">${user.profile_image?`<img src="${user.profile_image}" alt="Profile picture">`:`<span>${user.profile_icon||'●'}</span>`}</div><h2>${user.display_name||user.name}</h2><p>Public profile</p>${user.show_country&&user.country?`<p>Lives in ${user.country}</p>`:''}<label class="upload-button">Upload profile picture<input id="profile-image-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp"></label></div>`;document.body.append(modal);document.getElementById('profile-image-input').onchange=uploadProfileImage}
function toggleProfileMenu(event){event.stopPropagation();document.querySelector('.profile-wrap')?.classList.toggle('menu-open')}
function closeProfileMenu(){document.querySelector('.profile-wrap')?.classList.remove('menu-open')}
function uploadProfileImage(event){let file=event.target.files[0];if(!file)return;if(file.size>300000)return toast('Choose an image under 300 KB');let reader=new FileReader();reader.onload=async()=>{let response=await fetch('/api/profile/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,display_name:user.display_name||user.name,profile_icon:user.profile_icon||'●',profile_image:reader.result})});let data=await response.json();if(!response.ok)return toast(data.error);user=data;document.getElementById('profile-modal')?.remove();render();toast('Profile picture saved')};reader.readAsDataURL(file)}
function setupProfileMenu(){let avatar=document.querySelector('.avatar');if(!avatar||document.querySelector('.profile-wrap'))return;let wrap=document.createElement('div');wrap.className='profile-wrap';wrap.innerHTML=`<div class="profile-menu"><button onclick="showProfile()">Profile</button><button onclick="showSettingsTab()">Settings</button></div>`;avatar.parentNode.insertBefore(wrap,avatar);wrap.appendChild(avatar);avatar.onclick=toggleProfileMenu;if(user.profile_image){avatar.innerHTML=`<img src="${user.profile_image}" alt="Profile picture">`}else{avatar.textContent=user.profile_icon||'●'}document.addEventListener('click',closeProfileMenu);if(!document.getElementById('profile-style')){let style=document.createElement('style');style.id='profile-style';style.textContent='.profile-wrap{position:relative}.profile-menu{display:none;position:absolute;right:0;top:54px;width:130px;padding:6px;background:#fff;border:1px solid #e4ddec;border-radius:8px;box-shadow:0 10px 25px #32194f25;z-index:6}.profile-wrap.menu-open .profile-menu{display:grid;gap:3px}.profile-menu button{border:0;border-radius:5px;background:transparent;padding:8px;text-align:left;color:#32194f;cursor:pointer}.profile-menu button:hover{background:#f2ebf7}.profile-wrap img,.profile-preview img{width:100%;height:100%;object-fit:cover;border-radius:50%}.profile-modal,.settings-modal{position:fixed;inset:0;background:#21172b66;display:grid;place-items:center;z-index:20}.profile-window,.settings-window{position:relative;width:min(420px,calc(100% - 32px));padding:28px;background:#fff;border-radius:14px;box-shadow:0 18px 50px #21172b40}.profile-window{text-align:center}.profile-window h2{margin:14px 0 2px;color:#211b2b}.profile-window p{margin:0 0 18px;color:#766e82}.profile-close{position:absolute;right:12px;top:8px;border:0;background:transparent;font-size:24px;color:#766e82;cursor:pointer}.profile-preview{width:104px;height:104px;margin:auto;border-radius:50%;display:grid;place-items:center;background:#e8dcf2;color:#32194f;font-size:42px;overflow:hidden}.upload-button,.settings-action{display:inline-block;padding:10px 14px;border:0;border-radius:7px;background:#32194f;color:#fff;cursor:pointer}.upload-button input{display:none}.settings-tabs{display:flex;gap:6px;border-bottom:1px solid #e4ddec;margin:15px 0}.settings-tabs button{border:0;background:transparent;padding:9px;color:#766e82;cursor:pointer}.settings-tabs button.active{color:#32194f;border-bottom:2px solid #32194f}.settings-history{max-height:300px;overflow:auto}.settings-history div{padding:9px 0;border-bottom:1px solid #eee9f2;color:#514a5b;font-size:13px}';document.head.append(style)}}
function setupMemberImageStyles(){if(document.getElementById('member-image-style'))return;let style=document.createElement('style');style.id='member-image-style';style.textContent='.member-identity{display:flex;align-items:center;gap:9px}.member-identity>img{width:30px;height:30px;object-fit:cover;border-radius:50%}.member-icon{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:#e8dcf2;color:#32194f}.person-result{display:flex;align-items:center;gap:10px;border-bottom:1px solid #eee9f2;padding:12px 0}.person-result>img{width:38px;height:38px;border-radius:50%;object-fit:cover}.person-result small{display:block;color:var(--bank-muted)}.person-result button{margin-left:auto;border:0;border-radius:6px;padding:8px 10px;background:var(--bank-purple);color:#fff;cursor:pointer}.friend-status{margin-left:auto;color:var(--bank-green);font-size:13px}.message-friends,.message-groups{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}.message-friends button,.message-groups button,.message-tools button{border:0;border-radius:6px;padding:8px 10px;background:#eee8f5;color:var(--bank-purple);cursor:pointer}.message-groups button img{width:22px;height:22px;vertical-align:middle;border-radius:50%;object-fit:cover}.message-list{height:300px;overflow:auto;border:1px solid #eee9f2;border-radius:8px;padding:10px;margin:14px 0}.message-row{max-width:75%;padding:8px 10px;margin:7px 0;border-radius:9px;background:#f4f0f7}.message-row.sent{margin-left:auto;background:var(--bank-lilac)}.message-row small{display:block;font-size:10px;color:var(--bank-muted)}.message-media{max-width:100%;max-height:220px;border-radius:6px}.message-tools{display:flex;flex-wrap:wrap;gap:6px}.message-tools input{flex:1;min-width:160px;padding:9px;border:1px solid #d9d0e2;border-radius:7px}.group-heading{display:flex;align-items:center;gap:12px;margin-bottom:8px}.group-heading h3{margin:0 0 3px}.group-picture,.group-picture-fallback{width:50px;height:50px;border-radius:50%;object-fit:cover}.group-picture-fallback{display:grid;place-items:center;background:#e8dcf2;color:#32194f;font-size:24px}.settings-input{display:block;width:100%;margin:8px 0 10px;padding:10px;border:1px solid #d9d0e2;border-radius:7px;font:15px "Trebuchet MS",Arial,sans-serif}.privacy-switch{display:flex;align-items:center;gap:8px;margin:10px 0 16px}.theme-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.theme-choice{display:flex;align-items:center;gap:8px;border:1px solid #e4ddec;border-radius:7px;background:#fff;padding:9px;text-align:left;color:#32194f;cursor:pointer}.theme-choice span{width:18px;height:18px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1px #bbb}.theme-choice.selected{border-color:#32194f;box-shadow:0 0 0 2px #32194f33}';document.head.append(style)}
const bankRender=render;render=function(){applyTheme(user.theme||'purple');bankRender();setupNotifications();setupSearchTab();setupMessagesTab();setupGroupTab();setupProfileMenu();setupMemberImageStyles();if(!document.getElementById('search-panel'))document.querySelector('main')?.insertAdjacentHTML('beforeend','<section class="panel history" id="search-panel"><h2>Find people</h2><form onsubmit="event.preventDefault();searchPeople()"><input class="settings-input" id="people-search-input" placeholder="Search by email, name, or username"><button class="primary">Search</button></form><div id="people-results"></div></section>');if(!document.getElementById('messages-panel'))document.querySelector('main')?.insertAdjacentHTML('beforeend','<section class="panel history" id="messages-panel"><h2>Messages</h2><div class="message-friends"></div><div id="message-list" class="message-list"></div><div class="message-tools"><button onclick="sendEmoji(\'😀\')">😀</button><button onclick="sendEmoji(\'😂\')">😂</button><button onclick="sendSticker(\'⭐\')">⭐</button><button onclick="sendSticker(\'🎉\')">🎉</button><button onclick="chooseMessageFile()">Photo/video</button><input id="message-input" placeholder="Write a message"><button class="primary" onclick="sendMessage()">Send</button></div></section>');if(!document.getElementById('groups-panel'))document.querySelector('main')?.insertAdjacentHTML('beforeend','<section class="panel history" id="groups-panel"></section>');setupMessagePanel();showOverviewTab();loadGroups();loadAutoDeposit();setTimeout(showOverviewTab,100)}
function addExtraThemes(){let grid=document.querySelector('.theme-grid');if(!grid)return;let themes={wine:['Wine red','#641c32'],lavender:['Lavender purple','#76529b'],plum:['Plum purple','#6b285f'],violet:['Violet purple','#542a87'],orchid:['Orchid purple','#914e9f'],cool_black:['Cool black','#17202b']};Object.entries(themes).forEach(([key,value])=>{if(grid.querySelector('[data-theme="'+key+'"]'))return;let button=document.createElement('button');button.className='theme-choice '+(user.theme===key?'selected':'');button.dataset.theme=key;button.innerHTML='<span style="background:'+value[1]+'"></span>'+value[0];button.onclick=()=>saveTheme(key);grid.append(button)})}
document.addEventListener('click',event=>{let choice=event.target.closest('.theme-choice');if(choice){let match=(choice.getAttribute('onclick')||'').match(/saveTheme\('([^']+)'\)/);if(match)localStorage.setItem('bank_theme',match[1])}});setInterval(()=>{if(!user)applyTheme(localStorage.getItem('bank_theme')||'purple')},1000);setInterval(refreshNotifications,5000);setInterval(addExtraThemes,500)
async function saveAutoDeposit(){let toggle=document.getElementById('auto-deposit-toggle');let select=document.getElementById('auto-deposit-group');let group_id=toggle.checked?select.value:'';let response=await fetch('/api/profile/auto-deposit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:user.name,group_id})});let data=await response.json();if(!response.ok){toast(data.error);return}user=data;toast(toggle.checked?'Auto deposit enabled':'Auto deposit disabled')}
async function loadAutoDeposit(){let response=await fetch('/api/groups');if(!response.ok)return;let list=await response.json();let existing=document.getElementById('auto-deposit-panel');if(existing)existing.remove();let panel=document.createElement('section');panel.className='panel history';panel.id='auto-deposit-panel';let selected=user.auto_deposit_group_id||'';panel.innerHTML=`<h2>Automatic deposits</h2><div class="auto-deposit"><span>Send new income to a group</span><label class="switch"><input id="auto-deposit-toggle" type="checkbox" ${selected?'checked':''} onchange="saveAutoDeposit()"><span class="slider"></span></label><select id="auto-deposit-group" onchange="saveAutoDeposit()" ${selected?'':'disabled'}><option value="">Choose a group</option>${list.filter(group=>group.my_role).map(group=>`<option value="${group.id}" ${group.id===selected?'selected':''}>${group.name}</option>`).join('')}</select></div>`;document.querySelector('main')?.insertBefore(panel,document.getElementById('groups-panel'));let toggle=document.getElementById('auto-deposit-toggle');let select=document.getElementById('auto-deposit-group');toggle.onchange=()=>{select.disabled=!toggle.checked;saveAutoDeposit()}}
async function manageGroup(group){let email=prompt('Member email:');if(!email)return;let action=prompt('Type role, kick, or member:','role');if(action==='kick')return groupPost('kick',{group_id:group.id,name:user.name,email});let role=action==='member'?'member':'subcreator';return groupPost('role',{group_id:group.id,name:user.name,email,role})}
const bankLoadGroups=loadGroups;loadGroups=async function(){await bankLoadGroups();for(const group of (user.groups||[])){let card=[...document.querySelectorAll('.group-card')].find(item=>item.querySelector('h3')?.textContent===group.name);if(!card)continue;let members=document.createElement('div');members.innerHTML='<strong>Members</strong>'+group.members.map(member=>`<div class="group-member"><span class="member-identity">${member.image?`<img src="${member.image}" alt="Profile picture">`:`<span class="member-icon">${member.icon||'●'}</span>`}<span>${member.name}</span></span><span class="role-${member.role}">${member.role}</span></div>`).join('');card.append(members);if(group.my_role==='creator'||group.my_role==='subcreator'){let button=document.createElement('button');button.textContent='Manage members';button.onclick=()=>manageGroup(group);card.querySelector('.group-actions')?.append(button)}if(group.my_role==='creator'){let button=document.createElement('button');button.textContent='Delete group';button.onclick=()=>deleteGroup(group);card.querySelector('.group-actions')?.append(button)}}}
</script></body></html>'''
PAGE = PAGE.replace("showLogin();", "applyTheme(localStorage.getItem('bank_theme')||'purple');showLogin();", 1)
PAGE = PAGE.replace(
    "Create a new account</button></p>",
    "Create a new account</button></p><p><button class=\"link\" onclick=\"showForgotPassword(event)\">Forgot password?</button></p>",
    1,
)
PAGE = PAGE.replace(
    "</script></body></html>",
    """function showForgotPassword(event){if(event)event.preventDefault();app.innerHTML='<section class=\"login\"><div class=\"eyebrow\">Northstar Bank</div><h1>Recover your account.</h1><p>Enter your account email and we will send a reset link if it exists.</p><form id=\"forgot-password\"><div class=\"field\"><label>Email</label><input name=\"email\" type=\"email\" required></div><div class=\"error\"></div><button class=\"primary\">Send reset link</button></form><p><button class=\"link\" onclick=\"showLogin()\">Back to login</button></p></section>';document.getElementById('forgot-password').onsubmit=forgotPassword}
async function forgotPassword(event){event.preventDefault();let body=Object.fromEntries(new FormData(event.target));let response=await fetch('/api/password/forgot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let data=await response.json();event.target.querySelector('.error').textContent=data.message||data.error}
</script></body></html>""",
    1,
)


class Handler(BaseHTTPRequestHandler):
    def log_request_ip(self):
        client_address = getattr(self, "client_address", ())
        ip_address = client_address[0] if client_address else "unknown"
        method = getattr(self, "command", "UNKNOWN")
        entry = {
            "timestamp": current_date(),
            "ip": ip_address,
            "method": method,
            "path": self.path,
            "user": self.cookie_value("bank_user") or None,
        }
        with IP_LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
        print(f"[{entry['timestamp']}] {ip_address} {method} {self.path}")

    def send_html(self, body, status=200):
        body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            return {key: values[0] for key, values in urllib.parse.parse_qs(raw.decode()).items()}
        return json.loads(raw)

    def cookie_value(self, name):
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == name:
                return urllib.parse.unquote(value)
        return ""

    def do_GET(self):
        self.log_request_ip()
        parsed = urlparse(self.path)
        if parsed.path == "/reset-password":
            token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
            page = """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>Reset password</title></head><body style='font-family:Arial;max-width:420px;margin:15vh auto;padding:24px'><h1>Reset your password</h1><p>Choose a new password for your Northstar account.</p><form method='post' action='/api/password/reset'><input type='hidden' name='token' value='%s'><input name='password' type='password' minlength='6' placeholder='New password' required style='display:block;width:100%%;padding:12px;margin:12px 0'><button style='padding:12px 18px'>Set new password</button></form></body></html>""" % token
            return self.send_html(page)
        if parsed.path == "/auth/discord":
            required = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI")
            missing = [name for name in required if not os.getenv(name)]
            if missing:
                names = ", ".join(missing)
                return self.send_html(
                    "<h1>Discord connection is not configured.</h1>"
                    f"<p>Restart Northstar Bank with these environment settings: {names}.</p>",
                    503,
                )
            params = urllib.parse.parse_qs(parsed.query)
            linked_name = params.get("user", [""])[0].strip() or self.cookie_value("bank_user").strip()
            if not linked_name:
                return self.send_html("<h1>Discord account connection requires a logged-in bank account.</h1>", 400)
            state = secrets.token_urlsafe(24)
            oauth_states[state] = linked_name
            self.send_response(302)
            self.send_header("Location", discord_login_url(state))
            self.end_headers()
            return
        if parsed.path == "/auth/discord/callback":
            query = urllib.parse.parse_qs(parsed.query)
            state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            linked_name = oauth_states.pop(state, "") if state else ""
            if not linked_name:
                linked_name = self.cookie_value("bank_user").strip()
            if not code or not linked_name:
                return self.send_html("<h1>Discord account connection requires a logged-in bank account.</h1>", 400)
            try:
                token = discord_token(code)
                profile = discord_profile(token["access_token"])
                discord_id = profile["id"]
                user = find_user(linked_name)
                if not user:
                    return self.send_html("<h1>Bank account not found.</h1>", 404)
                user.discord_id = discord_id
                if profile.get("email") and not user.email:
                    user.email = profile["email"].strip().lower()
                save_users(users)
                account = json.dumps(public_user(user))
                cookie = urllib.parse.quote(user.full_name())
                page = PAGE.replace(
                    "showLogin();",
                    f"user={account};localStorage.setItem('bank_user',user.name);"
                    f"document.cookie='bank_user='+encodeURIComponent(user.name)+'; path=/; SameSite=Lax';"
                    "render();toast('Discord account connected');"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", f"bank_user={cookie}; Path=/; SameSite=Lax")
                body = page.encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
                return self.send_html(f"<h1>Discord connection failed.</h1><p>{error}</p>", 502)
        if urlparse(self.path).path == "/":
            page = PAGE.replace(
                "and notification history. Your optional telephone number is stored privately.",
                "and notification history, IP address, and security/access logs. Your optional telephone number is stored privately.")
            page = page.replace(
                "Your real first and last name stay private from group members.",
                "Your real first and last name stay private from group members. Your IP address is saved in access and security logs to help protect the service.")
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/groups":
            user = session_user(self)
            if not user:
                return self.send_json({"error": "Please log in."}, 401)
            visible = [public_group(group, user) for group in groups]
            self.send_json(visible)
        elif parsed.path == "/api/notifications":
            user = session_user(self)
            if not user:
                return self.send_json({"error": "Please log in."}, 401)
            self.send_json({"notifications": list(reversed(user.notifications[-30:])),
                            "unread": sum(1 for item in user.notifications
                                           if not item.get("read"))})
        elif parsed.path == "/api/messages":
            user = session_user(self)
            if not user:
                return self.send_json({"error": "Please log in."}, 401)
            query = urllib.parse.parse_qs(parsed.query)
            friend_email = query.get("friend", [""])[0].lower()
            group_id = query.get("group", [""])[0]
            group = group_for_user(group_id, user) if group_id else None
            if friend_email and friend_email not in user.friend_emails:
                return self.send_json({"error": "Messages are only available with friends."}, 403)
            if group_id and not group:
                return self.send_json({"error": "You are not a member of that group."}, 403)
            visible = [message for message in messages
                       if ((group_id and message.get("group_id") == group_id) or
                           (not group_id and (not friend_email or
                           {message["sender_email"].lower(), message["recipient_email"].lower()} ==
                           {user.email.lower(), friend_email})))]
            self.send_json({"messages": visible[-200:]})
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        self.log_request_ip()
        try:
            data = self.read_json()
            public_paths = {"/api/login", "/api/register", "/api/password/reset",
                            "/api/password/forgot"}
            if self.path not in public_paths:
                authenticated = session_user(self)
                if not authenticated:
                    return self.send_json({"error": "Please log in."}, 401)
                data["name"] = authenticated.full_name()
            if self.path == "/api/notifications/read":
                user = find_user(data.get("name", ""))
                if not user:
                    return self.send_json({"error": "Account not found."}, 404)
                notification_id = data.get("id", "")
                for item in user.notifications:
                    if not notification_id or item.get("id") == notification_id:
                        item["read"] = True
                save_users(users)
                return self.send_json(public_user(user))
            if self.path == "/api/password/reset":
                token = data.get("token", "")
                password = data.get("password", "")
                reset = recovery_tokens.pop(token, None)
                if not reset or reset["expires_at"] < time.time() or len(password) < 6:
                    return self.send_html("<h1>This reset link is invalid or expired.</h1>", 400)
                reset["user"].password = hash_password(password)
                save_users(users)
                return self.send_html("<h1>Password updated.</h1><p>You can now return to Northstar and log in.</p>")
            if self.path == "/api/password/forgot":
                email = data.get("email", "").strip().lower()
                account = find_user_by_email(email)
                if account:
                    token = secrets.token_urlsafe(32)
                    recovery_tokens[token] = {"user": account, "expires_at": time.time() + RECOVERY_TOKEN_TTL}
                    host = self.headers.get("Host", "127.0.0.1:8001")
                    base = os.getenv("PUBLIC_BASE_URL", f"http://{host}")
                    link = f"{base}/reset-password?token={urllib.parse.quote(token)}"
                    email_body = f"Use this link within 30 minutes to reset your password:\n\n{link}"
                    send_email(email, "Northstar password reset", email_body)
                    if not os.getenv("SMTP_HOST"):
                        print(f"Password reset link for {email}: {link}")
                return self.send_json({"message": "If that email has an account, a reset link has been sent."})
            if self.path == "/api/messages":
                sender = find_user(data.get("name", ""))
                recipient = find_user_by_email(data.get("friend_email", ""))
                group_id = data.get("group_id", "")
                group = group_for_user(group_id, sender) if sender and group_id else None
                group_exists = next((item for item in groups if item["id"] == group_id), None)
                message_type = data.get("type", "text")
                content = data.get("content", "")
                allowed_types = {"text", "emoji", "sticker", "photo", "video"}
                if group_id and group_exists and not group:
                    return self.send_json({"error": "You are not a member of that group."}, 403)
                if not sender or (not recipient and not group):
                    return self.send_json({"error": "Friend not found."}, 404)
                if not group and recipient.email.lower() not in sender.friend_emails:
                    return self.send_json({"error": "You can only message friends."}, 403)
                if message_type not in allowed_types:
                    return self.send_json({"error": "Unsupported message type."}, 400)
                if not isinstance(content, str) or not content:
                    return self.send_json({"error": "Message content is required."}, 400)
                if message_type in ("photo", "video"):
                    if not content.startswith("data:") or len(content) * 3 // 4 > MAX_MESSAGE_FILE_SIZE:
                        return self.send_json({"error": "Photos and videos must be 250 MB or smaller."}, 413)
                elif len(content) > 4000:
                    return self.send_json({"error": "Message is too long."}, 400)
                message = {"id": secrets.token_urlsafe(12),
                           "sender_email": sender.email,
                           "recipient_email": recipient.email if recipient else "",
                           "group_id": group_id,
                           "sender_name": sender.profile_username,
                           "type": message_type, "content": content,
                           "file_name": data.get("file_name", ""),
                           "created_at": current_date()}
                messages.append(message)
                save_messages(messages)
                if group:
                    notify_group(group, "New group message", f"{visible_name(sender)} posted in {group['name']}.", sender.email)
                else:
                    add_notification(recipient, "New message", f"{visible_name(sender)} sent you a {message_type}.")
                save_users(users)
                return self.send_json({"message": message})
            if self.path == "/api/login":
                user = find_user_login(data.get("name", ""))
                if not user:
                    return self.send_json({"error": "Name or password is incorrect."}, 401)
                password_matches, is_legacy_password = verify_password(
                    data.get("password", ""), user.password)
                if not password_matches:
                    return self.send_json({"error": "Name or password is incorrect."}, 401)
                if is_legacy_password:
                    user.password = hash_password(data.get("password", ""))
                    save_users(users)
                session = create_session(user)
                return self.send_json(public_user(user), 200, {"Set-Cookie": session_cookie(session)})
            if self.path == "/api/register":
                first_name = data.get("first_name", "").strip()
                last_name = data.get("last_name", "").strip()
                password = data.get("password", "")
                email = data.get("email", "").strip().lower()
                if data.get("accept_terms") != "yes":
                    return self.send_json({"error": "You must accept the community rules and data policy."}, 400)
                if not first_name or not last_name:
                    return self.send_json({"error": "Please enter your first and last name."}, 400)
                if len(password) < 6:
                    return self.send_json({"error": "Password must be at least 6 characters."}, 400)
                if not valid_email(email):
                    return self.send_json({"error": "Please enter a valid email address."}, 400)
                if find_user(f"{first_name} {last_name}"):
                    return self.send_json({"error": "That account already exists."}, 409)
                if any(existing.email.lower() == email for existing in users):
                    return self.send_json({"error": "That email is already in use."}, 409)
                user = User(first_name, last_name, hash_password(password), email=email,
                            terms_accepted_at=current_date())
                users.append(user)
                save_users(users)
                session = create_session(user)
                return self.send_json(public_user(user), 201, {"Set-Cookie": session_cookie(session)})
            if self.path == "/api/profile/email":
                user = find_user(data.get("name", ""))
                email = data.get("email", "").strip().lower()
                if not user:
                    return self.send_json({"error": "Account not found."}, 404)
                if not valid_email(email):
                    return self.send_json({"error": "Please enter a valid email address."}, 400)
                user.email = email
                save_users(users)
                send_email(email, "Northstar email added", 
                           f"An email was added to your Northstar account for {user.full_name()}.")
                notify_discord(f"Northstar update: {user.full_name()} changed their account email to {email}.")
                return self.send_json(public_user(user))
            if self.path == "/api/profile/auto-deposit":
                user = find_user(data.get("name", ""))
                group_id = data.get("group_id", "")
                if not user:
                    return self.send_json({"error": "Account not found."}, 404)
                if group_id and not group_for_user(group_id, user):
                    return self.send_json({"error": "Choose a group you belong to."}, 400)
                user.auto_deposit_group_id = group_id
                save_users(users)
                return self.send_json(public_user(user))
            if self.path == "/api/profile/settings":
                user = find_user(data.get("name", ""))
                display_name = data.get("display_name", "").strip()
                profile_icon = data.get("profile_icon", "●")
                profile_image = data.get("profile_image", "")
                theme = data.get("theme", user.theme)
                country = data.get("country", user.country).strip()
                show_country = bool(data.get("show_country", user.show_country))
                telephone = data.get("telephone", user.telephone).strip()
                if not user:
                    return self.send_json({"error": "Account not found."}, 404)
                if not display_name or len(display_name) > 30:
                    return self.send_json({"error": "Username must be 1 to 30 characters."}, 400)
                if any(existing is not user and
                       existing.profile_username.casefold() == display_name.casefold()
                       for existing in users):
                    return self.send_json({"error": "That public username is already taken."}, 409)
                if profile_icon not in ("●", "◆", "✦", "★", "◈"):
                    return self.send_json({"error": "Choose one of the available icons."}, 400)
                if theme not in THEMES:
                    return self.send_json({"error": "Choose a valid appearance theme."}, 400)
                if country and country not in COUNTRIES:
                    return self.send_json({"error": "Choose a valid country."}, 400)
                if show_country and not country:
                    return self.send_json({"error": "Choose a country before showing it."}, 400)
                if telephone and not re.fullmatch(r"[+0-9() .-]{7,25}", telephone):
                    return self.send_json({"error": "Enter a valid telephone number."}, 400)
                if profile_image and (not re.fullmatch(r"data:image/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+", profile_image)
                                     or len(profile_image) > 400000):
                    return self.send_json({"error": "Profile image must be a supported image under 300 KB."}, 400)
                user.profile_username = display_name
                user.profile_icon = profile_icon
                user.profile_image = profile_image
                if profile_image:
                    profile_images[user.email] = profile_image
                else:
                    profile_images.pop(user.email, None)
                save_profile_images(profile_images)
                user.theme = theme
                user.country = country
                user.show_country = show_country
                user.telephone = telephone
                save_users(users)
                return self.send_json(public_user(user))
            if self.path == "/api/friends/search":
                viewer = find_user(data.get("name", ""))
                query = data.get("query", "").strip().casefold()
                if not viewer:
                    return self.send_json({"error": "Account not found."}, 404)
                if not query:
                    return self.send_json({"results": []})
                matches = [public_profile(candidate, viewer) for candidate in users
                           if candidate is not viewer and
                           (query in candidate.email.casefold()
                            or query in candidate.full_name().casefold()
                            or query in candidate.profile_username.casefold())]
                return self.send_json({"results": matches[:20]})
            if self.path == "/api/friends/add":
                user = find_user(data.get("name", ""))
                friend = find_user_by_email(data.get("email", ""))
                if not user or not friend:
                    return self.send_json({"error": "Person not found."}, 404)
                if user is friend:
                    return self.send_json({"error": "You cannot add yourself."}, 400)
                if friend.email.lower() not in user.friend_emails:
                    user.friend_emails.append(friend.email)
                if user.email.lower() not in friend.friend_emails:
                    friend.friend_emails.append(user.email)
                add_notification(friend, "New friend", f"{visible_name(user)} added you as a friend.")
                save_users(users)
                return self.send_json(public_user(user))
            if self.path == "/api/groups/create":
                creator = find_user(data.get("name", ""))
                name = data.get("group_name", "").strip()
                if not creator:
                    return self.send_json({"error": "Account not found."}, 404)
                if not name or len(name) > 60:
                    return self.send_json({"error": "Enter a group name up to 60 characters."}, 400)
                if any(group["name"].lower() == name.lower() for group in groups):
                    return self.send_json({"error": "That group name is already in use."}, 409)
                group = {"id": secrets.token_urlsafe(12), "name": name,
                         "creator_email": creator.email, "balance": 0.0,
                         "members": [{"email": creator.email, "name": creator.full_name(),
                                      "role": "creator"}], "join_requests": [], "history": []}
                groups.append(group)
                add_group_history(group, f"{visible_name(creator)} created the group")
                save_groups(groups)
                return self.send_json(public_user(creator), 201)
            if self.path == "/api/groups/delete":
                creator = find_user(data.get("name", ""))
                group = next((item for item in groups if item["id"] == data.get("group_id")), None)
                if not creator or not group or group["creator_email"].lower() != creator.email.lower():
                    return self.send_json({"error": "Only the group creator can delete this group."}, 403)
                for account in users:
                    if account.auto_deposit_group_id == group["id"]:
                        account.auto_deposit_group_id = ""
                groups.remove(group)
                messages[:] = [message for message in messages
                               if message.get("group_id") != group["id"]]
                save_groups(groups)
                save_messages(messages)
                save_users(users)
                return self.send_json(public_user(creator))
            if self.path == "/api/groups/profile":
                creator = find_user(data.get("name", ""))
                group = next((item for item in groups if item["id"] == data.get("group_id")), None)
                image = data.get("group_image", "")
                if not creator or not group or group["creator_email"].lower() != creator.email.lower():
                    return self.send_json({"error": "Only the group creator can change this picture."}, 403)
                if image and (not re.fullmatch(r"data:image/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+", image)
                              or len(image) > 400000):
                    return self.send_json({"error": "Group image must be a supported image under 300 KB."}, 400)
                group["group_image"] = image
                add_group_history(group, f"{visible_name(creator)} changed the group picture")
                save_groups(groups)
                return self.send_json(public_user(creator))
            if self.path == "/api/groups/join":
                user = find_user(data.get("name", ""))
                group = next((item for item in groups if item["id"] == data.get("group_id")), None)
                if not user or not group:
                    return self.send_json({"error": "Account or group not found."}, 404)
                if group_member(group, user.email):
                    return self.send_json({"error": "You are already in this group."}, 400)
                if any(item["email"].lower() == user.email.lower() for item in group["join_requests"]):
                    return self.send_json({"error": "Your join request is already pending."}, 409)
                group["join_requests"].append({"email": user.email, "name": visible_name(user),
                                                "icon": user.profile_icon})
                add_group_history(group, f"{visible_name(user)} requested to join")
                notify_group(group, "New join request", f"{visible_name(user)} requested to join {group['name']}.")
                save_groups(groups)
                save_users(users)
                return self.send_json(public_user(user))
            if self.path == "/api/groups/respond":
                manager = find_user(data.get("name", ""))
                group = group_for_user(data.get("group_id", ""), manager) if manager else None
                if not group or not can_manage_group(group, manager):
                    return self.send_json({"error": "Only the creator or a sub-creator can manage requests."}, 403)
                email = data.get("email", "").strip().lower()
                request = next((item for item in group["join_requests"]
                                if item["email"].lower() == email), None)
                if not request:
                    return self.send_json({"error": "Join request not found."}, 404)
                group["join_requests"].remove(request)
                if data.get("action") == "accept":
                    group["members"].append({"email": request["email"], "name": request["name"],
                                              "role": "member"})
                    joined = find_user_by_email(request["email"])
                    if joined:
                        add_notification(joined, "Joined group", f"You joined {group['name']}.")
                    notify_group(group, "New group member", f"{request['name']} joined {group['name']}.")
                    add_group_history(group, f"{request['name']} joined the group")
                elif data.get("action") == "reject":
                    add_group_history(group, f"Join request from {request['name']} was rejected")
                elif data.get("action") != "reject":
                    return self.send_json({"error": "Choose accept or reject."}, 400)
                save_groups(groups)
                save_users(users)
                return self.send_json(public_user(manager))
            if self.path == "/api/groups/deposit":
                user = find_user(data.get("name", ""))
                group = group_for_user(data.get("group_id", ""), user) if user else None
                amount = float(data.get("amount", 0))
                if not group:
                    return self.send_json({"error": "You are not a member of that group."}, 403)
                if amount <= 0:
                    return self.send_json({"error": "Amount must be positive."}, 400)
                user.withdraw(amount)
                group["balance"] = round(group["balance"] + amount, 2)
                user.history.append(f"{current_date()} - Deposited into {group['name']}: -${amount:.2f}")
                add_group_history(group, f"{visible_name(user)} deposited ${amount:.2f}")
                notify_group(group, "Group deposit", f"{visible_name(user)} deposited ${amount:.2f} into {group['name']}.", user.email)
                save_groups(groups)
                save_users(users)
                return self.send_json(public_user(user))
            if self.path in ("/api/groups/withdraw", "/api/groups/transfer"):
                manager = find_user(data.get("name", ""))
                group = group_for_user(data.get("group_id", ""), manager) if manager else None
                amount = float(data.get("amount", 0))
                if not group or not can_manage_group(group, manager):
                    return self.send_json({"error": "Only the creator or a sub-creator can use group money."}, 403)
                if amount <= 0 or amount > group["balance"]:
                    return self.send_json({"error": "The group does not have enough money."}, 400)
                recipient = manager
                if self.path == "/api/groups/transfer":
                    add_group_history(group, f"{visible_name(manager)} transferred ${amount:.2f} to {visible_name(recipient)}")
                    recipient = find_user_by_email(data.get("email", ""))
                    if not recipient or not group_member(group, recipient.email):
                        return self.send_json({"error": "Choose a user in this group."}, 400)
                group["balance"] = round(group["balance"] - amount, 2)
                recipient.deposit(amount)
                recipient.history.append(f"{current_date()} - Received from group {group['name']}: +${amount:.2f}")
                if self.path == "/api/groups/transfer":
                    notify_group(group, "Group transfer", f"{visible_name(manager)} transferred ${amount:.2f} from {group['name']} to {visible_name(recipient)}.")
                    add_notification(recipient, "Group money received", f"You received ${amount:.2f} from {group['name']}.")
                else:
                    add_group_history(group, f"{visible_name(manager)} withdrew ${amount:.2f}")
                    notify_group(group, "Group withdrawal", f"{visible_name(manager)} withdrew ${amount:.2f} from {group['name']}.")
                save_groups(groups)
                save_users(users)
                return self.send_json(public_user(manager))
            if self.path == "/api/groups/role":
                manager = find_user(data.get("name", ""))
                group = group_for_user(data.get("group_id", ""), manager) if manager else None
                target = group_member(group, data.get("email", "")) if group else None
                if not group or not can_manage_group(group, manager) or not target:
                    return self.send_json({"error": "You cannot change that member's role."}, 403)
                if target["role"] == "creator":
                    return self.send_json({"error": "The creator role cannot be changed."}, 400)
                target["role"] = "subcreator" if data.get("role") == "subcreator" else "member"
                add_group_history(group, f"{target['name']} became {target['role']}")
                target_user = find_user_by_email(target["email"])
                if target_user:
                    add_notification(target_user, "Group role changed", f"Your role in {group['name']} is now {target['role']}.")
                notify_group(group, "Group role changed", f"{target['name']} is now a {target['role']} in {group['name']}.", target["email"])
                save_groups(groups)
                save_users(users)
                return self.send_json(public_user(manager))
            if self.path == "/api/groups/kick":
                creator = find_user(data.get("name", ""))
                group = next((item for item in groups if item["id"] == data.get("group_id")), None)
                target = group_member(group, data.get("email", "")) if group else None
                if not creator or not group or group["creator_email"].lower() != creator.email.lower():
                    return self.send_json({"error": "Only the creator can kick members."}, 403)
                if not target or target["role"] == "creator":
                    return self.send_json({"error": "That member cannot be kicked."}, 400)
                group["members"].remove(target)
                kicked = find_user_by_email(target["email"])
                if kicked:
                    if kicked.auto_deposit_group_id == group["id"]:
                        kicked.auto_deposit_group_id = ""
                    add_notification(kicked, "Removed from group", f"You were removed from {group['name']}.")
                removed_name = visible_name(kicked) if kicked else target["name"]
                add_group_history(group, f"{removed_name} was removed from the group")
                notify_group(group, "Member removed", f"{removed_name} was removed from {group['name']}.")
                save_groups(groups)
                save_users(users)
                return self.send_json(public_user(creator))
            if self.path == "/api/transaction":
                user = find_user(data.get("name", ""))
                if not user:
                    return self.send_json({"error": "Account not found."}, 404)
                amount = float(data.get("amount", 0))
                if data.get("type") == "deposit":
                    deposit_income(user, amount, "manual deposit")
                else:
                    user.withdraw(amount)
                save_users(users)
                save_groups(groups)
                send_email(user.email, f"Northstar {data.get('type', 'account')} notification",
                           f"A {data.get('type', 'account')} of ${amount:.2f} was made on your Northstar account.\n"
                           f"Your new balance is ${user.balance:.2f}.")
                notify_discord(f"Northstar balance update for {user.full_name()}: "
                               f"{data.get('type', 'account').title()} of ${amount:.2f}. "
                               f"New balance: ${user.balance:.2f}.")
                return self.send_json(public_user(user))
            if self.path == "/api/transfer/request":
                sender = find_user(data.get("name", ""))
                recipient = find_user_by_email(data.get("email", ""))
                amount = float(data.get("amount", 0))
                if not sender:
                    return self.send_json({"error": "Account not found."}, 404)
                if not recipient:
                    return self.send_json({"error": "No account uses that email address."}, 404)
                if sender is recipient:
                    return self.send_json({"error": "You cannot transfer money to yourself."}, 400)
                if amount <= 0:
                    return self.send_json({"error": "Amount must be positive."}, 400)
                if amount > sender.balance:
                    return self.send_json({"error": "You do not have enough money for this request."}, 400)
                request = {"id": secrets.token_urlsafe(12), "sender_name": sender.full_name(),
                           "sender_email": sender.email, "recipient_name": recipient.full_name(),
                           "recipient_email": recipient.email, "amount": round(amount, 2),
                           "status": "pending", "created_at": current_date()}
                sender.transfer_requests.append(request)
                recipient.transfer_requests.append(request)
                save_users(users)
                send_email(recipient.email, "Northstar transfer request",
                           f"{sender.full_name()} wants to send you ${amount:.2f}. Log in to accept or decline it.")
                return self.send_json(public_user(sender))
            if self.path == "/api/transfer/respond":
                recipient = find_user(data.get("name", ""))
                request_id = data.get("id", "")
                request = next((item for item in recipient.transfer_requests
                                if item.get("id") == request_id and item.get("status") == "pending"), None) if recipient else None
                if not recipient or not request or request.get("recipient_email") != recipient.email:
                    return self.send_json({"error": "Transfer request not found."}, 404)
                sender = find_user_by_email(request.get("sender_email", ""))
                if data.get("action") == "reject":
                    for account in users:
                        for item in account.transfer_requests:
                            if item.get("id") == request_id:
                                item["status"] = "rejected"
                    save_users(users)
                    return self.send_json(public_user(recipient))
                if data.get("action") != "accept":
                    return self.send_json({"error": "Choose accept or reject."}, 400)
                amount = float(request["amount"])
                if not sender or sender.balance < amount:
                    return self.send_json({"error": "The sender no longer has enough money."}, 400)
                sender.balance -= amount
                deposit_income(recipient, amount, "accepted transfer")
                sender.history.append(f"{current_date()} - Transfer sent to {recipient.full_name()}: -${amount:.2f}")
                recipient.history.append(f"{current_date()} - Transfer received from {sender.full_name()}: +${amount:.2f}")
                for account in users:
                    for item in account.transfer_requests:
                        if item.get("id") == request_id:
                            item["status"] = "accepted"
                save_users(users)
                save_groups(groups)
                send_email(sender.email, "Northstar transfer accepted",
                           f"{recipient.full_name()} accepted your ${amount:.2f} transfer.")
                return self.send_json(public_user(recipient))
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    start_discord_bot()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{browser_host}:{port}"
    print(f"Northstar Bank is open at {url}")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nNorthstar Bank closed.")
        server.server_close()


