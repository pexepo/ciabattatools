/* Ciabatta Tools -- mini-app shell.
 *
 * Plain DOM, no framework. A consequence of having no bundler: a framework from
 * a CDN would put a network round trip in front of first paint, and this runs on
 * mobile data inside Telegram's webview.
 *
 * State is one object and every screen renders from it. The rule is strict:
 * mutate `state`, then call `render()`. Nothing writes to the DOM outside a
 * render function -- except toast expansion, which is noted where it happens.
 */

import {
  api,
  ApiError,
  fmtTon,
  haptic,
  initTelegram,
  notifyHaptic,
  openTgLink,
  tg,
} from './api.js';

const state = {
  screen: 'catalog',
  me: null,
  // null means "not fetched yet", [] means "fetched and empty". That distinction
  // is what lets the UI show a skeleton instead of "nothing found".
  collections: null,
  listings: null,
  cursor: '',
  events: null,
  ciabattas: null,
  filters: { collection: [], model: [], backdrop: [], maxPriceTon: '' },
  cheapestFirst: true,
  error: null,
};

const root = document.getElementById('root');

/* --- helpers -------------------------------------------------------------- */

/** Escape text before it reaches innerHTML.
 *
 * Collection and model names come from the market, not from us. A name
 * containing a tag would otherwise run as markup, so every interpolation of
 * external text goes through this.
 */
function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

/** A gradient built from the two RGB24 backdrop colours the API returns. */
function backdropStyle(colors) {
  if (!Array.isArray(colors) || colors.length !== 2) return '';
  const hex = (n) => `#${(n & 0xffffff).toString(16).padStart(6, '0')}`;
  return `background:radial-gradient(circle at 50% 38%, ${hex(colors[0])}, ${hex(colors[1])})`;
}

const CDN = 'https://cdn.tgmrkt.io/';

function thumbHtml(item) {
  const src = item.thumb ? CDN + item.thumb : null;
  return `
    <div class="thumb" style="${backdropStyle(item.backdrop_colors)}">
      ${
        src
          ? // loading=lazy matters: a page is 30 images and the first screen
            // shows six of them.
            `<img src="${esc(src)}" alt="${esc(item.model)}" loading="lazy" decoding="async">`
          : ''
      }
    </div>`;
}

function rarityBadge(item) {
  if (!item.rarity) return '';
  // Per-mille is more useful than the band name when it is known: "0.5%" says
  // more than "legendary".
  const label = item.rarity_per_mille
    ? `${(item.rarity_per_mille / 10).toFixed(1)}%`
    : item.rarity;
  return `<span class="badge badge--${esc(item.rarity)}">${esc(label)}</span>`;
}

function skeletons(count, height = '') {
  const style = height ? `height:${height}` : 'aspect-ratio:1';
  return `<div class="skeleton" style="${style}"></div>`.repeat(count);
}

/* --- screens -------------------------------------------------------------- */

function screenCatalog() {
  if (state.listings === null) {
    return `<div class="stack">${filtersBar()}<div class="grid">${skeletons(6)}</div></div>`;
  }
  if (state.listings.length === 0) {
    return `
      <div class="stack">
        ${filtersBar()}
        <div class="empty">
          <div style="font-size:32px">🥖</div>
          <p>Ничего не нашлось.<br>Попробуй ослабить фильтры.</p>
        </div>
      </div>`;
  }

  const cards = state.listings
    .map(
      (item) => `
      <div class="card card--tap" data-listing="${esc(item.id)}" style="padding:var(--s2)">
        ${thumbHtml(item)}
        <div style="padding:var(--s2) var(--s1) 0">
          <div class="row row--between">
            <strong class="num">${fmtTon(item.price)}</strong>
            ${rarityBadge(item)}
          </div>
          <div class="dim" style="font-size:12px;margin-top:2px">
            ${esc(item.model || item.collection)}${item.number ? ` #${item.number}` : ''}
          </div>
          ${
            item.floor_collection
              ? `<div class="dim" style="font-size:11px">флор ${esc(item.floor_collection.ton)}</div>`
              : ''
          }
        </div>
      </div>`
    )
    .join('');

  return `
    <div class="stack">
      ${filtersBar()}
      <div class="grid">${cards}</div>
      ${state.cursor ? '<button class="btn btn--ghost" data-action="more">Показать ещё</button>' : ''}
    </div>`;
}

function filtersBar() {
  const active =
    state.filters.collection.length +
    state.filters.model.length +
    state.filters.backdrop.length;
  return `
    <div class="row row--between">
      <button class="btn btn--ghost" data-action="filters" style="flex:1">
        🔎 Фильтры${active ? ` · ${active}` : ''}
      </button>
      <button class="btn btn--ghost" data-action="sort">
        ${state.cheapestFirst ? '↑ дешевле' : '↓ дороже'}
      </button>
    </div>`;
}

const TOOL_LABELS = {
  tracker: 'Трекинг',
  ordering: 'Ордеринг',
  sniping: 'Снайпинг',
  automessage: 'Авто-сообщение',
};

function screenTools() {
  const tools = [
    ['tracker', '📡', 'Трекинг подарков', 'Новые улучшённые и сгоревшие в крафте — в реальном времени.'],
    ['ordering', '🎯', 'Авто-ордеринг', 'Заявки ниже флора на MRKT с автоперебиванием.'],
    ['sniping', '⚡', 'Авто-снайпинг', 'Автобай и автооффер по твоим условиям.'],
  ];

  const mine = state.ciabattas ?? [];

  return `
    <div class="stack">
      ${tools
        .map(
          ([kind, icon, title, text]) => `
        <div class="card card--tap" data-tool="${kind}">
          <div class="row">
            <div style="font-size:24px">${icon}</div>
            <div style="flex:1">
              <h3>${title}</h3>
              <div class="dim" style="font-size:13px">${text}</div>
            </div>
            <div class="dim">›</div>
          </div>
        </div>`
        )
        .join('')}

      <h2 style="margin-top:var(--s3)">Мои Чиабатты</h2>
      ${
        mine.length === 0
          ? '<div class="empty" style="padding:var(--s5)">Пока ни одной.<br>Выбери инструмент выше.</div>'
          : mine.map(ciabattaCard).join('')
      }
    </div>`;
}

function ciabattaCard(c) {
  const label = TOOL_LABELS[c.kind] ?? c.kind;
  return `
    <div class="card">
      <div class="row row--between">
        <div>
          <strong>${esc(c.title || label)}</strong>
          <div class="dim" style="font-size:12px">
            ${esc(label)}${c.max_price ? ` · до ${esc(c.max_price.ton)} TON` : ''}${
              c.quantity ? ` · ${c.filled}/${c.quantity}` : ''
            }
          </div>
        </div>
        <button class="btn ${c.active ? 'btn--danger' : 'btn--primary'}"
                data-toggle="${c.id}" style="min-height:36px;padding:0 var(--s3)">
          ${c.active ? 'Стоп' : 'Пуск'}
        </button>
      </div>
    </div>`;
}

const EVENT_ICONS = {
  gift_upgraded: '✨',
  gift_burned: '🔥',
  snipe_found: '👀',
  snipe_bought: '✅',
  offer_sent: '📨',
  order_filled: '🎯',
  order_outbid: '⚠️',
  guard_tripped: '🛑',
};

function screenFeed() {
  if (state.events === null) return `<div class="stack">${skeletons(4, '64px')}</div>`;
  if (state.events.length === 0) {
    return '<div class="empty"><div style="font-size:32px">🔔</div><p>Уведомлений пока нет.</p></div>';
  }
  return `<div class="stack">${state.events.map(eventToast).join('')}</div>`;
}

function eventToast(e) {
  const p = e.payload ?? {};
  const rows = [
    ['Модель', p.model],
    ['Фон', p.backdrop],
    ['Узор', p.symbol],
    ['Флор модели', p.floor_model],
    ['Флор коллекции', p.floor_collection],
  ].filter(([, v]) => v);

  return `
    <div class="toast" data-open="false" data-event="${e.id}">
      <div class="row row--between">
        <div class="row" style="gap:var(--s2)">
          <span>${EVENT_ICONS[e.kind] ?? '•'}</span>
          <strong style="font-size:14px">${esc(e.body || e.kind)}</strong>
        </div>
        ${e.price ? `<span class="num dim" style="font-size:13px">${esc(e.price.ton)}</span>` : ''}
      </div>
      <div class="toast__more"><div style="padding-top:var(--s2)">
        ${rows
          .map(
            ([k, v]) =>
              `<div class="row row--between" style="font-size:13px"><span class="dim">${k}</span><span>${esc(v)}</span></div>`
          )
          .join('')}
        ${
          e.gift_slug
            ? `<button class="btn btn--ghost" data-gift="${esc(e.gift_slug)}"
                 style="width:100%;margin-top:var(--s2);min-height:36px">Открыть подарок</button>`
            : ''
        }
      </div></div>
    </div>`;
}

function screenSettings() {
  const me = state.me;
  if (!me) return `<div class="stack">${skeletons(2, '80px')}</div>`;

  return `
    <div class="stack">
      <div class="card">
        <div class="row row--between">
          <div>
            <strong>${esc(me.username ? '@' + me.username : 'Аккаунт')}</strong>
            <div class="dim" style="font-size:12px">ID ${esc(me.tg_id)}</div>
          </div>
          ${me.is_owner ? '<span class="badge badge--legendary">владелец</span>' : ''}
        </div>
      </div>

      <div class="card">
        <div class="row row--between">
          <div>
            <strong>Режим симуляции</strong>
            <div class="dim" style="font-size:12px">
              ${me.dry_run ? 'Покупки не совершаются' : 'Тратятся реальные средства'}
            </div>
          </div>
          <button class="btn ${me.dry_run ? 'btn--primary' : 'btn--danger'}"
                  data-action="dry-run" style="min-height:36px;padding:0 var(--s3)">
            ${me.dry_run ? 'Боевой режим' : 'Симуляция'}
          </button>
        </div>
      </div>

      <div class="card">
        <strong>Telegram-аккаунт</strong>
        <div class="dim" style="font-size:12px">
          основной: ${me.sessions.main ? 'подключён' : 'нет'} ·
          для сообщений: ${me.sessions.writer ? 'подключён' : 'нет'}
        </div>
      </div>

      <div class="card">
        <strong>Gift Satellite</strong>
        <div class="dim" style="font-size:12px">
          ${
            me.gift_satellite
              ? 'Ключ сохранён — цены по всем маркетам.'
              : 'Ключа нет. Флор считается по Telegram и MRKT. Добавить — /gskey в боте.'
          }
        </div>
      </div>

      <div class="dim" style="text-align:center;font-size:12px;padding:var(--s4)">
        За покупкой доступа — ${esc(me.support)}
      </div>
    </div>`;
}

/* --- shell ---------------------------------------------------------------- */

const TABS = [
  ['catalog', '🥖', 'Каталог'],
  ['tools', '⚙️', 'Инструменты'],
  ['feed', '🔔', 'Лента'],
  ['settings', '👤', 'Профиль'],
];

const SCREENS = {
  catalog: screenCatalog,
  tools: screenTools,
  feed: screenFeed,
  settings: screenSettings,
};

function render() {
  // The licence gate comes first: without it an unlicensed user sees empty
  // screens and 403s rather than being told what they need.
  if (state.me && !state.me.licensed) {
    root.innerHTML = `
      <div class="app">
        <div class="empty">
          <div style="font-size:40px">🥖</div>
          <h1>Ciabatta Tools</h1>
          <p>Нужен ключ доступа. Отправь его боту в чат.</p>
          <p class="dim">За покупкой — ${esc(state.me.support)}</p>
        </div>
      </div>`;
    return;
  }

  root.innerHTML = `
    <div class="app fade-in">
      ${state.me?.dry_run ? '<div class="dry-banner">⚠️ Режим симуляции — покупки не совершаются</div>' : ''}
      ${state.error ? `<div class="card" style="color:var(--down)">${esc(state.error)}</div>` : ''}
      ${SCREENS[state.screen]()}
    </div>
    <nav class="tabs">
      ${TABS.map(
        ([id, icon, label]) => `
        <button class="tab" data-tab="${id}" ${state.screen === id ? 'aria-current="page"' : ''}>
          <span style="font-size:18px">${icon}</span>
          <span>${label}</span>
        </button>`
      ).join('')}
    </nav>`;
}

/* --- data ---------------------------------------------------------------- */

function describe(err) {
  if (!(err instanceof ApiError)) return 'Что-то пошло не так';
  if (err.needsSession) {
    return 'Telegram-аккаунт не подключён. Открой бота и нажми «Подключить аккаунт».';
  }
  if (err.needsLicence) return 'Нужен ключ доступа.';
  if (err.isAuth) return 'Открой приложение через бота — так Telegram подтверждает вход.';
  return err.message;
}

// Cancels a superseded catalogue request: a quick filter change can otherwise
// let the slower earlier response land last and show the wrong results.
let catalogAbort = null;

async function loadCatalog({ append = false } = {}) {
  catalogAbort?.abort();
  catalogAbort = new AbortController();

  if (!append) state.listings = null;
  render();

  try {
    const data = await api.catalog({
      collection: state.filters.collection,
      model: state.filters.model,
      backdrop: state.filters.backdrop,
      maxPriceTon: state.filters.maxPriceTon || undefined,
      cheapestFirst: state.cheapestFirst,
      cursor: append ? state.cursor : '',
      signal: catalogAbort.signal,
    });
    state.listings = append ? [...(state.listings ?? []), ...data.items] : data.items;
    state.cursor = data.cursor ?? '';
    state.error = null;
  } catch (err) {
    if (err.name === 'AbortError') return;
    // [] rather than null: null would keep the skeleton animating forever and
    // hide the error underneath it.
    state.listings = [];
    state.error = describe(err);
  }
  render();
}

async function loadScreen(screen) {
  if (screen === 'catalog') {
    if (state.listings === null) await loadCatalog();
    return;
  }

  const [key, fetch] =
    screen === 'feed'
      ? ['events', () => api.events().then((d) => d.events)]
      : screen === 'tools'
        ? ['ciabattas', () => api.ciabattas().then((d) => d.ciabattas)]
        : [null, null];

  if (!key) return;

  try {
    state[key] = await fetch();
    state.error = null;
  } catch (err) {
    state[key] = [];
    state.error = describe(err);
  }
  render();
}

/* --- events -------------------------------------------------------------- */

// One delegated listener rather than per-element handlers: innerHTML replaces
// the tree on every render, so bound handlers would be lost each time.
root.addEventListener('click', async (event) => {
  const target = event.target.closest(
    '[data-tab],[data-action],[data-tool],[data-event],[data-gift],[data-toggle]'
  );
  if (!target) return;

  const { tab, action, tool, event: eventId, gift, toggle } = target.dataset;

  if (tab) {
    haptic();
    state.screen = tab;
    state.error = null;
    render();
    loadScreen(tab);
    return;
  }

  if (action === 'sort') {
    state.cheapestFirst = !state.cheapestFirst;
    haptic();
    loadCatalog();
    return;
  }

  if (action === 'more') {
    loadCatalog({ append: true });
    return;
  }

  if (action === 'filters') {
    tg?.showAlert?.('Экран фильтров — следующий шаг сборки.');
    return;
  }

  if (action === 'dry-run') {
    const next = !state.me.dry_run;
    // Friction on the dangerous direction only: arming real spending is
    // confirmed, returning to simulation is not.
    if (!next) {
      const ok = confirm('Включить боевой режим? Будут тратиться реальные средства.');
      if (!ok) return;
    }
    try {
      await api.setDryRun(next);
      state.me.dry_run = next;
      notifyHaptic(next ? 'success' : 'warning');
    } catch (err) {
      state.error = describe(err);
    }
    render();
    return;
  }

  if (toggle) {
    const c = (state.ciabattas ?? []).find((x) => String(x.id) === toggle);
    if (!c) return;
    try {
      await api.patchCiabatta(c.id, { active: !c.active });
      c.active = !c.active;
      notifyHaptic('success');
    } catch (err) {
      state.error = describe(err);
    }
    render();
    return;
  }

  if (eventId) {
    // Toggled in place instead of through render(): a re-render would collapse
    // every other toast the user had already expanded.
    const node = target.closest('.toast');
    node.dataset.open = node.dataset.open === 'true' ? 'false' : 'true';
    haptic();
    return;
  }

  if (gift) {
    openTgLink(`https://t.me/nft/${gift}`);
    return;
  }

  if (tool) {
    tg?.showAlert?.('Настройка инструмента — следующий шаг сборки.');
  }
});

/* --- boot ---------------------------------------------------------------- */

async function boot() {
  initTelegram();
  render();

  try {
    state.me = await api.me();
  } catch (err) {
    state.error = describe(err);
    render();
    return;
  }

  render();
  if (state.me.licensed) loadScreen(state.screen);
}

boot();
