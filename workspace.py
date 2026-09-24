"""Personal card workspaces. Pure functions; callers own the storage transaction."""
from copy import deepcopy
import hashlib
import json
import time
import uuid

SUBJECTS = [
    {"id": "anatomy", "name": "Анатомия"},
    {"id": "latin", "name": "Латынь"},
    {"id": "microbiology", "name": "Микробиология"},
]
SUBJECT_IDS = {s["id"] for s in SUBJECTS}


class Problem(Exception):
    def __init__(self, message, status=400, **details):
        self.message, self.status, self.details = message, status, details


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def text(value, label, limit=200, required=True):
    if not isinstance(value, str):
        raise Problem(f"Некорректное поле: {label}")
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise Problem(f"Заполните поле «{label}» (не более {limit} символов)")
    return value


def subject_id(value):
    if not isinstance(value, str) or value not in SUBJECT_IDS:
        raise Problem("Выберите доступный предмет")
    return value


def empty_workspace():
    return {"schema": 2, "topics": {}, "decks": {}, "cards": {},
            "copies": {}, "operations": {}, "conflicts": {}}


def ensure_topic(ws, subject, name, topic_id=None):
    subject_id(subject)
    name = text(name, "Тема")
    if topic_id:
        if not isinstance(topic_id, str):
            raise Problem("Некорректная тема")
        found = ws["topics"].get(topic_id)
        if not found or found["subject_id"] != subject:
            raise Problem("Тема не принадлежит выбранному предмету")
        return found
    for topic in ws["topics"].values():
        if topic["subject_id"] == subject and topic["name"] == name:
            return topic
    topic = {"id": uid("topic"), "subject_id": subject, "name": name}
    ws["topics"][topic["id"]] = topic
    return topic


def create_deck(ws, owner, data, author=None, origin=None):
    subject = subject_id(data.get("subject_id", "anatomy"))
    topic = ensure_topic(ws, subject, data.get("topic", "Без темы"), data.get("topic_id"))
    deck = {"id": uid("deck"), "name": text(data.get("name", ""), "Набор"),
            "subject_id": subject, "topic_id": topic["id"], "author": author or owner,
            "origin": origin, "revision": 1, "archived": False, "published_id": None}
    ws["decks"][deck["id"]] = deck
    return deck


def migrate_user(user, base, owner):
    if user.get("workspace", {}).get("schema") == 2:
        return False
    # Keep the exact input before transforming anything. Never send this to clients.
    user.setdefault("legacy_backup_v1", deepcopy({k: v for k, v in user.items()
                                               if k != "legacy_backup_v1"}))
    ws = empty_workspace()
    deleted = {str(c) for c in user.get("deleted", [])}
    decks = {}
    for original in list(base) + user.get("added", []):
        c = deepcopy(original)
        c.update(user.get("edited", {}).get(str(c["id"]), {}))
        name = c.get("topic") or "Без темы"
        if name not in decks:
            decks[name] = create_deck(ws, owner, {"name": name, "topic": name,
                                                "subject_id": "anatomy"})
        deck = decks[name]
        c.update(subject_id="anatomy", topic_id=deck["topic_id"], deck_id=deck["id"],
                 author=owner, revision=1, deleted=str(c["id"]) in deleted,
                 updated_at=time.time())
        ws["cards"][str(c["id"])] = c
    user["workspace"] = ws
    return True


def get_deck(ws, did, active=True):
    deck = ws["decks"].get(str(did))
    if not deck or (active and deck.get("archived")):
        raise Problem("Набор не найден", 404)
    return deck


def get_card(ws, cid, active=True):
    card = ws["cards"].get(str(cid))
    if not card or (active and (card.get("import_pending") or card.get("deleted") or get_deck(ws, card["deck_id"], False).get("archived"))):
        raise Problem("Карточка не найдена", 404)
    return card


def active_cards(ws):
    return [c for c in ws["cards"].values() if not c.get("deleted") and not c.get("import_pending")
            and not ws["decks"][c["deck_id"]].get("archived")]


def check_revision(obj, expected):
    if expected != obj["revision"]:
        raise Problem("Эта запись уже изменилась. Обновите её перед сохранением.", 409,
                      current=deepcopy(obj), code="VERSION_CONFLICT")


def card_values(data):
    from card_media import FIELDS, valid_id
    result = {"q": text(data.get("q", ""), "Вопрос", 20000, not bool(data.get("q_image"))),
              "a": text(data.get("a", ""), "Ответ", 40000, not bool(data.get("a_image"))),
              "source": text(data.get("source", ""), "Источник", 2000, False)}
    for field in FIELDS:
        if field in data:
            value = data[field]
            if value != "" and not valid_id(value):
                raise Problem("Некорректное изображение")
            result[field] = value
    return result


def create_card(ws, deck, data, cid=None):
    card = dict(card_values(data), id=cid or uid("card"), subject_id=deck["subject_id"],
                topic_id=deck["topic_id"], topic=ws["topics"][deck["topic_id"]]["name"],
                deck_id=deck["id"], author=deck["author"], revision=1,
                deleted=False, updated_at=time.time())
    if str(card["id"]) in ws["cards"]:
        raise Problem("Карточка с таким идентификатором уже существует", 409)
    ws["cards"][str(card["id"])] = card
    deck["revision"] += 1
    return card


def change_card(ws, card, data=None, deleted=None):
    if data is not None:
        card.update(card_values(dict(card, **data)))
    if deleted is not None:
        card["deleted"] = deleted
    card["revision"] += 1
    card["updated_at"] = time.time()
    ws["decks"][card["deck_id"]]["revision"] += 1
    return card


def content(card):
    """Fields shared with Sheets. Progress, ownership and revisions stay server-side."""
    result = {k: deepcopy(card.get(k)) for k in
              ("id", "deck_id", "topic_id", "subject_id", "topic", "q", "a", "source", "deleted")}
    result["source"] = card.get("source") or ""
    for field in ("q_image", "a_image"):
        if card.get(field):
            result[field] = card[field]
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def conflict_view(conflict):
    return dict(conflict, version=digest([conflict["revision"], conflict["current"], conflict["proposed"]]))


def rebase(before, proposed, current):
    """Preserve unrelated writes made while this request was waiting for Google."""
    if proposed == before:
        return deepcopy(current)
    if current == before or current == proposed:
        return deepcopy(proposed)
    if all(isinstance(value, dict) for value in (before, proposed, current)):
        missing = object()
        result = {}
        for key in before.keys() | proposed.keys() | current.keys():
            old, new, latest = before.get(key, missing), proposed.get(key, missing), current.get(key, missing)
            if new == old:
                chosen = latest
            elif latest == old or latest == new:
                chosen = new
            elif all(isinstance(value, dict) for value in (old, new, latest)):
                chosen = rebase(old, new, latest)
            else:
                raise Problem("Во время обновления появились новые правки. Повторите обновление.", 409)
            if chosen is not missing:
                result[key] = deepcopy(chosen)
        return result
    raise Problem("Во время обновления появились новые правки. Повторите обновление.", 409)


def operation(ws, key, payload, execute):
    key = text(key, "Идентификатор действия", 128)
    fingerprint = digest(payload)
    previous = ws["operations"].get(key)
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise Problem("Идентификатор уже использован для другого действия", 409)
        return deepcopy(previous["result"])
    result = execute()
    ws["operations"][key] = {"fingerprint": fingerprint, "result": deepcopy(result)}
    return result


def study_summary(ws, srs, subject=None, topic=None, mode="due", now=None, deck_id=None):
    if subject is not None:
        subject_id(subject)
    if mode not in ("due", "all"):
        raise Problem("Выберите режим изучения")
    if topic and (topic not in ws["topics"] or ws["topics"][topic]["subject_id"] != subject):
        raise Problem("Тема не найдена", 404)
    if deck_id:
        deck = get_deck(ws, deck_id)
        if subject and deck['subject_id'] != subject:
            raise Problem("Тема не принадлежит выбранному предмету", 404)
    now = time.time() if now is None else now
    cards = [c for c in active_cards(ws) if (not subject or c["subject_id"] == subject)
             and (not topic or c["topic_id"] == topic)
             and (not deck_id or c["deck_id"] == deck_id)]
    due = [c for c in cards if str(c["id"]) in srs and srs[str(c["id"])].get("due", float("inf")) <= now]
    stats = {rating: sum(srs.get(str(c["id"]), {}).get("last") == rating for c in cards)
             for rating in ("know", "dontknow", "unsure")}
    return {"total": len(cards), "due": len(due), "stats": stats,
            "ids": [c["id"] for c in (due if mode == "due" else cards)]}
