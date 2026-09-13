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
            "archived_links": [{"url": link.get("url")} for link in google.get("archives", {}).values()],
            "links": {sid: {k: link.get(k) for k in
                       ("file_id", "url", "last_sync", "pending", "error", "recovery_job")}
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
        elif status == 400 and url == "https://oauth2.googleapis.com/token":
            try:
                detail = json.loads(error.read(4096))
            except (ValueError, OSError):
                detail = {}
            if isinstance(detail, dict) and detail.get("error") == "invalid_grant":
                message = "Доступ Google истёк или был отозван. Переподключите Google в профиле."
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

    def read(self, file_id, title="Карточки"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            raise w.Problem("Некорректная ссылка на таблицу")
        result = self.call("/" + file_id + "/values/" + urllib.parse.quote("'" + title + "'!A:K", safe="")
                           + "?valueRenderOption=FORMULA")
        if "values" not in result:
            raise w.Problem("Не удалось прочитать структуру таблицы. Карточки сохранены.", 409)
        return result["values"]

    def receipt(self, file_id, job_id):
        properties = self.call("/" + file_id + "?fields=sheets(properties(sheetId,title))")["sheets"]
        titles = {p["properties"]["title"] for p in properties}
        names = [snapshot_name(job_id, kind) for kind in ("before", "after")]
        if not any(name in titles for name in names):
            return None
        if not all(name in titles for name in names):
            raise w.Problem("Служебная копия таблицы изменена. Обновление приостановлено; карточки сохранены.", 409)
        return {kind: self.read(file_id, title) for kind, title in zip(("before", "after"), names)}

    def write(self, file_id, changes, before=None, job_id=None, previous_job=None):
        if not changes:
            return
        if job_id:
            properties = self.call("/" + file_id + "?fields=sheets(properties(sheetId,title))")["sheets"]
            sheets = {p["properties"]["title"]: p["properties"]["sheetId"] for p in properties}
            if "Карточки" not in sheets:
                raise w.Problem("Лист «Карточки» не найден. Восстановите его название.", 409)
            source = sheets["Карточки"]
            requests = []
            # Ordered, atomic batch: preserve the exact preimage, write, preserve the
            # exact result. A repeated job cannot write again: sheet IDs are unique.
            def snapshot(kind):
                sid = snapshot_id(job_id, kind)
                requests.extend([
                    {"duplicateSheet": {"sourceSheetId": source, "newSheetId": sid,
                                        "newSheetName": snapshot_name(job_id, kind)}},
                    {"updateSheetProperties": {"properties": {"sheetId": sid, "hidden": True},
                                               "fields": "hidden"}}])
            snapshot("before")
            for row, values in changes:
                prior = before[row - 1] if before and row <= len(before) else []
                for col, value in enumerate(values):
                    old = str(prior[col]) if col < len(prior) else ""
                    if old != value:
                        requests.append({"updateCells": {
                            "start": {"sheetId": source, "rowIndex": row - 1, "columnIndex": col},
                            "rows": [{"values": [{"userEnteredValue": {"stringValue": value}}]}],
                            "fields": "userEnteredValue"}})
            requests.append({"repeatCell": {"range": {"sheetId": source,
                "startRowIndex": min(row for row, _ in changes) - 1,
                "endRowIndex": max(row for row, _ in changes), "startColumnIndex": 3, "endColumnIndex": 10},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}})
            snapshot("after")
            # The previous receipt has already been acknowledged in the database.
            # Keep the current pair until a later successful synchronization.
            if previous_job:
                for kind in ("before", "after"):
                    title = snapshot_name(previous_job, kind)
                    if title in sheets:
                        requests.append({"deleteSheet": {"sheetId": sheets[title]}})
            self.call("/" + file_id + ":batchUpdate", "POST", {"requests": requests})
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

    def topic_layout(self, file_id):
        metadata = self.call('/' + file_id + '?fields=sheets(properties)')
        sheet_id = next((s['properties']['sheetId'] for s in metadata['sheets']
                        if s['properties']['title'] == 'Карточки'), None)
        if sheet_id is None:
            raise w.Problem('Не найден лист «Карточки». Верните ему прежнее название.', 409)
        self.call('/' + file_id + ':batchUpdate', 'POST', {'requests': [
            {'updateDimensionProperties': {'range': {'sheetId': sheet_id,
                'dimension': 'COLUMNS', 'startIndex': 4, 'endIndex': 5},
                'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}}]})

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
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                      "startColumnIndex": 0, "endColumnIndex": 11},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": .94, "green": .94, "blue": .94},
                    "textFormat": {"bold": True}, "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
                "fields": "userEnteredFormat"}},
            *[{"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                     "startIndex": col, "endIndex": col + 1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}}
              for col, width in ((3, 155), (4, 175), (5, 220), (6, 300), (7, 100), (8, 160), (9, 90))],
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 5, "endColumnIndex": 8},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0,
                                                      "endIndex": 3}, "properties": {"hiddenByUser": True},
                                            "fields": "hiddenByUser"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                    "startIndex": 10, "endIndex": 11}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            {"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 9,
                    "endColumnIndex": 10}, "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                        {"userEnteredValue": "активна"}, {"userEnteredValue": "в корзине"}]}, "strict": True, "showCustomUi": True}}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                    "startColumnIndex": 0, "endColumnIndex": 11}}}},
            {"addProtectedRange": {"protectedRange": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                                                       "warningOnly": True, "description": "Заголовок FlashCards"}}}
        ]})


def snapshot_name(job_id, kind):
    return "_FlashCards_" + job_id + "_" + kind


def snapshot_id(job_id, kind):
    return int(hashlib.sha256((job_id + kind).encode()).hexdigest()[:7], 16) + 1


def serialize(ws, card):
    return [str(card["id"]), card["deck_id"], card["topic_id"], ws["decks"][card["deck_id"]]["name"],
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
                # E is retained for old tables and unfinished jobs. New rows only
                # need D (the single user-facing topic name).
                deck_name = w.text(deck_name or topic_name, "Тема")
                matches = [d for d in ws["decks"].values() if d["subject_id"] == subject
                           and d["name"] == deck_name and not d.get("archived")]
                if len(matches) > 1:
                    raise w.Problem("Несколько тем с таким названием. Задайте им разные названия в приложении")
                deck = matches[0] if matches else None
                if not deck:
                    deck = w.create_deck(ws, owner, {"name": deck_name,
                                                   "topic": deck_name, "subject_id": subject})
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
    if link.get("recovery"):
        raise w.Problem("Структура таблицы изменилась во время обновления. Все версии сохранены; требуется восстановление таблицы.", 409)
    if link.get("sync_job"):
        return {"job_id": link["sync_job"]["id"], "pending_sync": True}
    if not link.get('single_topic_layout') and callable(getattr(sheet, 'topic_layout', None)):
        sheet.topic_layout(link['file_id'])
        link['single_topic_layout'] = True
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
        # A recovered preimage must remain available until the user chooses it.
        if any(str(c["card_id"]) == cid and c.get("recovered") for c in ws["conflicts"].values()):
            continue
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
               "local_hash": subject_hash(ws, subject), "imported_ids": imported_ids,
               "owner": owner, "previous_job": link.get("last_write", {}).get("id"), "at": time.time()}
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


def recover_preimage(user, subject, job, receipt, expected_after):
    """Surface edits captured atomically immediately before the API changed cells."""
    if normalized(receipt["before"]) == normalized(job["before"]):
        return
    ws = user["workspace"]
    link = user["google"]["links"][subject]
    actual = deepcopy(receipt["before"])
    observed = deepcopy(job["before"])
    try:
        # Content edits have an unambiguous identity. A concurrent row move/insert
        # needs recovery from the complete snapshots, never a guessed row identity.
        if len(actual) != len(job["before"]):
            raise w.Problem("Строки перемещены во время записи")
        for i, (left, right) in enumerate(zip(actual, job["before"])):
            a, b = list(left) + [""] * 11, list(right) + [""] * 11
            if any(a[j] != b[j] for j in (0, 1, 2, 3, 4, 8)):
                raise w.Problem("Структура строк изменена во время записи")
            if i and not a[0] and any(str(v).strip() for v in a):
                rendered = expected_after[i]
                for j in (0, 1, 2, 8, 10):
                    a[j] = rendered[j]
                    b[j] = rendered[j]
                actual[i] = a[:11]
                observed[i] = b[:11]
        incoming, _ = parse_rows(deepcopy(ws), job["owner"], subject, link["file_id"], actual)
        remote_after, _ = parse_rows(deepcopy(ws), job["owner"], subject, link["file_id"], receipt["after"])
        expected, _ = parse_rows(deepcopy(ws), job["owner"], subject, link["file_id"], observed)
        for cid, proposal in incoming.items():
            local = w.content(ws["cards"][cid])
            # Metadata assignment may have preserved a newly edited cell already;
            # still present its changed content to the app without silently losing it.
            if proposal != expected.get(cid) and proposal != local:
                conflict_id = "conflict_" + w.digest([job["id"], cid, proposal])[:32]
                ws["conflicts"][conflict_id] = {
                    "id": conflict_id, "source": "google", "recovered": True,
                    "card_id": cid, "current": local, "proposed": proposal,
                    "remote_baseline": remote_after.get(cid, job["base"].get(cid)),
                    "revision": ws["cards"][cid]["revision"]}
        link["base"] = remote_after
    except (w.Problem, KeyError, IndexError):
        link["recovery"] = {"job_id": job["id"], **deepcopy(receipt)}
        link["recovery_job"] = job["id"]
        raise w.Problem("Строки таблицы изменились во время обновления. Все версии сохранены в резервных копиях; обновление приостановлено.", 409) from None


def prepare_recovery(user, owner, subject, job_id):
    """Build a fresh personal sheet from preserved rows; keep the old file intact."""
    google = user["google"]
    link = google["links"].get(subject)
    if google.get("recovered_jobs", {}).get(job_id) == subject:
        return {"ok": True, "pending_sync": True}
    if not link or link.get("recovery_job") != job_id:
        raise w.Problem("Состояние таблицы изменилось. Обновите страницу.", 409)
    ws = deepcopy(user["workspace"])
    # Unacknowledged imports have no study history. Parse their original rows once
    # with fresh IDs, instead of keeping IDs written against moved row positions.
    for cid in link["sync_job"].get("imported_ids", []):
        if ws["cards"].get(cid, {}).get("import_pending"):
            del ws["cards"][cid]
    for conflict in ws["conflicts"].values():
        if ws["cards"].get(str(conflict["card_id"]), {}).get("subject_id") == subject:
            conflict["source"] = "app"
            conflict.pop("recovered", None)
            conflict.pop("remote_baseline", None)
    remote, _ = parse_rows(ws, owner, subject, link["file_id"], link["recovery"]["before"], job_id)
    observed_rows = [row for row in link["sync_job"]["before"][1:] if row and row[0]]
    observed, _ = parse_rows(deepcopy(ws), owner, subject, link["file_id"], [HEADERS] + observed_rows)
    for cid in set(remote) | set(observed):
        card = ws["cards"][cid]
        local, before = w.content(card), observed.get(cid)
        incoming = remote.get(cid) or dict(before, deleted=True)
        if incoming == local or incoming == before:
            continue
        if before is None or local == before:
            w.change_card(ws, card, incoming, incoming.get("deleted", False))
            if not card["deleted"]:
                ws["decks"][card["deck_id"]]["archived"] = False
        else:
            conflict_id = "conflict_" + w.digest([job_id, cid, incoming])[:32]
            # The new sheet exports the current workspace. Its independent saved
            # variant is resolved just like a simultaneous edit in another app tab.
            ws["conflicts"][conflict_id] = {"id": conflict_id, "card_id": cid, "source": "app",
                "current": local, "proposed": incoming, "revision": card["revision"]}
    google.setdefault("archives", {})[job_id] = deepcopy(link)
    google.setdefault("recovered_jobs", {})[job_id] = subject
    google.setdefault("generation", {})[subject] = job_id
    google.setdefault("pending_subjects", {})[subject] = True
    del google["links"][subject]
    user["workspace"] = ws
    return {"ok": True, "pending_sync": True, "previous_url": link["url"]}


def flush_sync(user, subject, sheet, job_id):
    link = user["google"]["links"][subject]
    job = link.get("sync_job")
    if not job:
        return {"ok": True, "pending_sync": link.get("pending", False)}
    if job["id"] != job_id:
        raise w.Problem("Обновление уже изменилось. Повторите его.", 409)
    receipt = sheet.receipt(link["file_id"], job_id)
    current = sheet.read(link["file_id"])
    after = deepcopy(job["before"])
    for row, values in job["changes"]:
        while len(after) < row:
            after.append([])
        after[row-1] = values
    unchanged_local = job["local_hash"] == subject_hash(user["workspace"], subject)
    if receipt is None and (normalized(current) != normalized(job["before"]) or not unchanged_local):
        # Preserve the observed versions, then plan a fresh merge rather than write stale rows.
        for cid in job.get("imported_ids", []):
            card = user["workspace"]["cards"].get(cid)
            if card and card.get("import_pending"):
                del user["workspace"]["cards"][cid]
        link["last_write"] = link.pop("sync_job")
        link["pending"] = True
        return {"retry": True, "pending_sync": True}
    if receipt is None:
        sheet.write(link["file_id"], job["changes"], job["before"], job_id, job.get("previous_job"))
        receipt = sheet.receipt(link["file_id"], job_id)
        if receipt is None:
            raise w.Problem("Google не подтвердил сохранение резервной копии. Повторите обновление.", 503)
        current = sheet.read(link["file_id"])
    job["receipt"] = receipt
    link["base"] = job["base"]
    recover_preimage(user, subject, job, receipt, after)
    for cid in job.get("imported_ids", []):
        user["workspace"]["cards"][cid].pop("import_pending", None)
    changed_after = (normalized(current) != normalized(receipt["after"])
                     or normalized(receipt["after"]) != normalized(after))
    link.update(last_sync=time.time(), error=None,
                pending=changed_after or not unchanged_local or any(c["source"] == "google" and
                user["workspace"]["cards"][str(c["card_id"])]["subject_id"] == subject
                for c in user["workspace"]["conflicts"].values()))
    link["last_write"] = link.pop("sync_job")
    return {"ok": True, "retry": changed_after, "pending_sync": link["pending"]}


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
                generation = google.get("generation", {}).get(subject)
                identity_text = "flashcards/sheet/v1/" + name + "/" + subject + ("/" + generation if generation else "")
                identity = hmac.new(secret, identity_text.encode(), hashlib.sha256).hexdigest()
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

    @route("/google/subjects/<subject>/recover", ("POST",))
    def recover(name, user, ws, subject):
        w.subject_id(subject)
        if not user.get("google", {}).get("token"):
            raise w.Problem("Сначала подключите Google", 409)
        return prepare_recovery(user, name, subject, data().get("job_id"))
