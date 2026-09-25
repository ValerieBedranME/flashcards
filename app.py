#!/usr/bin/env python3
"""Flashcards app — Flask server with per-user profiles + passwords + email reset + SRS.

Data model:
  cards.json  — legacy base used when migrating existing profiles
  users.json  — account credentials, personal workspace, progress and encrypted Google tokens
  library.json — immutable published deck snapshots
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
import workspace as workspace_model

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
    MAX_CONTENT_LENGTH=3 * 1024 * 1024,
)
init_storage(app)


@app.before_request
def reject_cross_site_writes():
    if request.path.startswith('/api/') and request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        from urllib.parse import urlsplit
        origin = request.headers.get('Origin')
        expected = app.config.get('PUBLIC_BASE_URL') or os.environ.get('PUBLIC_BASE_URL') or request.host_url
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and
                (urlsplit(origin).scheme, urlsplit(origin).netloc) != (urlsplit(expected).scheme, urlsplit(expected).netloc)):
            return jsonify(error='Запрос с другого сайта отклонён'), 403
        if request.method != 'DELETE' and request.path != '/api/logout' and not request.is_json:
            return jsonify(error='Ожидаются данные JSON'), 415
        if request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify(error='Ожидаются данные формы'), 400
            for field, limit in (('name', 200), ('email', 254), ('password', 4096)):
                if field in data and (not isinstance(data[field], str) or len(data[field]) > limit):
                    return jsonify(error='Некорректное поле формы'), 400


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


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
    if workspace_model.migrate_user(u, load_cards(), username):
        save_users(users)
    return u


def effective_cards(username):
    return workspace_model.active_cards(ensure_user(username)["workspace"])


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


@app.route("/privacy")
def privacy():
    return send_from_directory(BASE, "privacy.html")


@app.route("/terms")
def terms():
    return send_from_directory(BASE, "terms.html")


@app.route('/assets/<name>')
def asset(name):
    if name not in ('app.js', 'app.css', 'app-icon.svg', 'apple-touch-icon.png'):
        return jsonify(error='Не найдено'), 404
    return send_from_directory(os.path.join(BASE, 'assets'), name)


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
            "workspace": workspace_model.empty_workspace(),
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


def legacy_deck(ws, owner, topic, subject="anatomy"):
    for deck in ws["decks"].values():
        if not deck.get("archived") and deck["subject_id"] == subject and ws["topics"][deck["topic_id"]]["name"] == topic:
            return deck
    return workspace_model.create_deck(ws, owner, {"name": topic, "topic": topic, "subject_id": subject})


@app.errorhandler(workspace_model.Problem)
def workspace_problem(error):
    return jsonify(error=error.message, **error.details), error.status


def check_legacy_write(user, subject):
    # An old open client cannot bypass revision/conflict checks for linked content.
    google = user.get("google", {})
    if google.get("token") or subject in google.get("links", {}):
        raise workspace_model.Problem("Обновите страницу для работы со связанной таблицей", 409)


@app.route("/api/cards", methods=["POST"])
def add_card():
    username = session["user"]
    data = request.get_json()
    users = load_users()
    u = ensure_user(username, users)
    ws = u["workspace"]
    check_legacy_write(u, data.get("subject_id", "anatomy"))
    deck = legacy_deck(ws, username, data.get("topic") or "Без темы", data.get("subject_id", "anatomy"))
    cid = secrets.randbits(50) + 1000000
    card = workspace_model.create_card(ws, deck, data, cid)
    save_users(users)
    return jsonify(ok=True, id=card["id"])


@app.route("/api/cards/<cid>", methods=["PUT"])
def update_card(cid):
    username = session["user"]
    data = request.get_json()
    users = load_users()
    u = ensure_user(username, users)
    ws = u["workspace"]
    card = workspace_model.get_card(ws, cid)
    check_legacy_write(u, card["subject_id"])
    if "revision" in data:
        workspace_model.check_revision(card, data["revision"])
    target_topic = data.get("topic") or card["topic"]
    if target_topic != card["topic"]:
        deck = legacy_deck(ws, username, target_topic, card["subject_id"])
        card.update(topic=target_topic, topic_id=deck["topic_id"], deck_id=deck["id"])
    workspace_model.change_card(ws, card, data)
    save_users(users)
    return jsonify(ok=True)


@app.route("/api/cards/<cid>", methods=["DELETE"])
def delete_card(cid):
    username = session["user"]
    users = load_users()
    u = ensure_user(username, users)
    card = workspace_model.get_card(u["workspace"], cid)
    check_legacy_write(u, card["subject_id"])
    workspace_model.change_card(u["workspace"], card, deleted=True)
    save_users(users)
    return jsonify(ok=True)


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
    topic_stats = {}

    for c in cards:
        topic = topic_stats.setdefault(c.get("topic", "Без темы"),
                                       {"know": 0, "dontknow": 0, "unsure": 0})
        cid = str(c["id"])
        rec = srs.get(cid)
        if not rec:
            new_ids.append(c["id"])
        else:
            last = rec.get("last")
            if last in stats:
                stats[last] += 1
                topic[last] += 1
            if rec.get("due", 0) <= now:
                due_ids.append(c["id"])

    return jsonify({"new": new_ids, "due": due_ids, "stats": stats, "topics": topic_stats})


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
    workspace_model.get_card(u["workspace"], cid)
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


import sys
from workspace_api import install as install_workspace
install_workspace(app, sys.modules[__name__])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8081"))
    app.run(host="0.0.0.0", port=port, debug=False)
