# Сворачиваемые секции фильтров — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить секции «Коллекции / Модели / Фоны» в форме инструмента в компактные сворачиваемые панели с автооткрытием по правилам выбора.

**Architecture:** Чисто фронтендовая правка в двух файлах: `webapp/js/app.js` (разметка и логика состояний) и `webapp/css/tokens.css` (стили панелей). Данные, API и логика фильтров не меняются. Открытость панели — производное состояние: `available ? (sectionOpen[key] ?? true) : false`, где `sectionOpen` — словарь ручных тапов в `state.overlay`.

**Tech Stack:** Ванильный JS (без сборщика), CSS custom properties, Telegram Mini App webview. Проверка синтаксиса — `node --check`. Поведение — мануально в Telegram (initData не позволяет открыть приложение вне Telegram).

## Global Constraints

- Меняются только `webapp/js/app.js` и `webapp/css/tokens.css`.
- Репозиторий ещё не имеет конфигурации git identity — каждый коммит обязан передавать identity через переменные окружения:
  `GIT_AUTHOR_NAME=pexepo GIT_AUTHOR_EMAIL=pexepo@local GIT_COMMITTER_NAME=pexepo GIT_COMMITTER_EMAIL=pexepo@local git commit -m "..."`
- JS-тестов в проекте нет (тесты — только Python в `tests/`). Единица проверки — `node --check webapp/js/app.js` (синтаксис) плюс мануальная проверка в мини-аппе.
- Правила доступности секций (одинаковые для всех инструментов):
  - Коллекции — всегда доступны и всегда раскрыты, тапом не сворачиваются.
  - Модели — доступны только при выборе ровно одной коллекции; при 0 или >1 коллекциях панель закрыта и не раскрывается, в шапке подсказка.
  - Фоны — доступны только при выборе ≥1 модели; при 0 моделях панель закрыта и не раскрывается, в шапке подсказка.
- Автоматика: смена выбора коллекции сбрасывает ручные переключатели «Модели» и «Фоны»; смена модели сбрасывает переключатель «Фоны». После сброса секции снова подчиняются правилу доступности.
- Стиль кода — как в окружающем файле (без новых пространных комментариев; короткие поясняющие — только где логика неочевидна).
- Спека: `docs/superpowers/specs/2026-08-13-collapsible-facet-sections-design.md`.

---

### Task 1: Разметка панелей и производное состояние в `toolFacetSection()`

**Files:**
- Modify: `webapp/js/app.js:553-594` (функция `toolFacetSection`)
- Modify: `webapp/js/app.js:1227-1239` (объект `state.overlay` в `openTool`)

**Interfaces:**
- Consumes: существующие `facetSearch()`, `facetCards()`, `collectionChips()`, `skeletons()`, `monochromeBackdrops()`, `f.isMultiKind`-логика в `state.collections`.
- Produces: `state.overlay.sectionOpen: Record<'model'|'backdrop', boolean>` — инициализируется пустым; `facetPanelOpen(o, key, available)` — читает состояние панели (используется Task 2).

- [ ] **Step 1: Инициализировать `sectionOpen` в `openTool()`**

В `src`-файле `webapp/js/app.js`, в объекте, который присваивается `state.overlay` (сейчас начинается с `mode: 'tool',`), добавить строку после `draft: defaults,`:

```js
sectionOpen: {},
```

- [ ] **Step 2: Переписать `toolFacetSection()` на панели**

Заменить целиком тело функции `toolFacetSection` (строки 553-594) на:

```js
/** Open state of one facet panel: available panels open by default and keep a
 * manual toggle; unavailable ones are forced closed by the rules below. */
function facetPanelOpen(o, key, available) {
  if (!available) return false;
  return o.sectionOpen[key] ?? true;
}

/** One collapsible facet panel: head (title, meta, chevron) + animated body. */
function facetPanel({ title, key, available, open, meta, hint, body, toggleable = true }) {
  const head = !toggleable
    ? `<div class="facet-panel__head">
         <span class="facet-panel__title">${title}</span>
         <span class="facet-panel__meta">${meta}</span>
       </div>`
    : !available
      ? `<div class="facet-panel__head">
           <span class="facet-panel__title">${title}</span>
           <span class="facet-panel__meta dim">${hint}</span>
         </div>`
      : `<button class="facet-panel__head" data-action="toggle-section" data-section="${key}"
                 aria-expanded="${open ? 'true' : 'false'}">
           <span class="facet-panel__title">${title}</span>
           <span class="facet-panel__meta">${meta}</span>
           <span class="facet-panel__chev" aria-hidden="true">▾</span>
         </button>`;
  return `<section class="card facet-panel">
    ${head}
    <div class="facet-panel__body${open ? ' is-open' : ''}"><div>${body}</div></div>
  </section>`;
}

function toolFacetSection(o) {
  const f = o.draft;
  const facets = o.facets;

  const collectionCount = Array.isArray(f.collection)
    ? f.collection.length
    : f.collection ? 1 : 0;
  const modelCount = Array.isArray(f.model) ? f.model.length : f.model ? 1 : 0;
  const backdropCount = Array.isArray(f.backdrop)
    ? f.backdrop.length
    : f.backdrop ? 1 : 0;
  const modelsAvailable = collectionCount === 1;
  const backdropsAvailable = modelsAvailable && modelCount > 0;
  const modelsOpen = facetPanelOpen(o, 'model', modelsAvailable);
  const backdropsOpen = facetPanelOpen(o, 'backdrop', backdropsAvailable);

  // Collections: the only always-open panel; no chevron, no tap target.
  const collectionMeta =
    collectionCount === 0 ? '—' : f.collectionAll ? 'все' : String(collectionCount);

  // Models: scoped to exactly one collection; select-all lives in the body
  // now (the head is a button and cannot nest a second button).
  const modelSelectAll =
    modelsAvailable && Array.isArray(f.model) && facets?.models?.length
      ? `<button class="btn btn--ghost select-all" data-action="select-all" data-select-group="model">${facets.models.every((m) => f.model.includes(m.name)) ? 'Снять все' : 'Выбрать все'}</button>`
      : '';
  const modelBody = !modelsAvailable
    ? ''
    : facets === null
      ? skeletons(3)
      : `${facetSearch('model', f.modelQuery, 'Найти модель')}${modelSelectAll}${facetCards('model', facets.models, f.model, f.modelQuery)}`;

  // Backdrops: scoped to at least one model; monochrome filter stays in body.
  const reco = facets ? monochromeBackdrops(facets.backdrops) : [];
  const shownBackdrops = f.monochromeOnly ? reco : facets?.backdrops ?? [];
  const backdropBody = !backdropsAvailable
    ? ''
    : facets === null
      ? skeletons(3)
      : `<div class="facet-tools">
           ${facetSearch('backdrop', f.backdropQuery, 'Найти фон')}
           <label class="filter-check">
             <input type="checkbox" data-toggle-field="monochromeOnly" ${f.monochromeOnly ? 'checked' : ''}>
             <span><strong>Только монохромные</strong><small>Фоны с минимальным перепадом цвета</small></span>
           </label>
         </div>
         ${Array.isArray(f.backdrop) ? `<button class="btn btn--ghost select-all" data-action="select-all" data-select-group="backdrop">${shownBackdrops.length && shownBackdrops.every((b) => f.backdrop.includes(b.name)) ? 'Снять все' : 'Выбрать все'}</button>` : ''}
         ${facetCards('backdrop', shownBackdrops, f.backdrop, f.backdropQuery)}`;

  return [
    facetPanel({
      title: 'Коллекции',
      available: true,
      open: true,
      meta: collectionMeta,
      body: collectionChips(f.collection, f.collectionQuery),
      toggleable: false,
    }),
    facetPanel({
      title: 'Модели',
      key: 'model',
      available: modelsAvailable,
      open: modelsOpen,
      meta: modelCount === 0 ? '—' : String(modelCount),
      hint: collectionCount === 0 ? 'сначала выбери коллекцию' : 'выбери одну коллекцию',
      body: modelBody,
    }),
    facetPanel({
      title: 'Фоны',
      key: 'backdrop',
      available: backdropsAvailable,
      open: backdropsOpen,
      meta: backdropCount === 0 ? '—' : String(backdropCount),
      hint: 'выбери модель',
      body: backdropBody,
    }),
  ].join('');
}
```

Проверить, что в файле не осталось ссылок на удалённые классы разметки `facet-section` / `facet-section--locked`:

```bash
grep -n "facet-section" webapp/js/app.js
```

Ожидаемо: пусто (классы останутся только в CSS до Task 3).

- [ ] **Step 3: Проверить синтаксис**

Run: `node --check webapp/js/app.js`
Expected: без вывода, exit 0.

- [ ] **Step 4: Мануальная проверка**

Запустить бэкенд (`uvicorn src.api.app:app --port 8000` из корня, доступ через бота в Telegram как обычно) и в мини-аппе: Трекинг (или любой инструмент) → форма.
Expected:
- Коллекции раскрыты всегда, с поиском и счётчиком («—» без выбора).
- Модели: при 0 коллекциях закрыты с подсказкой «сначала выбери коллекцию»; при выборе одной коллекции раскрываются; поиск работает.
- Фоны: закрыты с подсказкой «выбери модель», пока не выбрана модель; после выбора модели раскрываются.

- [ ] **Step 5: Commit**

```bash
git add webapp/js/app.js
GIT_AUTHOR_NAME=pexepo GIT_AUTHOR_EMAIL=pexepo@local GIT_COMMITTER_NAME=pexepo GIT_COMMITTER_EMAIL=pexepo@local git commit -m "feat: сворачиваемые панели фильтров — разметка"
```

---

### Task 2: Тап по шапке и автоматика сброса ручных переключателей

**Files:**
- Modify: `webapp/js/app.js:1321-1361` (клик-обработчик: ветки `collection-sort` и `select-all`)
- Modify: `webapp/js/app.js:1477-1528` (клик-обработчик: ветка `chip`)

**Interfaces:**
- Consumes: `state.overlay.sectionOpen` из Task 1; `data-action="toggle-section"` с `data-section="model"|"backdrop"` из разметки Task 1.
- Produces: поведение — тап по шапке валидной панели переключает её; смена коллекции/модели сбрасывает ручные переключатели.

- [ ] **Step 1: Ветка `toggle-section` в клик-обработчике**

В `webapp/js/app.js`, в `root.addEventListener('click', ...)`, сразу после ветки `collection-sort` (сейчас следует за `use-floor`) добавить:

```js
  if (action === 'toggle-section' && state.overlay) {
    const key = target.dataset.section;
    const draft = state.overlay.draft;
    const collectionCount = Array.isArray(draft.collection)
      ? draft.collection.length
      : draft.collection ? 1 : 0;
    const modelCount = Array.isArray(draft.model) ? draft.model.length : draft.model ? 1 : 0;
    const available = key === 'model'
      ? collectionCount === 1
      : collectionCount === 1 && modelCount > 0;
    if (!available) return;
    state.overlay.sectionOpen[key] = !(state.overlay.sectionOpen[key] ?? true);
    haptic();
    render();
    return;
  }
```

- [ ] **Step 2: Сброс переключателей в ветке `select-all`**

В ветке `select-all` (обработчик `action === 'select-all' && state.overlay`), перед `haptic(); render();` (сейчас после `if (group === 'collection') { loadFacets(); }`) добавить:

```js
      if (group === 'collection') {
        delete state.overlay.sectionOpen.model;
        delete state.overlay.sectionOpen.backdrop;
      } else if (group === 'model') {
        delete state.overlay.sectionOpen.backdrop;
      }
```

- [ ] **Step 3: Сброс переключателей в ветке `chip`**

В ветке `chip && state.overlay` (оба пути — массив и одиночный выбор), перед `haptic(); render();` добавить:

```js
      if (chip === 'collection') {
        delete state.overlay.sectionOpen.model;
        delete state.overlay.sectionOpen.backdrop;
      } else if (chip === 'model') {
        delete state.overlay.sectionOpen.backdrop;
      }
```

- [ ] **Step 4: Проверить синтаксис**

Run: `node --check webapp/js/app.js`
Expected: без вывода, exit 0.

- [ ] **Step 5: Мануальная проверка**

В мини-аппе, форма Трекинга (много-выбор):
- С одной выбранной коллекцией: тап по шапке «Модели» сворачивает и снова разворачивает (при повторном открытии выбор сохранён).
- Раскрыть «Модели» вручную нельзя при 0 или 2+ коллекциях (тап игнорируется).
- Выбрать вторую коллекцию → «Модели» схлопываются и выбор модели очищается (старое поведение), «Фоны» тоже схлопываются.
- Снять выбор до одной коллекции → «Модели» снова раскрываются автоматически.
- Свернуть «Фоны» тапом, затем снять модель → «Фоны» закрыты и остаются закрыты; выбрать модель заново → «Фоны» раскрываются автоматически.
- В форме Снайпинга (одиночный выбор) — то же: выбор коллекции раскрывает «Модели», выбор модели раскрывает «Фоны».

- [ ] **Step 6: Commit**

```bash
git add webapp/js/app.js
GIT_AUTHOR_NAME=pexepo GIT_AUTHOR_EMAIL=pexepo@local GIT_COMMITTER_NAME=pexepo GIT_COMMITTER_EMAIL=pexepo@local git commit -m "feat: автооткрытие и ручное сворачивание панелей фильтров"
```

---

### Task 3: Стили `.facet-panel` в `tokens.css`

**Files:**
- Modify: `webapp/css/tokens.css:586-596` (блок `.facet-section` / `.facet-section--locked`)
- Modify: `webapp/css/tokens.css:598-607` (блок `.facet-search`)

**Interfaces:**
- Consumes: классы разметки из Task 1: `.facet-panel`, `.facet-panel__head`, `.facet-panel__title`, `.facet-panel__meta`, `.facet-panel__chev`, `.facet-panel__body` (+ `.is-open`).
- Produces: стили для панелей. Никаких других файлов.

- [ ] **Step 1: Заменить блок `.facet-section`**

В `webapp/css/tokens.css` заменить:

```css
.facet-section {
  max-height: min(310px, 42vh);
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
}

.facet-section--locked {
  max-height: 130px;
  opacity: 0.72;
}
```

на:

```css
/* Collapsible facet panels (collections / models / backdrops). */
.facet-panel {
  padding: var(--s3);
  padding-bottom: var(--s2);
}

.facet-panel__head {
  display: flex;
  align-items: center;
  gap: var(--s2);
  width: 100%;
  min-height: 34px;
  margin-bottom: var(--s2);
  padding: 0;
  border: 0;
  background: none;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.facet-panel__title {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.02em;
}

.facet-panel__meta {
  margin-left: auto;
  color: var(--text-dim);
  font-size: 12px;
}

.facet-panel__chev {
  color: var(--text-dim);
  font-size: 12px;
  transition: transform var(--t-fast) var(--ease);
}

.facet-panel__head[aria-expanded="false"] .facet-panel__chev {
  transform: rotate(-90deg);
}

/* Collapsed by default, animated exactly like .toast__more: grid-template-rows
 * animates to auto; max-height cannot without hardcoding a wrong height. */
.facet-panel__body {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--t-slow) var(--ease);
}

.facet-panel__body.is-open { grid-template-rows: 1fr; }

.facet-panel__body > div {
  min-height: 0;
  overflow: hidden;
}
```

- [ ] **Step 2: Почистить `.facet-search` от sticky**

В `webapp/css/tokens.css` заменить в блоке `.facet-search`:

```css
.facet-search {
  position: sticky;
  top: calc(var(--s4) * -1);
  z-index: 3;
  display: flex;
  align-items: center;
  gap: var(--s2);
  padding: var(--s2) 0;
  background: var(--bg-secondary);
}
```

на:

```css
.facet-search {
  display: flex;
  align-items: center;
  gap: var(--s2);
  padding: var(--s2) 0;
  background: var(--bg-secondary);
}
```

(`position: sticky` не работает внутри `overflow: hidden` — контейнера коллапса, поэтому убран.)

- [ ] **Step 3: Проверить, что в CSS не осталось `.facet-section`**

```bash
grep -n "facet-section" webapp/css/tokens.css webapp/js/app.js
```

Expected: без совпадений.

- [ ] **Step 4: Мануальная проверка**

В мини-аппе, форма Трекинга:
- Шапки панелей компактные: заголовок + счётчик слева-справа, чеврок «▾» у «Модели» и «Фоны».
- Сворачивание анимируется (у «Модели»/«Фоны»), чеврок поворачивается.
- У «Коллекции» чеврока нет, панель всегда раскрыта.
- В закрытом состоянии тела нет: подсказка только в шапке.
- `prefers-reduced-motion` (настройка macOS): сворачивание без анимации.

- [ ] **Step 5: Commit**

```bash
git add webapp/css/tokens.css
GIT_AUTHOR_NAME=pexepo GIT_AUTHOR_EMAIL=pexepo@local GIT_COMMITTER_NAME=pexepo GIT_COMMITTER_EMAIL=pexepo@local git commit -m "style: стили сворачиваемых панелей фильтров"
```

---

## Self-Review

**Спека — покрытие:**
- Требование 1 (сворачиваемость, чеврок, анимация `grid-template-rows`): Task 1 (разметка) + Task 3 (CSS).
- Требование 2 (автооткрытие: коллекции всегда, модели при ровно 1 коллекции, фоны при ≥1 модели): Task 1 (`modelsAvailable` / `backdropsAvailable`, `facetPanelOpen`).
- Требование 3 (ручной тап только по валидной секции, запоминание): Task 2 (`toggle-section` ветка, `sectionOpen`).
- Требование 4 (сброс переключателей при смене коллекции/модели): Task 2 (ветки `chip` и `select-all`).
- Требование 5 (счётчики и подсказки в шапке): Task 1 (`meta` / `hint`).
- Поиск, который уже был во всех секциях — сохраняется (переносится в тело панели).

**Placeholder-скан:** кода в каждом шаге достаточно, «TBD»/«TODO» отсутствуют.

**Типы и имена:** `facetPanelOpen`, `facetPanel`, `sectionOpen`, `toggle-section` (action), `model`/`backdrop` (data-section) согласованы между Task 1, Task 2 и Task 3. Классы разметки `.facet-panel__*` совпадают с именами в CSS.