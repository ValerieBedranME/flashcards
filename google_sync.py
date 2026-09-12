"""Google OAuth and conservative, three-way synchronization of personal Sheets."""
from copy import deepcopy
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from flask import g, redirect, request, session
from storage import external_io
import workspace as w

SCOPE = "https://www.googleapis.com/auth/drive.file"
HEADERS = ["ID карточки", "ID набора", "ID темы", "Тема", "Набор", "Вопрос", "Ответ",
           "Источник", "Автор исходного набора", "Статус", "Версия"]


def setting(app, key):
    return app.config.get(key) or os.environ.get(key)


def configured(app):
    return bool(setting(app, "GOOGLE_CLIENT_ID") and setting(app, "GOOGLE_CLIENT_SECRET")
                and (not os.environ.get("VERCEL") or setting(app, "PUBLIC_BASE_URL")))


def public_status(user, app):
    google = user.get("google", {})
    return {"configured": configured(app), "connected": bool(google.get("token")),
            "pending_subjects": list(google.get("pending_subjects", {})),
            "links": {sid: {k: link.get(k) for k in
                       ("file_id", "url", "last_sync", "pending", "error")}
                      for sid, link in google.get("links", {}).items()}}


def cipher(app):
    from cryptography.fernet import Fernet
    dedicated = setting(app, "GOOGLE_TOKEN_KEY")
    if dedicated:
        return Fernet(dedicated.encode() if isinstance(dedicated, str) else dedicated)
    key = app.secret_key
    if isinstance(key, str):
        key = key.encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"flashcards/google-tokens/v1\0" + key).digest()))


def http_json(url, method="GET", payload=None, token=None, form=False):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if payload is not None:
        body = (urllib.parse.urlencode(payload) if form else json.dumps(payload)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        with external_io():
            with urllib.request.urlopen(urllib.request.Request(url, body, headers, method=method), timeout=12) as response:
                raw = response.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise w.Problem("Таблица слишком велика для одного обновления", 422)
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        status = getattr(error, "code", 503)
        message = "Google временно недоступен. Сохранённые карточки остаются на месте."
        if status in (401, 403):
            message = "Нет доступа к Google-таблице. Переподключите Google в профиле."
        raise w.Problem(message, 503) from None


def access_token(user, app):
    encrypted = user.get("google", {}).get("token")
    if not encrypted:
        raise w.Problem("Сначала подключите Google в профиле", 409)
    try:
        token = json.loads(cipher(app).decrypt(encrypted.encode()))
    except Exception:
        raise w.Problem("Подключите Google заново", 409) from None
    if token.get("expires_at", 0) <= time.time() + 60:
        refreshed = http_json("https://oauth2.googleapis.com/token", "POST", {
            "client_id": setting(app, "GOOGLE_CLIENT_ID"),
            "client_secret": setting(app, "GOOGLE_CLIENT_SECRET"),
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}, form=True)
        if not refreshed.get("access_token"):
            raise w.Problem("Google не подтвердил подключение. Подключите его заново.", 503)
        token.update(access_token=refreshed["access_token"], expires_at=time.time() + refreshed.get("expires_in", 3600))
        user["google"]["token"] = cipher(app).encrypt(json.dumps(token).encode()).decode()
    return token["access_token"]


class GoogleSheet:
    def __init__(self, user, app):
        self.token = access_token(user, app)

    def call(self, path, method="GET", payload=None):
        return http_json("https://sheets.googleapis.com/v4/spreadsheets" + path, method, payload, self.token)

    def create(self, name, identity):
        # App-private metadata recovers a file whose create response was lost.
        query = urllib.parse.urlencode({
            "q": "trashed = false and appProperties has { key='flashcardsWorkspace' and value='" + identity + "' }",
            "fields": "files(id,webViewLink),nextPageToken", "pageSize": 2})
        found = http_json("https://www.googleapis.com/drive/v3/files?" + query, token=self.token)
        if "files" not in found:
            raise w.Problem("Google не подтвердил список таблиц. Повторите подключение.", 503)
        files = found.get("files", [])
        if len(files) > 1 or found.get("nextPageToken"):
            raise w.Problem("Обнаружено несколько связанных таблиц. Требуется проверить подключение.", 409)
        file = files[0] if files else http_json(
            "https://www.googleapis.com/drive/v3/files?fields=id,webViewLink", "POST",
            {"name": "FlashCards — " + name, "mimeType": "application/vnd.google-apps.spreadsheet",
             "appProperties": {"flashcardsWorkspace": identity}}, self.token)
        return {"spreadsheetId": file["id"], "spreadsheetUrl": file.get("webViewLink") or
                "https://docs.google.com/spreadsheets/d/" + file["id"] + "/edit"}

    def read(self, file_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            raise w.Problem("Некорректная ссылка на таблицу")
        result = self.call("/" + file_id + "/values/" + urllib.parse.quote("'Карточки'!A:K", safe="")
                           + "?valueRenderOption=FORMULA")
        if "values" not in result:
            raise w.Problem("Не удалось прочитать структуру таблицы. Карточки сохранены.", 409)
        return result["values"]

    def write(self, file_id, changes, before=None):
        if not changes:
            return
        ranges = []
        for row, values in changes:
            previous = before[row - 1] if before and row <= len(before) else []
            # Write only changed cells, so an unrelated concurrent edit is retained.
            for column, value in enumerate(values):
                old = str(previous[column]) if column < len(previous) else ""
                if before is None or old != value:
                    ranges.append({"range": f"'Карточки'!{chr(65 + column)}{row}", "values": [[value]]})
        self.call("/" + file_id + "/values:batchUpdate", "POST", {
            "valueInputOption": "RAW", "data": ranges})

    def initialize(self, file_id):
        metadata = self.call("/" + file_id + "?fields=sheets(properties)")
        properties = metadata["sheets"][0]["properties"]
        sheet_id = properties["sheetId"]
        self.call("/" + file_id + ":batchUpdate", "POST", {"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "title": "Карточки",
                "gridProperties": {"frozenRowCount": 1, "rowCount": max(10000, properties.get("gridProperties", {}).get("rowCount", 0))}},
                "fields": "title,gridProperties.frozenRowCount,gridProperties.rowCount"}}]})
        header = self.call("/" + file_id + "/values/" + urllib.parse.quote("'Карточки'!A1:K1", safe="")).get("values", [])
        if header and header[0] != HEADERS:
            raise w.Problem("Первая строка новой таблицы уже заполнена. Перенесите карточки ниже заголовка.", 409)
        self.write(file_id, [(1, HEADERS)])
        self.call("/" + file_id + ":batchUpdate", "POST", {"requests": [
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 5, "endColumnIndex": 8},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0,
                                                      "endIndex": 3}, "properties": {"hiddenByUser": True},
                                            "fields": "hiddenByUser"}},
            {"addProtectedRange": {"protectedRange": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                                                       "warningOnly": True, "description": "Заголовок FlashCards"}}}
        ]})


def serialize(ws, card):
    return [str(card["id"]), card["deck_id"], card["topic_id"], card["topic"],
            ws["decks"][card["deck_id"]]["name"], card["q"], card["a"], card.get("source", ""),
            card["author"], "в корзине" if card["deleted"] else "активна", str(card["revision"])]


def parse_rows(ws, owner, subject, file_id, rows, import_epoch=""):
    if not rows or rows[0] != HEADERS:
        raise w.Problem("Заголовки таблицы изменены. Восстановите исходную строку заголовков.", 409)
    if len(rows) > 10000:
        raise w.Problem("За одно обновление поддерживается до 9999 строк", 422)
    remote, positions, occurrences = {}, {}, {}
    for number, raw in enumerate(rows[1:], 2):
        row = [str(x) if x is not None else "" for x in raw]
        row += [""] * (len(HEADERS) - len(row))
        if not any(v.strip() for v in row):
            continue
        cid, did, tid, topic_name, deck_name, q, a, source, author, status, revision = row[:len(HEADERS)]
        try:
            values = w.card_values({"q": q, "a": a, "source": source})
            if status not in ("", "активна", "в корзине"):
                raise w.Problem("Статус должен быть «активна» или «в корзине»")
            if cid:
                card = ws["cards"].get(cid)
                if not card or card["subject_id"] != subject:
                    raise w.Problem("Неизвестный ID карточки: для новой карточки оставьте ID пустым")
                if did != card["deck_id"] or tid != card["topic_id"] or author != card["author"]:
                    raise w.Problem("Служебные поля и исходного автора нельзя менять вручную")
                # Names are descriptive; their stable IDs remain authoritative.
                item = dict(w.content(card), **values, deleted=status == "в корзине")
            else:
                if did or tid:
                    raise w.Problem("В новой строке оставьте все служебные ID пустыми")
                topic = w.ensure_topic(ws, subject, topic_name)
                deck_name = w.text(deck_name, "Набор")
                matches = [d for d in ws["decks"].values() if d["topic_id"] == topic["id"]
                           and d["name"] == deck_name and not d.get("archived")]
                if len(matches) > 1:
                    raise w.Problem("Несколько наборов с таким названием. Задайте им разные названия в приложении")
                deck = matches[0] if matches else None
                if not deck:
                    deck = w.create_deck(ws, owner, {"name": deck_name, "topic_id": topic["id"],
                                                   "topic": topic_name, "subject_id": subject})
                fingerprint = w.digest([topic_name, deck_name, q, a, source])
                occurrence = occurrences.get(fingerprint, 0)
                occurrences[fingerprint] = occurrence + 1
                cid = "card_" + uuid.uuid5(uuid.NAMESPACE_URL, file_id + import_epoch + fingerprint + str(occurrence)).hex
                # A retried metadata write must find the exact same imported card.
                card = ws["cards"].get(cid)
                if not card:
                    card = w.create_card(ws, deck, values, cid)
                    card["import_pending"] = True
                item = dict(w.content(card), **values, deleted=status == "в корзине")
            if cid in remote:
                raise w.Problem("Один ID карточки встречается дважды")
            remote[cid], positions[cid] = item, number
        except w.Problem as error:
            raise w.Problem(f"Строка {number}: {error.message}", 422) from None
    return remote, positions


def synchronize(user, owner, subject, sheet):
    """Only observed, valid rows can be merged. No bulk clear/replace is used."""
    original_ws = user["workspace"]
    ws = deepcopy(original_ws)
    link = user["google"]["links"][subject]
    if link.get("sync_job"):
        return {"job_id": link["sync_job"]["id"], "pending_sync": True}
    rows = sheet.read(link["file_id"])
    base = deepcopy(link.get("base", {}))
    remote, positions = parse_rows(ws, owner, subject, link["file_id"], rows, w.digest(base))
    imported_ids = list(set(ws["cards"]) - set(original_ws["cards"]))
    changes, next_base = [], deepcopy(base)
    next_row = len(rows) + 1
    for cid, card in list(ws["cards"].items()):
        if card["subject_id"] != subject:
            continue
        local = w.content(card)
        before, incoming = base.get(cid), remote.get(cid)
        if incoming is None:
            if before is not None:
                incoming = dict(before, deleted=True)
            else:
                incoming = local  # New app card, not exported yet.
        if before is not None and local != before and incoming != before and local != incoming:
            existing = next((c for c in ws["conflicts"].values() if str(c["card_id"]) == cid and c["source"] == "google"), None)
            conflict = {"id": existing["id"] if existing else w.uid("conflict"), "source": "google",
                        "card_id": card["id"], "current": local, "proposed": incoming,
                        "revision": card["revision"]}
            ws["conflicts"][conflict["id"]] = conflict
            continue
        if incoming != local and (before is None or local == before):
            w.change_card(ws, card, incoming, incoming.get("deleted", False))
            if not card["deleted"]:
                ws["decks"][card["deck_id"]]["archived"] = False
            local = w.content(card)
        row_number = positions.get(cid)
        rendered = serialize(ws, card)
        if row_number:
            existing_row = [str(x) for x in rows[row_number - 1]]
            if existing_row != rendered:
                changes.append((row_number, rendered))
        elif not card.get("deleted"):
            changes.append((next_row, rendered))
            next_row += 1
        next_base[cid] = local
    user["workspace"] = ws
    conflicts = any(
        c["source"] == "google" and ws["cards"][str(c["card_id"])]["subject_id"] == subject
        for c in ws["conflicts"].values())
    if changes:
        # This request commits IDs and the intent BEFORE a second request writes Google.
        # A lost response or DB failure after that write can be acknowledged on retry.
        job = {"id": w.uid("sync"), "before": rows, "changes": changes, "base": next_base,
               "local_hash": subject_hash(ws, subject), "imported_ids": imported_ids, "at": time.time()}
        link.update(sync_job=job, pending=True, error=None)
        return {"job_id": job["id"], "pending_sync": True, "conflicts": len(ws["conflicts"])}
    link.update(base=next_base, last_sync=time.time(), pending=conflicts, error=None)
    return {"ok": True, "conflicts": len(ws["conflicts"]), "pending_sync": link["pending"]}


def subject_hash(ws, subject):
    return w.digest([w.content(c) for c in ws["cards"].values() if c["subject_id"] == subject])


def normalized(rows):
    result = []
    for raw in rows:
        row = [str(v) if v is not None else "" for v in raw]
        while row and not row[-1]:
            row.pop()
        result.append(row)
    while result and not result[-1]:
        result.pop()
    return result


def flush_sync(user, subject, sheet, job_id):
    link = user["google"]["links"][subject]
    job = link.get("sync_job")
    if not job:
        return {"ok": True, "pending_sync": link.get("pending", False)}
    if job["id"] != job_id:
        raise w.Problem("Обновление уже изменилось. Повторите его.", 409)
    current = sheet.read(link["file_id"])
    after = deepcopy(job["before"])
    for row, values in job["changes"]:
        while len(after) < row:
            after.append([])
        after[row-1] = values
    already_written = normalized(current) == normalized(after)
    unchanged_local = job["local_hash"] == subject_hash(user["workspace"], subject)
    if not already_written and (normalized(current) != normalized(job["before"]) or not unchanged_local):
        # Preserve the observed versions, then plan a fresh merge rather than write stale rows.
        for cid in job.get("imported_ids", []):
            card = user["workspace"]["cards"].get(cid)
            if card and card.get("import_pending"):
                del user["workspace"]["cards"][cid]
        link["last_write"] = link.pop("sync_job")
        link["pending"] = True
        return {"retry": True, "pending_sync": True}
    if not already_written:
        sheet.write(link["file_id"], job["changes"], job["before"])
        # A user may have edited immediately after our write. Do not acknowledge stale data.
        if normalized(sheet.read(link["file_id"])) != normalized(after):
            for cid in job.get("imported_ids", []):
                user["workspace"]["cards"][cid].pop("import_pending", None)
            link["last_write"] = link.pop("sync_job")
            link["base"] = job["base"]
            link["pending"] = True
            return {"retry": True, "pending_sync": True}
    for cid in job.get("imported_ids", []):
        user["workspace"]["cards"][cid].pop("import_pending", None)
    link.update(base=job["base"], last_sync=time.time(), error=None,
                pending=not unchanged_local or any(c["source"] == "google" and
                user["workspace"]["cards"][str(c["card_id"])]["subject_id"] == subject
                for c in user["workspace"]["conflicts"].values()))
    link["last_write"] = link.pop("sync_job")
    return {"ok": True, "pending_sync": link["pending"]}


def install(app, host, route, data):
    def callback_uri():
        base = setting(app, "PUBLIC_BASE_URL") or request.url_root.rstrip("/")
        return base.rstrip("/") + "/api/v2/google/callback"

    @route("/google/connect", ("POST",))
    def connect(name, user, ws):
        if not configured(app):
            raise w.Problem("Подключение Google ещё настраивается. Пока можно работать в приложении.", 503)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        user["google_oauth"] = {"state_hash": hashlib.sha256(state.encode()).hexdigest(),
                                "verifier": verifier, "expires": time.time() + 600,
                                "auth_version": user["salt"], "redirect_uri": callback_uri()}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
            "client_id": setting(app, "GOOGLE_CLIENT_ID"), "redirect_uri": callback_uri(),
            "response_type": "code", "scope": SCOPE, "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})}

    @app.route("/api/v2/google/callback")
    def google_callback():
        name = session.get("user")
        users = host.load_users()
        user = users.get(name)
        if not user or session.get("auth_version") != user.get("salt"):
            return redirect("/?google=login")
        flow = user.pop("google_oauth", {})
        host.save_users(users)
        if (flow.get("expires", 0) < time.time() or flow.get("auth_version") != user["salt"]
                or not secrets.compare_digest(flow.get("state_hash", ""), hashlib.sha256(request.args.get("state", "").encode()).hexdigest())):
            return redirect("/?google=error")
        if request.args.get("error") or not request.args.get("code"):
            return redirect("/?google=cancelled")
        try:
            token = http_json("https://oauth2.googleapis.com/token", "POST", {
                "client_id": setting(app, "GOOGLE_CLIENT_ID"), "client_secret": setting(app, "GOOGLE_CLIENT_SECRET"),
                "code": request.args["code"], "code_verifier": flow["verifier"],
                "redirect_uri": flow["redirect_uri"], "grant_type": "authorization_code"}, form=True)
            if not token.get("refresh_token") or SCOPE not in token.get("scope", "").split():
                raise w.Problem("Google не предоставил доступ к таблицам")
            token["expires_at"] = time.time() + token.get("expires_in", 3600)
            # The network exchange released the shared transaction. Reload before saving.
            users = host.load_users()
            latest = users.get(name)
            if not latest or latest.get("salt") != flow["auth_version"]:
                return redirect("/?google=login")
            google = latest.setdefault("google", {"links": {}})
            google["token"] = cipher(app).encrypt(json.dumps(token).encode()).decode()
            host.save_users(users)
            return redirect("/?google=connected")
        except w.Problem:
            return redirect("/?google=error")

    @route("/google/subjects/<subject>", ("POST",))
    def prepare(name, user, ws, subject):
        w.subject_id(subject)
        google = user.setdefault("google", {"links": {}})
        link = google["links"].get(subject)
        if not link:
            creating = google.setdefault("creating", {})
            if creating.get(subject, 0) > time.time():
                raise w.Problem("Личная таблица уже создаётся. Повторите обновление чуть позже.", 409)
            creating[subject] = time.time() + 180
            # Checkpoint the per-owner lease before the first network call releases the lock.
            users = host.load_users()
            users[name] = user
            host.save_users(users)
            g.workspace_rebase = deepcopy(user)
            g.commit_storage_on_error = True
        try:
            sheet = GoogleSheet(user, app)
            if not link:
                secret = app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key
                identity = hmac.new(secret, ("flashcards/sheet/v1/" + name + "/" + subject).encode(), hashlib.sha256).hexdigest()
                result = sheet.create(next(s["name"] for s in w.SUBJECTS if s["id"] == subject), identity)
                link = {"file_id": result["spreadsheetId"], "url": result["spreadsheetUrl"],
                        "base": {}, "pending": True, "initialized": False}
                google["links"][subject] = link
            if not link.get("initialized"):
                sheet.initialize(link["file_id"])
                link["initialized"] = True
            google.get("pending_subjects", {}).pop(subject, None)
            result = synchronize(user, name, subject, sheet)
            return dict(result, url=link["url"])
        except w.Problem as error:
            if link:
                link["error"] = error.message
            g.commit_storage_on_error = True
            raise
        finally:
            google.get("creating", {}).pop(subject, None)

    @route("/google/subjects/<subject>/sync", ("POST",))
    def sync(name, user, ws, subject):
        w.subject_id(subject)
        link = user.get("google", {}).get("links", {}).get(subject)
        if not link:
            raise w.Problem("Сначала создайте личную таблицу для предмета", 409)
        try:
            return synchronize(user, name, subject, GoogleSheet(user, app))
        except w.Problem as error:
            link["error"] = error.message
            g.commit_storage_on_error = True
            raise

    @route("/google/subjects/<subject>/flush", ("POST",))
    def flush(name, user, ws, subject):
        w.subject_id(subject)
        link = user.get("google", {}).get("links", {}).get(subject)
        if not link:
            raise w.Problem("Таблица не подключена", 409)
        try:
            return flush_sync(user, subject, GoogleSheet(user, app), data().get("job_id"))
        except w.Problem as error:
            link["error"] = error.message
            g.commit_storage_on_error = True
            raise

    @route("/google/disconnect", ("POST",))
    def disconnect(name, user, ws):
        user.get("google", {}).pop("token", None)
        user.pop("google_oauth", None)
        return {"ok": True}
