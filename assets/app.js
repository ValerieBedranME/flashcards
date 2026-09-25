'use strict';
const root = document.getElementById('fc-tree-mock'),
  main = document.getElementById('content'),
  editor = document.getElementById('editor'),
  notice = document.getElementById('notice');
const state = {
  data: null,
  page: 'study',
  subject: null,
  topic: null,
  mode: 'due',
  deck: null,
  library: [],
  study: null,
  lastStudyOrders: new Map(),
  index: 0,
  flipped: false,
  auth: 'login',
  busy: false,
  syncing: false,
  generation: 0
};
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
} [c]));
const op = () => crypto.randomUUID();
function shuffledStudy(cards) {
  const result = [...cards];
  for (let index = result.length - 1; index > 0; index--) {
    const other = Math.floor(Math.random() * (index + 1));
    [result[index], result[other]] = [result[other], result[index]];
  }
  const key = result.map(card => String(card.id)).sort().join('|');
  const previous = state.lastStudyOrders.get(key);
  if (result.length > 1 && previous && result.every((card, index) => String(card.id) === previous[index])) {
    [result[0], result[1]] = [result[1], result[0]];
  }
  state.lastStudyOrders.set(key, result.map(card => String(card.id)));
  return result;
}
const imageUrl = id => '/api/v2/images/' + encodeURIComponent(id);
const imageView = (id, label) => id ? `<button type="button" class="fc-image" data-zoom="${esc(id)}" aria-label="Увеличить: ${esc(label)}"><img src="${imageUrl(id)}" alt="${esc(label)}" loading="lazy"><span>Увеличить изображение</span></button>` : '';
const cardInfo = c => `<details class="fc-card-info"><summary>Информация о карточке</summary><label>ID карточки<input readonly value="${esc(c.id)}" aria-label="ID карточки"></label><button type="button" class="fc-link" data-copy-id="${esc(c.id)}">Скопировать ID</button><span role="status" data-id-status></span></details>`;
const finishCheck = () => `<div class="fc-completion-art" aria-hidden="true"><svg class="fc-completion-check" viewBox="0 0 240 180" focusable="false"><defs><mask id="fc-check-reveal" maskUnits="userSpaceOnUse" x="0" y="0" width="240" height="180"><path class="fc-check-pen" d="M43 99 L90 135 L193 43" pathLength="1"/></mask></defs><g mask="url(#fc-check-reveal)"><path class="fc-check-shadow" d="M43 99 L90 135 L193 43"/><path class="fc-check-face" d="M43 99 L90 135 L193 43"/><path class="fc-check-light" d="M43 99 L90 135 L193 43"/></g></svg></div>`;
function imageField(side, card) {
  return `<label for="image-${side}">Картинка ${side === 'q' ? 'вопроса' : 'ответа'} (необязательно)</label><input id="image-${side}" type="file" accept="image/png,image/jpeg,image/webp" data-image-file="${side}"><input type="hidden" name="${side}_image" value="${esc(card?.[side+'_image']||'')}"><div data-image-preview="${side}">${imageView(card?.[side+'_image'],side==='q'?'Вопрос':'Ответ')}</div><button type="button" class="fc-link" data-remove-image="${side}">Убрать картинку</button>`;
}

function message(text = '', error = false) {
  notice.textContent = text;
  notice.hidden = !text;
  notice.classList.toggle('fc-error', error);
}
async function api(path, {
  method = 'GET',
  body,
  legacy = false
} = {}) {
  const generation = state.generation;
  const headers = {};
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (state.data?.csrf) headers['X-CSRF-Token'] = state.data.csrf;
  }
  let response;
  try {
    response = await fetch((legacy ? '/api' : '/api/v2') + path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch {
    throw new Error('Нет связи с сервером. Повтори действие; текст формы остаётся на экране.');
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error('Сервер не ответил. Повтори действие.');
  }
  if (!response.ok) {
    if (response.status === 401 && state.data && generation === state.generation) {
      clearAccount();
      render();
    }
    const e = new Error(result.error || 'Не удалось выполнить действие');
    e.status = response.status;
    e.details = result;
    if (result.reconnect_required && state.data && generation === state.generation) {
      state.data.google.reconnect_required = true;
      if (state.page === 'profile') render();
    }
    throw e;
  }
  return result;
}

function clearAccount() {
  state.generation++;
  state.lastStudyOrders.clear();
  Object.assign(state, {
    data: null,
    page: 'study',
    subject: null,
    topic: null,
    deck: null,
    library: [],
    study: null,
    auth: 'login'
  });
  delete state.reviewOperation;
  delete state.reviewRating;
  editor.close();
  editor.innerHTML = '';
  document.getElementById('image-viewer').close();
  document.querySelector('#image-viewer img').removeAttribute('src');
}
async function refresh() {
  const generation = state.generation,
    data = await api('/bootstrap');
  if (generation === state.generation) state.data = data;
}
const subjectName = id => state.data.subjects.find(s => s.id === id)?.name || '';
const topicName = id => state.data.decks.find(t => t.id === id)?.name || '';
const cardsIn = (subject = state.subject, topic = state.topic) => state.data.cards.filter(c => (!subject || c.subject_id === subject) && (!topic || c.deck_id === topic));
const due = cards => cards.filter(c => state.data.srs[String(c.id)]?.due <= Date.now() / 1000);
const count = cards => state.mode === 'due' ? due(cards).length : cards.length;
const cardCount = n => n + ' ' + (n % 100 >= 11 && n % 100 <= 14 ? 'карточек' : n % 10 === 1 ? 'карточка' : n % 10 >= 2 && n % 10 <= 4 ? 'карточки' : 'карточек');
const topicCount = n => n + ' ' + (n % 100 >= 11 && n % 100 <= 14 ? 'тем' : n % 10 === 1 ? 'тема' : n % 10 >= 2 && n % 10 <= 4 ? 'темы' : 'тем');
const row = (attr, name, meta, n = '') => `<button class="fc-row" ${attr}><span class="fc-row-text"><strong>${esc(name)}</strong><small>${esc(meta)}</small></span><span class="fc-row-count">${n}</span><span aria-hidden="true">›</span></button>`;
const errors = '<p class="fc-field-error" role="alert"></p>';
const controls = (label = 'Сохранить') => `<div class="fc-actions"><button type="button" class="fc-secondary" data-close>Отмена</button><button class="fc-primary">${label}</button></div>${errors}`;

function authView() {
  if (state.auth === 'forgot') return `<h2>Восстановление пароля</h2><form data-form="forgot"><label for="email">Email профиля</label><input id="email" name="email" type="email" autocomplete="email" required><label for="recovery-name">Имя профиля (если на email несколько профилей)</label><input id="recovery-name" name="name" autocomplete="username" maxlength="100"><button class="fc-primary" style="margin-top:16px">Отправить код</button>${errors}</form><form data-form="reset"><label for="code">Код из письма</label><input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required><label for="new-password">Новый пароль</label><input id="new-password" name="password" type="password" autocomplete="new-password" minlength="4" required><button class="fc-secondary" style="margin-top:16px">Сменить пароль</button>${errors}</form><button class="fc-link" data-auth="login">Вернуться ко входу</button>`;
  const reg = state.auth === 'register';
  return `<h2>${reg?'Создать профиль':'Войти в FlashCards'}</h2><p class="fc-muted">Свои карточки, темы и повторения</p><form data-form="${state.auth}"><label for="name">${reg?'Имя':'Имя или email'}</label><input id="name" name="name" autocomplete="username" maxlength="100" required>${reg?'<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email" required>':''}<label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="${reg?'new-password':'current-password'}" minlength="4" required><button class="fc-primary" style="margin-top:18px">${reg?'Зарегистрироваться':'Войти'}</button>${errors}</form><div class="fc-actions"><button class="fc-link" data-auth="${reg?'login':'register'}">${reg?'Уже есть профиль':'Создать профиль'}</button>${reg?'':'<button class="fc-link" data-auth="forgot">Забыли пароль?</button>'}</div>`;
}

function studyView() {
  if (state.study) {
    if (state.index >= state.study.length) return finishCheck() + '<h2>Занятие завершено</h2><p class="fc-muted">Оценки сохранены в твоём профиле</p><button class="fc-primary" style="margin-top:20px" data-end>К выбору карточек</button>';
    const c = state.study[state.index];
    return `<button class="fc-link" data-end>‹ К выбору карточек</button><h2>${esc(topicName(c.deck_id))}</h2><p class="fc-muted">${esc(subjectName(c.subject_id))} · карточка ${state.index+1} из ${state.study.length}</p><article class="fc-flash" aria-live="polite"><small class="fc-muted">${state.flipped?'Ответ':'Вопрос'}</small><div>${esc(state.flipped?c.a:c.q)}</div>${imageView(c[state.flipped?'a_image':'q_image'],state.flipped?'Ответ':'Вопрос')}</article><button class="fc-primary" style="margin-bottom:18px" data-flip>${state.flipped?'Вернуться к вопросу':'Показать ответ'}</button>${state.flipped&&c.source?`<p class="fc-body">${esc(c.source)}</p>`:''}<div class="fc-ratings">${[["dontknow","Не знаю"],["unsure","Не уверен"],["know","Знаю"]].map(([key,label])=>`<button class="fc-rating-${key}" data-rating="${key}" ${state.reviewRating&&state.reviewRating!==key?"disabled":""}>${state.reviewRating===key?"Повторить: ":""}${label}</button>`).join("")}</div>${cardInfo(c)}`;
  }
  const selected = cardsIn(),
    n = count(selected),
    label = state.topic ? topicName(state.topic) : state.subject ? subjectName(state.subject) : 'Все предметы';
  const crumbs = state.subject ? `<div class="fc-crumbs"><button data-all>Все предметы</button><span>›</span><button data-subject="${state.subject}">${esc(subjectName(state.subject))}</button>${state.topic?`<span>›</span><span>${esc(topicName(state.topic))}</span>`:''}</div>` : '';
  const visibleDecks = state.data.decks.filter(deck => cardsIn(deck.subject_id, deck.id).length);
  const rows = state.topic ? '' : state.subject ? visibleDecks.filter(t => t.subject_id === state.subject).map(t => row(`data-topic="${t.id}"`, t.name, `${cardCount(cardsIn(state.subject,t.id).length)} · ${t.author}`, count(cardsIn(state.subject, t.id)))).join('') : state.data.subjects.map(s => row(`data-subject="${s.id}"`, s.name, `${topicCount(visibleDecks.filter(t=>t.subject_id===s.id).length)} · ${cardCount(cardsIn(s.id,null).length)}`, count(cardsIn(s.id, null)))).join('');
  const stats = {
    know: 0,
    dontknow: 0,
    unsure: 0
  };
  selected.forEach(c => {
    const last = state.data.srs[String(c.id)]?.last;
    if (last in stats) stats[last]++;
  });
  return `<h2>Начать изучение</h2><p class="fc-muted">Выбери, что повторить сегодня</p><div class="fc-segment" aria-label="Режим изучения"><button data-mode="due" aria-pressed="${state.mode==='due'}">Пора повторить</button><button data-mode="all" aria-pressed="${state.mode==='all'}">Все карточки</button></div>${crumbs}<section class="fc-scope"><div class="fc-scope-head"><div><h3>${esc(label)}</h3><p class="fc-muted" style="margin-top:4px">${state.mode==='due'?'Карточки, у которых подошёл срок':'Все твои карточки в этом разделе'}</p></div><span class="fc-count">${n}</span></div>${n?`<button class="fc-primary" data-start>${state.mode==='due'?'Повторить':'Изучать'} · ${cardCount(n)}</button>`:selected.length?'<p class="fc-muted">Сейчас повторять нечего</p><button class="fc-link" data-mode="all">Посмотреть все карточки</button>':state.data.google.connected && state.data.google.workbook?.ready?'<p class="fc-muted">Здесь пока нет карточек. Добавь их в таблицу и обнови её.</p><button class="fc-link" data-page="profile">Моя таблица</button>':'<p class="fc-muted">Здесь пока нет карточек</p><button class="fc-link" data-new-deck>Создать тему</button>'}<div class="fc-progress"><span class="fc-rating-know">Знаю: ${stats.know}</span><span class="fc-rating-dontknow">Не знаю: ${stats.dontknow}</span><span class="fc-rating-unsure">Не уверен: ${stats.unsure}</span></div></section>${rows?`<p class="fc-kicker">${state.subject?'Темы предмета':'Выбрать предмет'} · ${state.mode==='due'?'пора повторить':'все карточки'}</p><div class="fc-list">${rows}</div>`:''}`;
}

function mineView() {
  if (state.deck) {
    const deck = state.data.decks.find(d => d.id === state.deck);
    if (!deck) {
      state.deck = null;
      return mineView();
    }
    const cards = state.data.cards.filter(c => c.deck_id === deck.id);
    return `<button class="fc-link" data-decks>‹ Мои темы</button><h2>${esc(deck.name)}</h2><p class="fc-muted">${esc(subjectName(deck.subject_id))}</p><p class="fc-status">Автор темы: ${esc(deck.author)}</p><div class="fc-actions"><button class="fc-primary" data-new-card="${deck.id}">Добавить карточку</button><button class="fc-secondary" data-publish="${deck.id}">Опубликовать</button></div><div class="fc-actions"><button class="fc-link" data-rename="${deck.id}">Название темы</button><button class="fc-link" data-delete-deck="${deck.id}">Удалить тему</button></div>${cards.length?cards.map(c=>`<section class="fc-panel"><h3>${esc(c.q)}</h3>${imageView(c.q_image,"Вопрос")}<div class="fc-body">${esc(c.a)}</div>${imageView(c.a_image,"Ответ")}${cardInfo(c)}<div class="fc-actions"><button class="fc-secondary" data-edit-card="${c.id}">Изменить</button><button class="fc-secondary" data-delete-card="${c.id}">Удалить</button></div></section>`).join(''):'<p class="fc-empty fc-muted">В теме пока нет карточек</p>'}`;
  }
  const sheetReady = state.data.google.connected && state.data.google.workbook?.ready;
  const decks = sheetReady ? state.data.decks.filter(d => d.count) : state.data.decks;
  return `<h2>Мои темы</h2><p class="fc-muted">Нажми на название темы, чтобы открыть и изменить её, или опубликуй тему в библиотеке</p>${sheetReady?'':'<button class="fc-primary" style="margin-top:18px" data-new-deck>＋ Создать тему</button>'}${decks.length?decks.map(d=>`<section class="fc-panel"><p class="fc-muted">${esc(subjectName(d.subject_id))}</p><h3 style="margin-top:6px"><button class="fc-topic-title" data-deck="${esc(d.id)}" aria-label="Открыть тему ${esc(d.name)}">${esc(d.name)}</button></h3><p class="fc-status">${cardCount(d.count)} · Автор темы: ${esc(d.author)}</p>${d.origin?'<span class="fc-tag" style="margin-top:9px">Моя независимая копия</span>':''}<button class="fc-secondary" style="margin-top:14px" data-publish="${esc(d.id)}">Опубликовать тему</button></section>`).join(''):`<p class="fc-empty fc-muted">${sheetReady?'Добавь тему и карточки в таблицу, затем обнови её в профиле.':'Создай первую тему или возьми готовую в библиотеке.'}</p>`}`;
}

function libraryView() {
  return `<h2>Библиотека</h2><p class="fc-muted">Темы, которыми поделились участники</p>${state.library.length?state.library.map(p=>`<section class="fc-panel"><div class="fc-line"><span class="fc-tag">${esc(subjectName(p.subject_id))}</span><span class="fc-muted">${cardCount(p.count)}</span></div><h3 style="margin-top:12px">${esc(p.name)}</h3><p class="fc-status">Автор: ${esc(p.author)} · версия ${p.version}</p><button class="fc-link" data-preview="${p.id}">Посмотреть карточки</button>${p.own?'<p class="fc-status">Моя публикация · видна в библиотеке</p>':p.taken?'<p class="fc-status">Уже в моих темах</p>':`<button class="fc-primary" data-copy="${p.id}">Взять себе</button>`}</section>`).join(''):'<p class="fc-empty fc-muted">Публикаций пока нет. Готовой темой можно поделиться из раздела «Мои темы».</p>'}`;
}

function profileView() {
  const google = state.data.google;
  const connection = !google.connected
    ? '<button class="fc-primary" style="margin-top:15px" data-connect>Подключить Google</button>'
    : google.reconnect_required
      ? '<p class="fc-status">Доступ к Google прервался. Переподключи аккаунт, чтобы обновлять таблицу.</p><button class="fc-primary" style="margin-top:15px" data-connect>Переподключить Google</button>'
      : '<p class="fc-status">Google подключён</p>';
  return `<h2>Мой профиль</h2><p class="fc-muted">${esc(state.data.name)}</p><section class="fc-panel"><h3>Google Sheets</h3><p class="fc-muted" style="margin-top:8px">Добавь сразу много карточек через свою таблицу. Картинки для вопроса и ответа вставляй в столбцы L и M; они появятся в приложении после синхронизации.</p>${connection}${!google.configured?'<p class="fc-status">Подключение Google ещё настраивается. Карточки можно добавлять в приложении.</p>':''}${google.connected?'<button class="fc-secondary fc-profile-action" data-disconnect>Отключить Google</button>':''}</section>${google.connected?googleWorkbookView(google):''}${state.data.conflicts.length?`<section class="fc-panel"><h3>Выбрать правки</h3><p class="fc-muted">Карточек с двумя вариантами: ${state.data.conflicts.length}</p><button class="fc-secondary" style="margin-top:12px" data-conflicts>Посмотреть варианты</button></section>`:''}<button class="fc-secondary fc-profile-action fc-logout-action" data-logout>Выйти из профиля</button>`;
}

function googleWorkbookView(google) {
  const ready = google.workbook?.ready;
  const hasOld = Object.keys(google.links).length > 0;
  const links = state.data.subjects.map(subject => google.links[subject.id]);
  const lastSync = ready && links.every(link => link?.last_sync) ? Math.min(...links.map(link => link.last_sync)) : null;
  const updated = lastSync ? `Обновлено ${new Intl.DateTimeFormat('ru-RU', {day:'2-digit', month:'2-digit', year:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false}).format(new Date(lastSync * 1000)).replace(',', '')}` : 'Ожидает обновления';
  const errors = links.filter(link => link?.error).map(link => esc(link.error));
  const status = `<p class="fc-status">${updated}</p>${errors.map(error => `<p class="fc-status fc-error">${error}</p>`).join('')}${links.some(link => link?.pending) ? '<p class="fc-status">Есть изменения для обновления</p>' : ''}`;
  const recovery = state.data.subjects.map(subject => recoveryButton(google.links[subject.id] || {}, subject.id)).join('');
  return `<section class="fc-panel"><h3>Моя таблица</h3>
    ${ready ? `<div class="fc-actions"><a href="${esc(google.workbook.url)}" target="_blank" rel="noopener noreferrer">Открыть таблицу</a><button class="fc-secondary" data-sync-all>Синхронизировать</button></div>${status}${recovery}` :
    `<p class="fc-status">${hasOld ? 'После объединения добавляй карточки в новый файл.' : 'Создай таблицу, чтобы добавлять карточки по предметам.'}</p><button class="fc-primary" data-workbook>${google.setting_up ? 'Продолжить объединение' : hasOld ? 'Объединить в одну таблицу' : 'Создать мою таблицу'}</button>${hasOld ? status : ''}${recovery}`}
    <p class="fc-status">Чтобы добавить предмет, создай вкладку с его названием и нажми «Синхронизировать». На вкладке заполняй тему, вопрос и ответ.</p></section>`;
}

function render() {
  document.getElementById('navigation').hidden = !state.data;
  root.querySelector('.fc-avatar').hidden = !state.data;
  root.querySelector('.fc-avatar').textContent = state.data?.name?.slice(0, 1) || '';
  root.querySelectorAll('.fc-nav button').forEach(b => {
    if (b.dataset.page === state.page) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  main.innerHTML = !state.data ? authView() : state.page === 'study' ? studyView() : state.page === 'mine' ? mineView() : state.page === 'library' ? libraryView() : profileView();
  if (!state.data || state.page === 'profile') {
    main.insertAdjacentHTML('beforeend', '<div class="fc-policy-links"><a href="/privacy">Данные и конфиденциальность</a><a href="/terms">Правила использования</a></div>');
  }
}

function openDialog(html) {
  editor.innerHTML = html;
  editor.showModal();
}

function conflictDialog(c) {
  if (c) openDialog(`<h2 id="editor-title">Какой вариант оставить?</h2>${['current','proposed'].map((k,i)=>`<section class="fc-panel"><h3>${i===0?'В приложении':c.source==='google'?'В таблице':'Другая правка'}</h3><p class="fc-body">${esc(c[k].q)}</p>${imageView(c[k].q_image,"Вопрос")}<p class="fc-body">${esc(c[k].a)}</p>${imageView(c[k].a_image,"Ответ")}${c[k].deleted?'<p>Карточка удалена</p>':''}<button class="fc-secondary" data-resolve="${c.id}" data-version="${c.version}" data-choice="${k}" style="margin-top:12px">Оставить этот вариант</button></section>`).join('')}<button class="fc-link" data-close>Выбрать позже</button>`);
}

function deckForm() {
  const subject = state.subject || 'anatomy';
  openDialog(`<h2 id="editor-title">Новая тема</h2><form data-form="deck" data-operation="${op()}"><label for="deck-subject">Предмет</label><select id="deck-subject" name="subject_id">${state.data.subjects.map(s=>`<option value="${s.id}" ${s.id===subject?'selected':''}>${esc(s.name)}</option>`).join('')}</select><label for="deck-name">Название темы</label><input id="deck-name" name="name" maxlength="200" required>${controls('Создать')}</form>`);
}

function cardForm(deckId, card = null) {
  openDialog(`<h2 id="editor-title">${card?'Изменить':'Новая карточка'}</h2><form data-form="card" data-operation="${op()}" data-deck="${esc(deckId)}" data-card="${card?.id||''}" data-revision="${card?.revision||''}"><label for="card-q">Вопрос</label><textarea id="card-q" name="q" maxlength="20000">${esc(card?.q||'')}</textarea>${imageField("q",card)}<label for="card-a">Ответ</label><textarea id="card-a" name="a" maxlength="40000">${esc(card?.a||'')}</textarea>${imageField("a",card)}<p class="fc-status">PNG, JPEG или WebP · до 2 МБ. На каждой стороне нужен текст или картинка. Картинка, загруженная здесь, видна в приложении; для таблицы вставь её также в столбец L или M.</p><label for="card-source">Источник (необязательно)</label><input id="card-source" name="source" maxlength="2000" value="${esc(card?.source||'')}">${controls()}</form>`);
}
async function syncSubject(subject, legacy = false) {
  if (!state.data.google.connected) return;
  if (!legacy && !state.data.google.workbook?.ready) await ensureWorkbook();
  for (let attempt = 0; attempt < 3; attempt++) {
    const prepared = await api('/google/subjects/' + subject + '/sync', {
      method: 'POST',
      body: {}
    });
    if (!prepared.job_id) return prepared;
    const result = await api('/google/subjects/' + subject + '/flush', {
      method: 'POST',
      body: {
        job_id: prepared.job_id
      }
    });
    if (!result.retry) return result;
  }
  throw new Error('Таблица продолжает меняться. Повтори обновление.');
}

async function ensureWorkbook() {
  // The previous setup request may have committed before its response was lost.
  await refresh();
  if (state.data.google.workbook?.ready) return;
  if (!state.data.google.setting_up) {
    for (const subject of Object.keys(state.data.google.links)) await syncSubject(subject, true);
  }
  for (let attempt = 0; attempt < state.data.subjects.length + Object.keys(state.data.google.links).length + 4; attempt++) {
    const result = await api('/google/workbook', {method: 'POST', body: {}});
    if (!result.pending_setup) { await refresh(); return; }
  }
  throw new Error('Подготовка таблицы продолжается. Нажми «Продолжить объединение» в профиле.');
}

async function syncWorkbook() {
  await ensureWorkbook();
  await api('/google/discover', {method: 'POST', body: {}});
  await refresh();
  if (state.subject && !state.data.subjects.some(subject => subject.id === state.subject)) {
    state.subject = null;
    state.topic = null;
  }
  for (const subject of state.data.subjects) await syncSubject(subject.id);
  await refresh();
}

function recoveryButton(link, subject) {
  return link.recovery_job ? `<button class="fc-secondary" data-recover="${subject}" data-job="${esc(link.recovery_job)}">Восстановить таблицу</button>` : '';
}
async function afterWrite(result, subject, mediaChanged = false) {
  await refresh();
  render();
  if (result?.pending_sync) {
    if (!state.data.google.connected) {
      message('Сохранено в приложении. Для обновления таблицы переподключи Google.');
      return;
    }
    message('Сохранено в приложении. Обновляю таблицу…');
    try {
      await syncSubject(subject);
      await refresh();
      render();
      message(state.data.google.links[subject]?.pending ? 'Выбери вариант правки в профиле.' : mediaChanged ? 'Текст сохранён в таблице. Картинку из приложения вставь в L/M таблицы, если она нужна там.' : 'Сохранено в приложении и таблице');
    } catch (e) {
      message(e.message + ' Изменения ожидают обновления таблицы.', true);
    }
  } else message('Сохранено');
}
root.addEventListener('submit', async event => {
  const form = event.target;
  if (!form.dataset.form) return;
  event.preventDefault();
  if (state.busy) return;
  state.busy = true;
  const button = event.submitter;
  if (button) button.disabled = true;
  const errorBox = form.querySelector('.fc-field-error');
  errorBox.textContent = '';
  const fields = Object.fromEntries(new FormData(form));
  try {
    const kind = form.dataset.form;
    if (['login', 'register', 'forgot', 'reset'].includes(kind)) {
      if (kind === 'reset') fields.email = document.getElementById('email').value;
      await api('/' + kind, {
        method: 'POST',
        body: fields,
        legacy: true
      });
      if (kind === 'login') {
        state.generation++;
        await refresh();
        render();
        message('');
      }
      if (kind === 'register') {
        state.auth = 'login';
        render();
        message('Профиль создан. Теперь войди.');
      }
      if (kind === 'forgot') message('Если профиль найден, код отправлен. Проверь почту.');
      if (kind === 'reset') {
        state.auth = 'login';
        render();
        message('Пароль изменён. Теперь войди.');
      }
    } else {
      fields.operation_id = form.dataset.operation;
      if (kind === 'deck') {
        const deck = await api('/decks', {
          method: 'POST',
          body: fields
        });
        editor.close();
        state.deck = deck.id;
        state.page = 'mine';
        await afterWrite(null, deck.subject_id);
      }
      if (kind === 'card') {
        fields.deck_id = form.dataset.deck;
        const cid = form.dataset.card,
          deck = state.data.decks.find(d => d.id === fields.deck_id);
        if (cid) fields.revision = Number(form.dataset.revision);
        const previous = cid ? state.data.cards.find(c => String(c.id) === cid) : null;
        const mediaChanged = ['q_image', 'a_image'].some(key => (fields[key] || '') !== (previous?.[key] || ''));
        const result = await api('/cards' + (cid ? '/' + encodeURIComponent(cid) : ''), {
          method: cid ? 'PATCH' : 'POST',
          body: fields
        });
        editor.close();
        await afterWrite(result, deck.subject_id, mediaChanged);
      }
      if (kind === 'rename') {
        fields.revision = Number(form.dataset.revision);
        await api('/decks/' + form.dataset.deck, {
          method: 'PATCH',
          body: fields
        });
        editor.close();
        await refresh();
        render();
        message('Название сохранено');
      }
    }
  } catch (e) {
    errorBox.textContent = e.message;
    if (!editor.open && state.data) message(e.message, true);
    if (e.details?.conflict) {
      message('Сохранены оба варианта. Выбери нужный.', true);
      await refresh();
      editor.close();
      conflictDialog(e.details.conflict);
    }
  } finally {
    state.busy = false;
    if (button?.isConnected) button.disabled = false;
  }
});
root.addEventListener('change', async event => {
  const input = event.target;
  if (!input.matches('[data-image-file]') || !input.files[0] || state.busy) return;
  const form = input.closest('form'), error = form.querySelector('.fc-field-error'), side = input.dataset.imageFile;
  const generation = state.generation;
  state.busy = true;
  form.querySelectorAll('button,input[type=file]').forEach(b=>b.disabled=true);
  error.textContent = 'Загружаю изображение…';
  try {
    const file = input.files[0];
    if (file.size > 2*1024*1024) throw new Error('Выбери изображение не больше 2 МБ.');
    const encoded = await new Promise((resolve,reject)=>{
      const reader = new FileReader();
      reader.onload=()=>resolve(reader.result.split(',')[1]);
      reader.onerror=()=>reject(new Error('Не удалось прочитать файл.'));
      reader.readAsDataURL(file);
    });
    const result = await api('/images',{method:'POST',body:{data:encoded}});
    if (generation !== state.generation || !form.isConnected) return;
    form.elements[side+'_image'].value=result.id;
    form.querySelector(`[data-image-preview="${side}"]`).innerHTML=imageView(result.id,side==='q'?'Вопрос':'Ответ');
    error.textContent='';
  } catch(e) { error.textContent=e.message; }
  finally { state.busy=false;input.value='';form.querySelectorAll('button,input[type=file]').forEach(b=>b.disabled=false); }
});
root.addEventListener('click', async event => {
  const b = event.target.closest('button');
  if (!b || b.disabled || (b.type === 'submit' && b.closest('form'))) return;
  if (b.hasAttribute('data-close-image')) { document.getElementById('image-viewer').close(); return; }
  if (b.dataset.zoom && state.data) {
    const viewer=document.getElementById('image-viewer');
    viewer.querySelector('img').src=imageUrl(b.dataset.zoom);
    viewer.showModal();return;
  }
  if (b.dataset.copyId) {
    const info=b.closest('.fc-card-info');
    try {await navigator.clipboard.writeText(b.dataset.copyId);info.querySelector('[data-id-status]').textContent='ID скопирован';}
    catch {info.querySelector('input').select();info.querySelector('[data-id-status]').textContent='ID выделен — скопируй его вручную';}
    return;
  }
  if (b.dataset.removeImage && !state.busy) {
    const form=b.closest('form'),side=b.dataset.removeImage;
    form.elements[side+'_image'].value='';form.querySelector(`[data-image-preview="${side}"]`).innerHTML='';return;
  }
  if (b.hasAttribute('data-close')) {
    editor.close();
    return;
  }
  if (b.dataset.auth) {
    state.auth = b.dataset.auth;
    message('');
    render();
    return;
  }
  if (!state.data || state.busy) return;
  state.busy = true;
  b.disabled = true;
  try {
    const d = b.dataset;
    if (d.page) {
      message('');
      state.page = d.page;
      state.deck = null;
      if (d.page === 'library') state.library = await api('/library');
      await refresh();
      render();
    }
    if ('all' in d) {
      state.subject = null;
      state.topic = null;
      render();
    }
    if (d.subject) {
      state.subject = d.subject;
      state.topic = null;
      render();
    }
    if (d.topic) {
      state.topic = d.topic;
      render();
    }
    if (d.mode) {
      state.mode = d.mode;
      render();
    }
    if ('start' in d) {
      delete state.reviewOperation;
      delete state.reviewRating;
      const p = new URLSearchParams({
        mode: state.mode
      });
      if (state.subject) p.set('subject_id', state.subject);
      if (state.topic) p.set('deck_id', state.topic);
      const result = await api('/study?' + p);
      state.study = shuffledStudy(result.cards);
      state.index = 0;
      state.flipped = false;
      render();
    }
    if ('flip' in d) {
      state.flipped = !state.flipped;
      render();
      main.querySelector('[data-flip]')?.focus({preventScroll:true});
    }
    if ('end' in d) {
      delete state.reviewOperation;
      delete state.reviewRating;
      state.study = null;
      await refresh();
      render();
    }
    if (d.rating) {
      const c = state.study[state.index];
      state.reviewOperation ||= op();
      state.reviewRating ||= d.rating;
      const rec = await api('/review', {
        method: 'POST',
        body: {
          id: c.id,
          rating: state.reviewRating,
          operation_id: state.reviewOperation
        }
      });
      delete state.reviewOperation;
      delete state.reviewRating;
      state.data.srs[String(c.id)] = rec;
      state.index++;
      state.flipped = false;
      render();
      main.querySelector('[data-flip],[data-end]')?.focus({preventScroll:true});
    }
    if ('newDeck' in d) deckForm();
    if ('decks' in d) {
      state.deck = null;
      render();
    }
    if (d.deck) {
      state.deck = d.deck;
      state.page = 'mine';
      render();
    }
    if (d.newCard) cardForm(d.newCard);
    if (d.editCard) {
      const c = state.data.cards.find(c => String(c.id) === d.editCard);
      cardForm(c.deck_id, c);
    }
    if (d.deleteCard) {
      const c = state.data.cards.find(c => String(c.id) === d.deleteCard);
      await afterWrite(await api('/cards/' + encodeURIComponent(c.id), {
        method: 'DELETE',
        body: {
          revision: c.revision,
          operation_id: op()
        }
      }), c.subject_id);
    }
    if (d.rename) {
      const deck = state.data.decks.find(x => x.id === d.rename);
      openDialog(`<h2 id="editor-title">Название темы</h2><form data-form="rename" data-deck="${deck.id}" data-revision="${deck.revision}" data-operation="${op()}"><label for="rename-name">Название</label><input id="rename-name" name="name" value="${esc(deck.name)}" maxlength="200" required>${controls()}</form>`);
    }
    if (d.deleteDeck) {
      const deck = state.data.decks.find(x => x.id === d.deleteDeck);
      if (confirm('Удалить тему и её карточки?')) {
        await api('/decks/' + deck.id, {
          method: 'DELETE',
          body: {
            revision: deck.revision,
            operation_id: op()
          }
        });
        state.deck = null;
        await afterWrite({
          pending_sync: !!state.data.google.links[deck.subject_id]
        }, deck.subject_id);
      }
    }
    if (d.publish) {
      message('Публикую тему…');
      const deck = state.data.decks.find(x => x.id === d.publish);
      await syncSubject(deck.subject_id);
      await refresh();
      const current = state.data.decks.find(x => x.id === d.publish);
      await api('/decks/' + current.id + '/publish', {
        method: 'POST',
        body: {
          revision: current.revision,
          operation_id: op()
        }
      });
      await refresh();
      render();
      message('Версия темы опубликована в библиотеке');
    }
    if (d.copy) {
      const p = state.library.find(x => x.id === d.copy),
        result = await api('/library/' + d.copy + '/copy', {
          method: 'POST',
          body: {
            operation_id: op()
          }
        });
      state.library = await api('/library');
      await afterWrite(result, p.subject_id);
    }
    if (d.preview) {
      const p = state.library.find(x => x.id === d.preview);
      openDialog(`<h2 id="editor-title">${esc(p.name)}</h2>${p.cards.map(c=>`<section class="fc-panel"><h3>${esc(c.q)}</h3>${imageView(c.q_image,"Вопрос")}<p class="fc-body">${esc(c.a)}</p>${imageView(c.a_image,"Ответ")}</section>`).join('')}<button class="fc-secondary" style="margin-top:15px" data-close>Закрыть</button>`);
    }
    if ('connect' in d) {
      const result = await api('/google/connect', {
        method: 'POST',
        body: {}
      });
      location.assign(result.url);
    }
    if ('workbook' in d || 'syncAll' in d) {
      message(state.data.google.workbook?.ready ? 'Обновляю таблицу…' : 'Подготавливаю одну таблицу с вкладками…');
      await syncWorkbook();
      render();
      message(Object.values(state.data.google.links).some(link => link.pending) ? 'Таблица готова. Проверь варианты правок в профиле.' : 'Все вкладки таблицы обновлены');
    }
    if ('disconnect' in d) {
      await api('/google/disconnect', {
        method: 'POST',
        body: {}
      });
      await refresh();
      render();
      message('Google отключён. Личные карточки сохранены.');
    }
    if (d.recover) {
      openDialog(`<h2 id="editor-title">Восстановить таблицу</h2><p>Создадим новую таблицу из сохранённых карточек. Если есть разные правки, ты сможешь выбрать нужную в профиле. Предыдущая таблица тоже сохранится.</p><div class="fc-actions"><button class="fc-primary" data-recover-confirm="${d.recover}" data-job="${esc(d.job)}">Создать новую таблицу</button><button class="fc-secondary" data-close>Отмена</button></div>`);
    }
    if (d.recoverConfirm) {
      const result = await api('/google/subjects/' + d.recoverConfirm + '/recover', {
        method: 'POST', body: {job_id: d.job}
      });
      editor.close();
      await afterWrite(result, d.recoverConfirm);
    }
    if ('conflicts' in d) {
      const c = state.data.conflicts[0];
      conflictDialog(c);
    }
    if (d.resolve) {
      const conflict = state.data.conflicts.find(c => c.id === d.resolve),
        c = [...state.data.cards, ...state.data.trash].find(c => String(c.id) === String(conflict.card_id));
      const result = await api('/conflicts/' + d.resolve, {
        method: 'POST',
        body: {
          choice: d.choice,
          version: d.version,
          operation_id: op()
        }
      });
      editor.close();
      await afterWrite(result, c.subject_id);
    }
    if ('logout' in d) {
      await api('/logout', {
        method: 'POST',
        legacy: true,
        body: {}
      });
      clearAccount();
      message('');
      render();
    }
  } catch (e) {
    message(e.message, true);
    if (state.reviewRating && state.study) render();
    if (e.details?.conflict) {
      await refresh();
      render();
      editor.close();
      conflictDialog(e.details.conflict);
    }
  } finally {
    state.busy = false;
    if (b.isConnected) b.disabled = false;
  }
});
async function automaticSync() {
  if (!state.data?.google.connected || state.syncing || state.busy || !state.data.google.workbook?.ready || editor.open || state.study || document.hidden) return;
  state.syncing = true;
  const generation = state.generation;
  try {
    await api('/google/discover', {method: 'POST', body: {}});
    await refresh();
    if (state.subject && !state.data.subjects.some(subject => subject.id === state.subject)) {
      state.subject = null;
      state.topic = null;
    }
    for (const subject of new Set([...Object.keys(state.data.google.links), ...(state.data.google.pending_subjects || [])])) {
      if (generation !== state.generation) return;
      await syncSubject(subject);
    }
    if (generation === state.generation) {
      await refresh();
      render();
    }
  } catch (e) {
    message(e.message, true);
  } finally {
    state.syncing = false;
  }
}
setInterval(automaticSync, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) automaticSync(); });
(async () => {
  try {
    await refresh();
    const p = new URLSearchParams(location.search);
    if (p.has('google')) {
      state.page = 'profile';
      message(p.get('google') === 'connected' ? 'Google подключён. Создай свою таблицу с вкладками по предметам.' : 'Подключение Google не завершено. Можно повторить.', p.get('google') !== 'connected');
      history.replaceState(null, '', location.pathname);
    }
  } catch (e) {
    if (e.status !== 401) message(e.message, true);
  }
  render();
  automaticSync();
})();
