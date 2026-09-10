#!/usr/bin/env python3
"""Flashcards app — Flask server with per-user profiles + passwords + email reset + SRS.

Data model:
  cards.json  — shared base card set (visible to everyone)
  users.json  — {username: {email, salt, password_hash, srs, added, deleted, edited}}
    srs: {card_id: {interval, due, last}}  — spaced repetition state
      interval: days until next review (0 = due today)
      due:      unix timestamp when card becomes due
      last:     "know" | "dontknow" | "unsure"  (last answer, for stats)
  reset.json  — hashed, expiring password-reset codes and attempt/send limits

SRS logic (Leitner-like):
  new card (no srs entry):
    know     -> interval 1 day
    unsure   -> interval 0 (due today)
    dontknow -> interval 0 (due today)
  review (has srs entry):
    know     -> interval doubles (min 1)
    unsure   -> interval halves (min 1)
    dontknow -> interval resets to 0
"""
import json
import os
import hashlib
import hmac
import secrets
import time
from flask import Flask, jsonify, request, send_from_directory, session, g
from storage import init_storage, load_document, save_document
from mailer import configured as email_configured, send_email

BASE = os.path.dirname(os.path.abspath(__file__))
CARDS_PATH = os.path.join(BASE, "cards.json")
USERS_PATH = os.path.join(BASE, "users.json")
RESET_PATH = os.path.join(BASE, "reset.json")

app = Flask(__name__, static_folder=None)
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
# A domain-separated key keeps sessions stable across instances when the hosting
# integration provides only a database credential. SECRET_KEY overrides it.
SESSION_KEY = os.environ.get("SECRET_KEY")
if not SESSION_KEY and DATABASE_URL:
    SESSION_KEY = hmac.digest(DATABASE_URL.encode(), b"flashcards/session/v1", "sha256")
app.config.update(
    DATABASE_URL=DATABASE_URL,
    SECRET_KEY=SESSION_KEY or (None if os.environ.get("VERCEL") else secrets.token_hex(32)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=1024 * 1024,
)
init_storage(app)


@app.before_request
def require_login():
    if not request.path.startswith("/api/"):
        return
    if not app.secret_key:
        return jsonify({"error": "Сервер ещё не настроен"}), 503
    if request.path.startswith(("/api/cards", "/api/topics", "/api/srs")):
        username = session.get("user")
        u = get_user(username) if username else None
        if not u or session.get("auth_version") != u.get("salt"):
            return jsonify({"error": "Войдите в свой профиль"}), 401
        if request.args.get("user", "").strip() != username:
            return jsonify({"error": "Нет доступа к чужому профилю"}), 403

DAY = 86400


def load_cards():
    with open(CARDS_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_users():
    return load_document("users", USERS_PATH)


def save_users(users):
    save_document("users", USERS_PATH, users)


def load_reset():
    return load_document("reset", RESET_PATH)


def save_reset(reset):
    save_document("reset", RESET_PATH, reset)


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()


def get_user(username):
    users = load_users()
    return users.get(username)


def migrate_stacks_to_srs(u):
    """Convert legacy 'stacks' field into 'srs'."""
    stacks = u.get("stacks")
    if not stacks:
        return
    now = time.time()
    srs = u.get("srs", {})
    for key, interval, last in (
        ("know", 1, "know"),
        ("dontknow", 0, "dontknow"),
        ("unsure", 0, "unsure"),
    ):
        for c in stacks.get(key, []):
            cid = c.get("id")
            if cid is None:
                continue
            srs[str(cid)] = {
                "interval": interval,
                "due": now + interval * DAY,
                "last": last,
            }
    u["srs"] = srs
    u.pop("stacks", None)


def ensure_user(username, users=None):
    if users is None:
        users = load_users()
    if username not in users:
        users[username] = {
            "email": "",
            "salt": "",
            "password_hash": "",
            "srs": {},
            "added": [],
            "deleted": [],
            "edited": {},
        }
        save_users(users)
        return users[username]
    u = users[username]
    if "stacks" in u:
        migrate_stacks_to_srs(u)
        save_users(users)
    if "srs" not in u:
        u["srs"] = {}
        save_users(users)
    return u


def effective_cards(username):
    base = load_cards()
    u = get_user(username) or {}
    deleted = set(u.get("deleted", []))
    edited = u.get("edited", {})
    result = []
    for c in base:
        cid = c["id"]
        if cid in deleted:
            continue
        key = str(cid)
        if key in edited:
            e = edited[key]
            result.append({
                "id": cid,
                "topic": e.get("topic", c.get("topic", "Без темы")),
                "q": e.get("q", c["q"]),
                "a": e.get("a", c["a"]),
                "source": e.get("source", c.get("source", "")),
            })
        else:
            result.append(c)
    result.extend(u.get("added", []))
    return result


def next_id():
    base = load_cards()
    users = load_users()
    maxid = max([c.get("id", 0) for c in base] or [0])
    for u in users.values():
        for c in u.get("added", []):
            maxid = max(maxid, c.get("id", 0))
    return maxid + 1


def is_base_id(cid):
    return any(c.get("id") == cid for c in load_cards())


@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")


# ---------- Auth ----------

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "")
    if not name or not email or not password:
        return jsonify({"error": "Заполните имя, email и пароль"}), 400
    if len(password) < 4:
        return jsonify({"error": "Пароль должен быть не короче 4 символов"}), 400
    if "@" not in email or "." not in email:
        return jsonify({"error": "Некорректный email"}), 400

    users = load_users()
    existing = users.get(name)
    if existing and existing.get("password_hash"):
        return jsonify({"error": "Это имя уже занято"}), 409
    if any(u.get("email", "").lower() == email for u in users.values()):
        return jsonify({"error": "Этот email уже зарегистрирован. Войдите или восстановите пароль."}), 409

    salt = secrets.token_hex(16)
    ph = hash_password(password, salt)
    if existing:
        existing["email"] = email
        existing["salt"] = salt
        existing["password_hash"] = ph
    else:
        users[name] = {
            "email": email,
            "salt": salt,
            "password_hash": ph,
            "srs": {},
            "added": [],
            "deleted": [],
            "edited": {},
        }
    save_users(users)
    return jsonify({"ok": True})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    password = (data.get("password") or "")
    users = load_users()
    u = users.get(name)
    if not u:
        matches = [(uname, item) for uname, item in users.items()
                   if uname.casefold() == name.casefold() or item.get("email", "").lower() == name.lower()]
        if len(matches) == 1:
            name, u = matches[0]
    if not u or not u.get("password_hash"):
        return jsonify({"error": "Неверное имя или пароль"}), 401
    ph = hash_password(password, u.get("salt", ""))
    if not secrets.compare_digest(ph, u["password_hash"]):
        return jsonify({"error": "Неверное имя или пароль"}), 401
    session.clear()
    session["user"] = name
    session["auth_version"] = u["salt"]
    return jsonify({"ok": True, "name": name})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/session")
def current_session():
    name = session.get("user")
    u = get_user(name) if name else None
    if not u or session.get("auth_version") != u.get("salt"):
        return jsonify({"error": "Войдите в свой профиль"}), 401
    return jsonify({"name": name})


@app.route("/api/forgot", methods=["POST"])
def forgot():
    if not email_configured():
        return jsonify({"error": "Восстановление по email пока не настроено"}), 503
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    users = load_users()
    requested_name = (data.get("name") or "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Укажите email профиля"}), 400
    matches = [(uname, u) for uname, u in users.items()
               if u.get("email", "").lower() == email and (not requested_name or uname == requested_name)]
    if not matches:
        return jsonify({"ok": True})
    if len(matches) != 1:
        return jsonify({"error": "Для этого email укажите также имя нужного профиля"}), 400
    name, user = matches[0]
    now = time.time()
    reset = load_reset()
    previous = reset.get(email, {})
    sent_times = [stamp for stamp in previous.get("sent_times", []) if stamp > now - 3600]
    if sent_times and (now - sent_times[-1] < 60 or len(sent_times) >= 5):
        return jsonify({"error": "Код уже запрошен. Попробуйте позже или проверьте почту и папку Спам."}), 429
    code = f"{secrets.randbelow(1000000):06d}"

    body = (
        f"Здравствуйте, {name}!\n\n"
        f"Ваш код для восстановления пароля в приложении «Карточки — Анатомия»:\n\n"
        f"{code}\n\n"
        f"Код действителен 15 минут. Если вы не запрашивали восстановление, просто проигнорируйте это письмо."
    )
    if not send_email(email, "Восстановление пароля — Карточки", body):
        return jsonify({"error": "Не удалось отправить письмо. Попробуйте позже."}), 503
    reset[email] = {"code_hash": reset_code_hash(email, code), "expires": now + 900,
                    "name": name, "auth_version": user.get("salt"), "attempts": 0,
                    "sent_times": sent_times + [now]}
    save_reset(reset)
    return jsonify({"ok": True})


def reset_code_hash(email, code):
    key = app.secret_key
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, ("password-reset\0" + email + "\0" + code).encode(), hashlib.sha256).hexdigest()


@app.route("/api/reset", methods=["POST"])
def reset():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    new_password = (data.get("password") or "")
    reset = load_reset()
    rec = reset.get(email)
    if not rec or rec.get("expires", 0) < time.time() or rec.get("attempts", 0) >= 5:
        return jsonify({"error": "Неверный или истёкший код"}), 400
    if len(new_password) < 4:
        return jsonify({"error": "Пароль должен быть не короче 4 символов"}), 400

    if not secrets.compare_digest(rec.get("code_hash", ""), reset_code_hash(email, code)):
        rec["attempts"] = rec.get("attempts", 0) + 1
        save_reset(reset)
        g.commit_storage_on_error = True
        return jsonify({"error": "Неверный или истёкший код"}), 400
    users = load_users()
    u = users.get(rec.get("name"))
    if not u or u.get("email", "").lower() != email or u.get("salt") != rec.get("auth_version"):
        return jsonify({"error": "Неверный или истёкший код"}), 400
    salt = secrets.token_hex(16)
    u["salt"] = salt
    u["password_hash"] = hash_password(new_password, salt)
    save_users(users)
    reset[email] = {"sent_times": rec.get("sent_times", [])}
    save_reset(reset)
    session.clear()
    return jsonify({"ok": True})


# ---------- Cards ----------

@app.route("/api/cards", methods=["GET"])
def get_cards():
    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "user required"}), 400
    return jsonify(effective_cards(username))


@app.route("/api/topics", methods=["GET"])
def get_topics():
    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "user required"}), 400
    cards = effective_cards(username)
    topics = {}
    for c in cards:
        t = c.get("topic", "Без темы")
        topics.setdefault(t, 0)
        topics[t] += 1
    return jsonify([{"name": k, "count": v} for k, v in topics.items()])


@app.route("/api/cards", methods=["POST"])
def add_card():
    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "user required"}), 400
    data = request.get_json(force=True)
    q = (data.get("q") or "").strip()
    a = (data.get("a") or "").strip()
    topic = (data.get("topic") or "Без темы").strip()
    source = (data.get("source") or "").strip()
    if not q or not a:
        return jsonify({"error": "Вопрос и ответ обязательны"}), 400
    users = load_users()
    u = ensure_user(username, users)
    card = {"id": next_id(), "topic": topic, "q": q, "a": a, "source": source}
    u["added"].append(card)
    save_users(users)
    return jsonify({"ok": True, "id": card["id"]})


@app.route("/api/cards/<int:cid>", methods=["PUT"])
def update_card(cid):
    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "user required"}), 400
    data = request.get_json(force=True)
    q = (data.get("q") or "").strip()
    a = (data.get("a") or "").strip()
    if not q or not a:
        return jsonify({"error": "Вопрос и ответ обязательны"}), 400
    topic = (data.get("topic") or "Без темы").strip()
    source = (data.get("source") or "").strip()
    users = load_users()
    u = ensure_user(username, users)
    if is_base_id(cid):
        u["edited"][str(cid)] = {"q": q, "a": a, "topic": topic, "source": source}
    else:
        for c in u["added"]:
            if c.get("id") == cid:
                c["q"] = q
                c["a"] = a
                c["topic"] = topic
                c["source"] = source
                break
    save_users(users)
    return jsonify({"ok": True})


@app.route("/api/cards/<int:cid>", methods=["DELETE"])
def delete_card(cid):
    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "user required"}), 400
    users = load_users()
    u = ensure_user(username, users)
    if is_base_id(cid):
        if cid not in u["deleted"]:
            u["deleted"].append(cid)
    else:
        u["added"] = [c for c in u["added"] if c.get("id") != cid]
    save_users(users)
    return jsonify({"ok": True})


# ---------- SRS ----------

@app.route("/api/srs", methods=["GET"])
def get_srs():
    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "user required"}), 400
    cards = effective_cards(username)
    u = ensure_user(username)
    srs = u.get("srs", {})
    now = time.time()

    new_ids = []
    due_ids = []
    stats = {"know": 0, "dontknow": 0, "unsure": 0}

    for c in cards:
        cid = str(c["id"])
        rec = srs.get(cid)
        if not rec:
            new_ids.append(c["id"])
        else:
            last = rec.get("last")
            if last in stats:
                stats[last] += 1
            if rec.get("due", 0) <= now:
                due_ids.append(c["id"])

    return jsonify({"new": new_ids, "due": due_ids, "stats": stats})


@app.route("/api/srs/answer", methods=["POST"])
def srs_answer():
    username = request.args.get("user", "").strip()
    if not username:
        return jsonify({"error": "user required"}), 400
    data = request.get_json(force=True)
    cid = data.get("id")
    rating = data.get("rating")
    if cid is None or rating not in ("know", "dontknow", "unsure"):
        return jsonify({"error": "bad request"}), 400

    users = load_users()
    u = ensure_user(username, users)
    srs = u.get("srs", {})
    key = str(cid)
    now = time.time()
    cur = srs.get(key, {}).get("interval", 0)

    if rating == "know":
        interval = max(1, cur * 2) if cur > 0 else 1
    elif rating == "unsure":
        interval = max(1, cur // 2) if cur > 0 else 0
    else:  # dontknow
        interval = 0

    srs[key] = {
        "interval": interval,
        "due": now + interval * DAY,
        "last": rating,
    }
    u["srs"] = srs
    save_users(users)
    return jsonify({"ok": True, "interval": interval})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8081"))
    app.run(host="0.0.0.0", port=port, debug=False)
