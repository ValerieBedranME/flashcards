"""Authenticated workspace API. All mutations run inside storage's transaction."""
from copy import deepcopy
from functools import wraps
import os
import secrets
import time

from flask import g, jsonify, request, session
from storage import load_document, save_document
import workspace as w


def install(app, host):
    def state():
        name = session.get("user")
        users = host.load_users()
        user = users.get(name)
        if not user or session.get("auth_version") != user.get("salt"):
            raise w.Problem("Войдите в свой профиль", 401)
        if request.args.get("user", name) != name:
            raise w.Problem("Нет доступа к чужому профилю", 403)
        if "stacks" in user:
            host.migrate_stacks_to_srs(user)
        user.setdefault("srs", {})
        if w.migrate_user(user, host.load_cards(), name):
            host.save_users(users)
        return name, users, user, user["workspace"]

    def route(path, methods=("GET",)):
        def decorate(fn):
            @app.route("/api/v2" + path, methods=list(methods), endpoint="workspace_" + fn.__name__)
            @wraps(fn)
            def endpoint(**kwargs):
                def persist():
                    latest_users = host.load_users()
                    latest = latest_users.get(name)
                    if not latest or latest.get("salt") != baseline.get("salt"):
                        raise w.Problem("Войдите в свой профиль заново", 401)
                    original = g.get("workspace_rebase", baseline)
                    merged = w.rebase(original, user, latest)
                    if latest.get("workspace") != original.get("workspace"):
                        for link in merged.get("google", {}).get("links", {}).values():
                            link["pending"] = True
                    latest_users[name] = merged
                    host.save_users(latest_users)
                try:
                    name, users, user, ws = state()
                    baseline = deepcopy(user)
                    if request.method != "GET":
                        expected = session.get("csrf", "")
                        if not expected or not secrets.compare_digest(expected, request.headers.get("X-CSRF-Token", "")):
                            raise w.Problem("Обновите страницу и повторите действие", 403)
                    result = fn(name, user, ws, **kwargs)
                    if request.method != "GET" or g.get("workspace_changed"):
                        persist()
                    return jsonify(result)
                except w.Problem as error:
                    if g.get("commit_storage_on_error"):
                        try:
                            persist()
                        except w.Problem as concurrent:
                            error = concurrent
                    return jsonify(error=error.message, **error.details), error.status
            return endpoint
        return decorate

    def data():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise w.Problem("Ожидаются данные формы")
        return value

    def library():
        return load_document("library", os.path.join(os.path.dirname(host.USERS_PATH), "library.json"))

    def save_library(value):
        save_document("library", os.path.join(os.path.dirname(host.USERS_PATH), "library.json"), value)

    def pending(user, subject):
        google = user.get("google", {})
        link = google.get("links", {}).get(subject)
        if link:
            link["pending"] = True
        elif google.get("token"):
            google.setdefault("pending_subjects", {})[subject] = True
        return bool(link or google.get("token"))

    @route("/bootstrap")
    def bootstrap(name, user, ws):
        session.setdefault("csrf", secrets.token_urlsafe(32))
        from google_sync import public_status
        decks = []
        for d in ws["decks"].values():
            if d.get("archived"):
                continue
            decks.append(dict(d, count=sum(c["deck_id"] == d["id"] for c in w.active_cards(ws))))
        return {"name": name, "csrf": session["csrf"], "subjects": w.SUBJECTS,
                "topics": list(ws["topics"].values()), "decks": decks,
                "cards": w.active_cards(ws), "srs": user["srs"],
                "google": public_status(user, app),
                "conflicts": [w.conflict_view(c) for c in ws["conflicts"].values()],
                "trash": [c for c in ws["cards"].values() if c.get("deleted")]}

    @route("/topics", ("POST",))
    def add_topic(name, user, ws):
        body = data()
        return w.operation(ws, body.get("operation_id", ""), ["topic", body],
                           lambda: w.ensure_topic(ws, body.get("subject_id"), body.get("name", "")))

    @route("/decks", ("POST",))
    def add_deck(name, user, ws):
        body = data()
        def execute():
            deck = w.create_deck(ws, name, dict(body, topic=body.get("name", ""), topic_id=None))
            pending(user, deck["subject_id"])
            return deck
        return w.operation(ws, body.get("operation_id", ""), ["deck", body], execute)

    @route("/decks/<did>", ("PATCH", "DELETE"))
    def edit_deck(name, user, ws, did):
        body = data()
        def execute():
            deck = w.get_deck(ws, did)
            w.check_revision(deck, body.get("revision"))
            if request.method == "DELETE":
                for c in list(w.active_cards(ws)):
                    if c["deck_id"] == did:
                        w.change_card(ws, c, deleted=True)
                deck["archived"] = True
            else:
                deck["name"] = w.text(body.get("name", ""), "Набор")
            deck["revision"] += 1
            pending(user, deck["subject_id"])
            return {"ok": True}
        return w.operation(ws, body.get("operation_id", ""), [request.method, did, body], execute)

    @route("/cards", ("POST",))
    def add_card(name, user, ws):
        body = data()
        def execute():
            deck = w.get_deck(ws, body.get("deck_id"))
            card = w.create_card(ws, deck, body)
            return {"card": card, "pending_sync": pending(user, card["subject_id"])}
        return w.operation(ws, body.get("operation_id", ""), ["card", body], execute)

    @route("/cards/<cid>", ("PATCH", "DELETE"))
    def edit_card(name, user, ws, cid):
        body = data()
        def execute():
            card = w.get_card(ws, cid, False)
            if card.get("import_pending"):
                raise w.Problem("Дождитесь завершения импорта", 409)
            if body.get("revision") != card["revision"]:
                proposed = dict(w.content(card), **(w.card_values(body) if request.method == "PATCH" else {}))
                proposed["deleted"] = request.method == "DELETE"
                conflict_id = "conflict_" + w.digest([cid, card["revision"], proposed])[:32]
                conflict = {"id": conflict_id, "card_id": card["id"],
                            "current": w.content(card), "proposed": proposed,
                            "revision": card["revision"], "source": "app"}
                ws["conflicts"][conflict["id"]] = conflict
                g.commit_storage_on_error = True
                raise w.Problem("Карточку уже изменили. Выберите вариант.", 409, conflict=w.conflict_view(conflict))
            w.get_card(ws, cid)
            w.change_card(ws, card, body if request.method == "PATCH" else None,
                          True if request.method == "DELETE" else None)
            return {"card": card, "pending_sync": pending(user, card["subject_id"])}
        return w.operation(ws, body.get("operation_id", ""), [request.method, cid, body], execute)

    @route("/cards/<cid>/restore", ("POST",))
    def restore_card(name, user, ws, cid):
        body = data()
        def execute():
            card = w.get_card(ws, cid, False)
            w.check_revision(card, body.get("revision"))
            w.get_deck(ws, card["deck_id"], False)["archived"] = False
            w.change_card(ws, card, deleted=False)
            return {"card": card, "pending_sync": pending(user, card["subject_id"])}
        return w.operation(ws, body.get("operation_id", ""), ["restore", cid, body], execute)

    @route("/conflicts/<conflict_id>", ("POST",))
    def resolve(name, user, ws, conflict_id):
        body = data()
        def execute():
            conflict = ws["conflicts"].get(conflict_id)
            if not conflict:
                raise w.Problem("Вариант уже выбран или запись не найдена", 404)
            if body.get("choice") not in ("current", "proposed"):
                raise w.Problem("Выберите один из двух вариантов")
            card = w.get_card(ws, conflict["card_id"], False)
            if card["revision"] != conflict["revision"]:
                conflict.update(current=w.content(card), revision=card["revision"])
                g.commit_storage_on_error = True
            visible = w.conflict_view(conflict)
            if body.get("version") != visible["version"]:
                raise w.Problem("Появилась ещё одна правка. Проверьте обновлённые варианты.", 409, conflict=visible)
            chosen = conflict[body["choice"]]
            w.change_card(ws, card, chosen, chosen.get("deleted", False))
            if not card["deleted"]:
                w.get_deck(ws, card["deck_id"], False)["archived"] = False
            if conflict["source"] == "google":
                user["google"]["links"][card["subject_id"]]["base"][str(card["id"])] = conflict.get("remote_baseline", conflict["proposed"])
            del ws["conflicts"][conflict_id]
            return {"ok": True, "pending_sync": pending(user, card["subject_id"])}
        return w.operation(ws, body.get("operation_id", ""), ["resolve", conflict_id, body], execute)

    @route("/study")
    def study(name, user, ws):
        summary = w.study_summary(ws, user["srs"], request.args.get("subject_id"),
                                  request.args.get("topic_id"), request.args.get("mode", "due"),
                                  deck_id=request.args.get("deck_id"))
        return dict(summary, cards=[w.get_card(ws, cid) for cid in summary["ids"]])

    @route("/review", ("POST",))
    def review(name, user, ws):
        body = data()
        def execute():
            card = w.get_card(ws, body.get("id"))
            rating = body.get("rating")
            if rating not in ("know", "dontknow", "unsure"):
                raise w.Problem("Выберите оценку")
            previous = user["srs"].get(str(card["id"]), {}).get("interval", 0)
            interval = (max(1, previous * 2) if previous else 1) if rating == "know" else (
                (max(1, previous // 2) if previous else 0) if rating == "unsure" else 0)
            rec = {"interval": interval, "due": time.time() + interval * host.DAY, "last": rating}
            user["srs"][str(card["id"])] = rec
            return rec
        return w.operation(ws, body.get("operation_id", ""), ["review", body], execute)

    @route("/library")
    def publications(name, user, ws):
        return [dict(p, taken=pid in ws["copies"], own=p["publisher"] == name)
                for pid, p in library().items()]

    @route("/decks/<did>/publish", ("POST",))
    def publish(name, user, ws, did):
        body = data()
        def execute():
            deck = w.get_deck(ws, did)
            w.check_revision(deck, body.get("revision"))
            link = user.get("google", {}).get("links", {}).get(deck["subject_id"])
            if link and (not user.get("google", {}).get("token") or link.get("pending") or not link.get("last_sync") or time.time() - link["last_sync"] > 120):
                raise w.Problem("Перед публикацией обновите связанную таблицу", 409)
            if any(c for c in ws["conflicts"].values() if w.get_card(ws, c["card_id"], False)["deck_id"] == did):
                raise w.Problem("Сначала выберите варианты спорных правок", 409)
            cards = [w.content(c) for c in w.active_cards(ws) if c["deck_id"] == did]
            if not cards:
                raise w.Problem("Сначала добавьте карточки в набор")
            pid = "pub_" + w.digest([name, did, deck["name"], deck["revision"], cards])[:32]
            pubs = library()
            pubs.setdefault(pid, {"id": pid, "name": deck["name"], "subject_id": deck["subject_id"],
                                  "topic": ws["topics"][deck["topic_id"]]["name"], "author": deck["author"],
                                  "publisher": name, "version": deck["revision"], "cards": cards,
                                  "count": len(cards), "created_at": time.time()})
            save_library(pubs)
            deck["published_id"] = pid
            return {"id": pid}
        return w.operation(ws, body.get("operation_id", ""), ["publish", did, body], execute)

    @route("/library/<pid>/copy", ("POST",))
    def copy_publication(name, user, ws, pid):
        body = data()
        def execute():
            pub = library().get(pid)
            if not pub:
                raise w.Problem("Публикация не найдена", 404)
            if pid in ws["copies"]:
                return {"deck": ws["decks"][ws["copies"][pid]],
                        "pending_sync": bool(user.get("google", {}).get("links", {}).get(pub["subject_id"], {}).get("pending"))}
            deck = w.create_deck(ws, name, pub, pub["author"], {"publication_id": pid,
                                 "version": pub["version"], "publisher": pub["publisher"]})
            for source in pub["cards"]:
                w.create_card(ws, deck, source)
            ws["copies"][pid] = deck["id"]
            return {"deck": deck, "pending_sync": pending(user, deck["subject_id"])}
        return w.operation(ws, body.get("operation_id", ""), ["copy", pid, body], execute)

    # Register Google endpoints using the same authentication and transaction guard.
    from google_sync import install as install_google
    install_google(app, host, route, data)
