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
  adjustNano,
  api,
  ApiError,
  fmtTon,
  haptic,
  initTelegram,
  nanoToTonString,
  notifyHaptic,
  openTgLink,
  tg,
} from './api.js';

const state = {
  screen: 'tools',
  me: null,
  collections: null,
  events: null,
  ciabattas: null,
  // A full-screen editor layered over the current tab: the filters picker or a
  // tool-config form. null means none. Kept off `screen` because it overlays any
  // tab and must restore it on close, and because closing it must not refetch the
  // tab underneath.
  overlay: null,
  // Facet names ({models, backdrops, symbols}) keyed by collection-scope, so
  // reopening the same scope is instant. '' is the key for "all collections".
  facetCache: {},
  collectionSort: 'price',
  error: null,
  // Licence screen. Separate from `error` because it is shown inside that form,
  // not in the app-wide banner.
  licenceError: null,
  licenceBusy: false,
  writerStatus: null,
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

/** An ISO timestamp as a short local date, or a dash when absent. */
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('ru-RU', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
}

/** A short shower of crumbs from a point, for a successful ciabatta.
 *
 * The one place DOM is written outside render(): a transient overlay that
 * removes itself when the animation ends, so it never lingers in the tree a
 * render would otherwise have to reason about. Each fleck gets its own arc via
 * inline custom properties; reduced-motion hides the layer entirely (CSS).
 */
function crumbBurst(x, y) {
  const layer = document.createElement('div');
  layer.className = 'crumbs';

  const count = 22;
  let alive = count;
  for (let i = 0; i < count; i++) {
    const angle = Math.random() * Math.PI * 2;
    const dist = 60 + Math.random() * 140;
    const crumb = document.createElement('div');
    crumb.className = 'crumb';
    crumb.style.left = `${x}px`;
    crumb.style.top = `${y}px`;
    crumb.style.setProperty('--dx', `${Math.cos(angle) * dist}px`);
    // Bias downward so the crumbs fall, not just scatter.
    crumb.style.setProperty('--dy', `${Math.sin(angle) * dist + 80}px`);
    crumb.style.setProperty('--rot', `${(Math.random() - 0.5) * 720}deg`);
    crumb.style.setProperty('--dur', `${700 + Math.random() * 500}ms`);
    // Remove the layer once the last fleck has finished, not on a fixed timer,
    // so it matches whatever duration the flecks drew.
    crumb.addEventListener('animationend', () => {
      if (--alive === 0) layer.remove();
    });
    layer.appendChild(crumb);
  }

  document.body.appendChild(layer);
  // Safety net: if animationend never fires (reduced-motion hides the layer, so
  // no animation runs), still clean up.
  setTimeout(() => layer.remove(), 1500);
}

/* --- screens -------------------------------------------------------------- */

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
    ['automessage', '💬', 'Авто-сообщение', 'Писать владельцу от выбранного Telegram-аккаунта.'],
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
        <div style="min-width:0;flex:1">
          <strong>${esc(c.title || label)}</strong>
          <div class="dim" style="font-size:12px">
            ${esc(label)}${c.max_price ? ` · до ${esc(c.max_price.ton)} TON` : ''}${
              c.quantity ? ` · ${c.filled}/${c.quantity}` : ''
            }
          </div>
        </div>
        <div class="ciabatta-actions">
          <button class="icon-btn" data-action="edit-ciabatta" data-id="${c.id}"
                  aria-label="Редактировать">✎</button>
          <button class="icon-btn icon-btn--danger" data-action="delete-ciabatta" data-id="${c.id}"
                  aria-label="Удалить">×</button>
          <button class="btn ${c.active ? 'btn--danger' : 'btn--primary'}"
                  data-toggle="${c.id}" style="min-height:36px;padding:0 var(--s3)">
            ${c.active ? 'Стоп' : 'Пуск'}
          </button>
        </div>
      </div>
    </div>`;
}

const EVENT_ICONS = {
  gift_upgraded: '✨',
  gift_crafted: '⚗️',
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

      <div class="licence-card">
        <div class="row row--between" style="margin-bottom:var(--s2)">
          <strong>Лицензия</strong>
          <span class="badge badge--legendary">активна</span>
        </div>
        <div class="licence-card__row">
          <span class="licence-card__label">Активирована</span>
          <span class="licence-card__value">${esc(fmtDate(me.licensed_at))}</span>
        </div>
        <div class="licence-card__row">
          <span class="licence-card__label">Действует</span>
          <span class="licence-card__value">бессрочно</span>
        </div>
      </div>

      <div class="dim" style="text-align:center;font-size:12px;padding:var(--s4)">
        За покупкой доступа — ${esc(me.support)}
      </div>
    </div>`;
}

/* --- overlay: shared primitives ------------------------------------------ */

/** Whether a facet value is currently chosen.
 *
 * Tracker holds selections as an array (multi-select); sniping and ordering hold
 * a single string. One predicate serves both so the pickers share a code path.
 */
function isPicked(current, value) {
  return Array.isArray(current) ? current.includes(value) : current === value;
}

/** Map a per-mille rarity onto a band, mirroring the server's cutoffs.
 *
 * The facet API sends the raw per-mille figure but not the band name, so the
 * badge colour is derived here rather than round-tripping a second field.
 */
function rarityBandFrom(perMille) {
  if (perMille === null || perMille === undefined) return null;
  const v = Number(perMille);
  if (!(v > 0)) return null;
  if (v <= 5) return 'legendary';
  if (v <= 15) return 'epic';
  if (v <= 50) return 'rare';
  return 'uncommon';
}

/** Collection picker with market thumbnail, floor, sort and select-all. */
function facetSearch(group, value, placeholder) {
  return `<label class="facet-search">
    <span aria-hidden="true">⌕</span>
    <input class="input" type="search" data-search-field="${esc(group)}Query"
           placeholder="${esc(placeholder)}" value="${esc(value || '')}">
  </label>`;
}

function collectionChips(selected, query = '') {
  const needle = query.trim().toLocaleLowerCase('ru-RU');
  const rows = [...(state.collections ?? [])].filter((c) =>
    !needle || String(c.title || c.name).toLocaleLowerCase('ru-RU').includes(needle)
  ).sort((a, b) => {
    if (state.collectionSort === 'newest') {
      return String(b.created_at || '').localeCompare(String(a.created_at || ''));
    }
    return (a.floor?.nano ?? Number.MAX_SAFE_INTEGER) - (b.floor?.nano ?? Number.MAX_SAFE_INTEGER);
  });
  if (rows.length === 0) return '<div class="dim" style="font-size:13px">—</div>';
  const selectedList = Array.isArray(selected) ? selected : selected ? [selected] : [];
  const allRows = state.collections ?? [];
  const allOn = allRows.length > 0 && allRows.every((c) => selectedList.includes(c.name));
  return `
    ${facetSearch('collection', query, 'Найти коллекцию')}
    <div class="facet-toolbar">
      <div class="segmented">
        <button class="segmented__btn${state.collectionSort === 'price' ? ' is-on' : ''}" data-action="collection-sort" data-sort="price">По цене</button>
        <button class="segmented__btn${state.collectionSort === 'newest' ? ' is-on' : ''}" data-action="collection-sort" data-sort="newest">По новизне</button>
      </div>
      ${Array.isArray(selected) ? `<button class="btn btn--ghost select-all" data-action="select-all" data-select-group="collection">${allOn ? 'Снять все' : 'Выбрать все'}</button>` : ''}
    </div>
    <div class="collection-grid">${rows
    .map((c) => {
      const on = isPicked(selected, c.name);
      const floor = c.floor ? `<span class="chip__floor">${esc(c.floor.ton)}</span>` : '';
      const src = c.thumb ? CDN + c.thumb : null;
      return `<button class="collection-card${on ? ' collection-card--on' : ''}" data-chip="collection"
        data-value="${esc(c.name)}" aria-pressed="${on}">
          <span class="collection-card__thumb">${src ? `<img src="${esc(src)}" alt="" loading="lazy">` : '🥖'}</span>
          <span class="collection-card__body"><strong>${esc(c.title || c.name)}</strong><span>${floor}</span></span>
        </button>`;
    })
    .join('')}</div>`;
}

/** Gift-Satellite-style cards for a model/backdrop facet.
 *
 * Each option is a card carrying its own thumbnail, sampled floor and -- for a
 * model -- its rarity chance, so the picker shows what a person would recognise
 * rather than a bare name. Works for both multi- and single-select via isPicked.
 */
function facetCards(group, options, selected, query = '') {
  const needle = query.trim().toLocaleLowerCase('ru-RU');
  const shown = (options || []).filter((opt) =>
    !needle || String(opt.name).toLocaleLowerCase('ru-RU').includes(needle)
  );
  if (shown.length === 0) {
    return '<div class="dim" style="font-size:13px">—</div>';
  }
  return `<div class="facet-cards">${shown
    .map((opt) => {
      const on = isPicked(selected, opt.name);
      const src = opt.thumb ? CDN + opt.thumb : null;
      const band = rarityBandFrom(opt.rarity_per_mille);
      const rarity = band
        ? `<span class="badge badge--${band}">${(opt.rarity_per_mille / 10).toFixed(1)}%</span>`
        : '';
      const floor = opt.floor
        ? `<span class="fcard__floor">${esc(opt.floor.ton)}</span>`
        : '<span class="fcard__floor dim">—</span>';
      return `
        <button class="fcard${on ? ' fcard--on' : ''}" data-chip="${esc(group)}"
                data-value="${esc(opt.name)}" aria-pressed="${on}">
          <div class="fcard__thumb${group === 'backdrop' ? ' fcard__thumb--backdrop' : ''}" style="${backdropStyle(opt.backdrop_colors)}">
            ${src ? `<img src="${esc(src)}" alt="${esc(opt.name)}" loading="lazy" decoding="async">` : ''}
          </div>
          <div class="fcard__name">${esc(opt.name)}</div>
          <div class="fcard__meta">${floor}${rarity}</div>
        </button>`;
    })
    .join('')}</div>`;
}

function overlayHeader(title) {
  return `
    <div class="row row--between" style="margin-bottom:var(--s3)">
      <button class="btn btn--ghost" data-action="overlay-close"
              style="min-height:38px;padding:0 var(--s3)">‹ Назад</button>
      <h2>${esc(title)}</h2>
      <span style="width:64px"></span>
    </div>`;
}

/* --- overlay: tool config ------------------------------------------------- */

/** How close a backdrop is to a flat, single-hue ground.
 *
 * A monochrome backdrop is one whose centre and edge are near the same colour --
 * little gradient. Distance is the sum of channel differences between the two
 * RGB24 ints; a small distance means a solid-looking ground, which is what Gift
 * Satellite recommends pairing with a model. Returns null when colours are
 * unknown, so such backdrops never qualify as "recommended".
 */
function monochromeScore(colors) {
  if (!Array.isArray(colors) || colors.length !== 2) return null;
  const [a, b] = colors;
  const dr = Math.abs(((a >> 16) & 0xff) - ((b >> 16) & 0xff));
  const dg = Math.abs(((a >> 8) & 0xff) - ((b >> 8) & 0xff));
  const db = Math.abs((a & 0xff) - (b & 0xff));
  return dr + dg + db;
}

// Below this summed channel distance a backdrop reads as one flat colour.
const MONOCHROME_MAX = 48;

/** The monochrome backdrops among a facet list, tightest gradient first. */
function monochromeBackdrops(backdrops) {
  return (backdrops || [])
    .map((b) => ({ b, score: monochromeScore(b.backdrop_colors) }))
    .filter((x) => x.score !== null && x.score <= MONOCHROME_MAX)
    .sort((x, y) => x.score - y.score)
    .map((x) => x.b);
}

/** A live preview of the first selected model on the first selected backdrop. */
function previewTile(o) {
  const f = o.draft;
  const facets = o.facets;

  // Normalise selections to name arrays, single-select or multi.
  const modelNames = Array.isArray(f.model) ? f.model : f.model ? [f.model] : [];
  const backdropNames = Array.isArray(f.backdrop)
    ? f.backdrop
    : f.backdrop
      ? [f.backdrop]
      : [];

  if (modelNames.length === 0 && backdropNames.length === 0) {
    return `
      <div class="preview-tile">
        <div class="preview-tile__empty">Выбери модель или фон — здесь появится превью</div>
      </div>`;
  }

  // Resolve names to their facet objects for thumbs and colours.
  const modelObjs = modelNames
    .map((n) => (facets?.models ?? []).find((m) => m.name === n))
    .filter(Boolean);
  const firstBackdrop = backdropNames
    .map((n) => (facets?.backdrops ?? []).find((b) => b.name === n))
    .find(Boolean);

  const ground = firstBackdrop ? backdropStyle(firstBackdrop.backdrop_colors) : '';
  const model = modelObjs[0];
  const src = model?.thumb ? CDN + model.thumb : null;
  const extra = Math.max(0, modelNames.length - 1);

  return `
    <div class="preview-tile" style="${ground}">
      ${extra > 0 ? `<div class="preview-tile__more">+${extra}</div>` : ''}
      ${src ? `<img class="preview-tile__model" src="${esc(src)}" alt="${esc(model.name)}" decoding="async">` : ''}
      ${firstBackdrop ? `<div class="preview-tile__caption">${esc(firstBackdrop.name)}</div>` : ''}
    </div>`;
}

/** Collection/model/backdrop pickers for a tool.
 *
 * Every tool now uses the same image cards: tracker holds an array (multi-select),
 * sniping and ordering hold one value each, and isPicked() reads both. One mental
 * model across the app, and each option shows its own art, floor and rarity.
 */
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

function trackerSettings(f, kind) {
  const states = [
    ['upgraded', '✨ Улучшенные'],
    ['crafted', '⚗️ Полученные крафтом'],
    ['burned', '🔥 Сгоревшие'],
  ];
  return `
    <div class="card stack">
      <h3>События</h3>
      <div class="check-grid">
        ${states.map(([value, label]) => `<label class="filter-check filter-check--compact">
          <input type="checkbox" data-array-field="states" value="${value}" ${f.states.includes(value) ? 'checked' : ''}>
          <span><strong>${label}</strong></span>
        </label>`).join('')}
      </div>
    </div>
    <div class="card stack">
      <h3>Владелец</h3>
      <div class="field-grid">
        <label><span class="field-label">Подарков от</span><input class="input input--num" data-field="ownerGiftsMin" inputmode="numeric" placeholder="0" value="${esc(f.ownerGiftsMin)}"></label>
        <label><span class="field-label">Подарков до</span><input class="input input--num" data-field="ownerGiftsMax" inputmode="numeric" placeholder="без лимита" value="${esc(f.ownerGiftsMax)}"></label>
        <label><span class="field-label">Репутация от</span><input class="input input--num" data-field="reputationMin" inputmode="numeric" placeholder="0" value="${esc(f.reputationMin)}"></label>
        <label><span class="field-label">Репутация до</span><input class="input input--num" data-field="reputationMax" inputmode="numeric" placeholder="без лимита" value="${esc(f.reputationMax)}"></label>
      </div>
      <div class="dim" style="font-size:11px">Уведомления приходят только по владельцам, которым можно написать в Telegram.</div>
    </div>
    <div class="card stack">
      <label class="filter-check">
        <input type="checkbox" data-toggle-field="offerEnabled" ${f.offerEnabled ? 'checked' : ''}>
        <span><strong>Предлагать оффер</strong><small>Добавить действие «Поставить оффер» к уведомлению</small></span>
      </label>
      ${f.offerEnabled ? `<label><span class="field-label">Сумма оффера, TON</span><input class="input input--num" data-field="offerPriceTon" inputmode="decimal" placeholder="0.0" value="${esc(f.offerPriceTon)}"></label>` : ''}
    </div>
    ${kind === 'automessage' ? `<div class="card stack">
      <div><strong>Сообщение владельцу</strong><div class="dim" style="font-size:11px">У этой автоматизации свои отдельные фильтры</div></div>
        <label><span class="field-label">Отправлять от</span>
          <select class="input" data-field="writerAccount">
            <option value="main" ${f.writerAccount === 'main' ? 'selected' : ''}>Основного аккаунта</option>
            <option value="writer" ${f.writerAccount === 'writer' ? 'selected' : ''}>Другого аккаунта</option>
          </select>
        </label>
        ${f.writerAccount === 'writer' && !state.me?.sessions?.writer ? '<div class="facet-hint">Подключи второй аккаунт в чате с ботом кнопкой ниже.</div><button class="btn btn--ghost" data-action="connect-writer">Подключить другой аккаунт</button>' : ''}
        ${state.writerStatus ? `<div class="account-status">${esc(state.writerStatus)}</div>` : ''}
        <label><span class="field-label">Текст сообщения</span><textarea class="input textarea" data-field="messageTemplate" placeholder="Привет! Интересует {название_подарка}: {ссылка_на_подарок}">${esc(f.messageTemplate)}</textarea></label>
        <div class="dim" style="font-size:11px">Переменные: {название_подарка}, {ссылка_на_подарок}</div>
    </div>` : ''}`;
}

function mrktDisclaimer() {
  return `<div class="mrkt-disclaimer">
    <span aria-hidden="true">⚠️</span>
    <div><strong>Проверь баланс на MRKT</strong><small>Для покупки, оффера или ордера на MRKT должно хватать TON на сумму операции и комиссию.</small></div>
  </div>`;
}

/** The price box with a floor reference and flanking +/-5% buttons.
 *
 * The floor is shown live for whichever facet is selected, because a max price
 * only means something next to the number it is competing with. The buttons
 * nudge the field itself so a person can land near the floor without typing.
 */
function priceStepper(o) {
  const f = o.draft;
  const floor = o.floor;
  return `
    <div class="card stack">
      <div class="row row--between">
        <h3>Макс. цена, TON</h3>
        ${
          o.floorBusy
            ? '<span class="dim" style="font-size:12px">флор…</span>'
            : floor
              ? `<span class="dim" style="font-size:12px">флор ${esc(floor.ton)}</span>`
              : ''
        }
      </div>
      <div class="stepper">
        <button class="btn btn--ghost" data-step="-5" aria-label="минус 5 процентов">−5%</button>
        <input class="input input--num" data-field="maxPriceTon" inputmode="decimal"
               placeholder="${floor ? esc(floor.ton) : '0.0'}" value="${esc(f.maxPriceTon)}">
        <button class="btn btn--ghost" data-step="5" aria-label="плюс 5 процентов">+5%</button>
      </div>
      ${
        floor && !o.floorBusy
          ? '<button class="btn btn--ghost" data-action="use-floor" style="min-height:34px">Взять флор</button>'
          : ''
      }
    </div>`;
}

/** The one order that must be beaten, kept separate from editable settings. */
function topOrderPlaque(o) {
  if (!o.draft.collection) {
    return `
      <div class="order-plaque order-plaque--empty">
        <div class="order-plaque__eyebrow">Максимальный ордер</div>
        <div class="dim">Выбери коллекцию, чтобы увидеть текущий максимум.</div>
      </div>`;
  }
  if (o.topOrderBusy) {
    return `
      <div class="order-plaque order-plaque--empty">
        <div class="order-plaque__eyebrow">Максимальный ордер</div>
        <div class="dim">Проверяю книгу ордеров…</div>
      </div>`;
  }

  const order = o.topOrder;
  if (!order) {
    return `
      <div class="order-plaque order-plaque--empty">
        <div class="order-plaque__eyebrow">Максимальный ордер</div>
        <strong>Активных ордеров нет</strong>
        <div class="dim">Твой ордер станет первым в очереди.</div>
      </div>`;
  }

  const scope = order.model || 'Вся коллекция';
  const remaining = Number.isInteger(order.remaining)
    ? `${order.remaining} шт. осталось`
    : 'Количество не указано';
  return `
    <div class="order-plaque">
      <div class="order-plaque__eyebrow">Максимальный ордер</div>
      <div class="order-plaque__row">
        <strong class="order-plaque__price">${esc(fmtTon(order.price_max || order.price))}</strong>
        <span class="badge">${esc(scope)}</span>
      </div>
      <div class="order-plaque__meta">
        <span>${esc(remaining)}</span>
        ${order.trustworthy ? '<span>Данные MRKT</span>' : '<span>Цена только для справки</span>'}
      </div>
    </div>`;
}

function orderingFloors(o) {
  const floors = o.floorSummary || {};
  const value = (key, available) => {
    if (!available) return '<span class="dim">не выбран</span>';
    if (o.floorSummaryBusy) return '<span class="dim">загрузка…</span>';
    return floors[key] ? `<strong>${esc(fmtTon(floors[key]))}</strong>` : '<span class="dim">нет данных</span>';
  };
  return `<div class="floor-strip">
    <div><span>Флор коллекции</span>${value('collection', Boolean(o.draft.collection))}</div>
    <div><span>Флор модели</span>${value('model', Boolean(o.draft.model))}</div>
    <div><span>Флор фона</span>${value('backdrop', Boolean(o.draft.backdrop))}</div>
  </div>`;
}

function screenToolConfig() {
  const o = state.overlay;
  const f = o.draft;
  const k = o.kind;
  const titles = {
    sniping: '⚡ Авто-снайпинг',
    ordering: '🎯 Авто-ордеринг',
    tracker: '📡 Трекинг подарков',
    automessage: '💬 Авто-сообщение',
  };
  const error = state.error
    ? `<div class="card" style="color:var(--down)">${esc(state.error)}</div>`
    : '';

  // Sniping: collection + model/backdrop + max_price + quantity + auto_buy/auto_offer.
  if (k === 'sniping') {
    return `
      <div class="app">
        ${overlayHeader(titles[k])}
        <div class="stack">
          ${error}
          ${mrktDisclaimer()}
          <div class="card">
            <label style="font-weight:600">Название</label>
            <input class="input" data-field="title" placeholder="Моя Чиабатта" value="${esc(f.title)}">
          </div>
          ${previewTile(o)}
          ${toolFacetSection(o)}
          ${priceStepper(o)}
          <div class="card stack">
            <label style="font-weight:600">Количество</label>
            <input class="input input--num" data-field="quantity" inputmode="numeric"
                   placeholder="1" value="${esc(f.quantity)}">
          </div>
          <div class="card">
            <label class="row row--between" style="cursor:pointer">
              <span style="font-weight:600">Автобай</span>
              <input type="checkbox" data-toggle-field="autoBuy" ${f.autoBuy ? 'checked' : ''}>
            </label>
          </div>
          <div class="card stack">
            <label class="row row--between" style="cursor:pointer">
              <span style="font-weight:600">Автооффер</span>
              <input type="checkbox" data-toggle-field="autoOffer" ${f.autoOffer ? 'checked' : ''}>
            </label>
            ${
              f.autoOffer
                ? `<label class="dim" style="font-size:12px">% ниже замеченной цены</label>
                   <input class="input input--num" data-field="offerPct" inputmode="numeric"
                          placeholder="5" value="${esc(f.offerPct)}">`
                : ''
            }
          </div>
          <button class="btn btn--primary" data-action="save-tool">Сохранить</button>
        </div>
      </div>`;
  }

  // Ordering: collection + model + quantity + stop_pct + outbid_step.
  if (k === 'ordering') {
    return `
      <div class="app">
        ${overlayHeader(titles[k])}
        <div class="stack">
          ${error}
          ${mrktDisclaimer()}
          <div class="card">
            <label style="font-weight:600">Название</label>
            <input class="input" data-field="title" placeholder="Моя Чиабатта" value="${esc(f.title)}">
          </div>
          ${previewTile(o)}
          ${toolFacetSection(o)}
          <div class="card stack">
            <label style="font-weight:600">Количество</label>
            <input class="input input--num" data-field="quantity" inputmode="numeric"
                   placeholder="1" value="${esc(f.quantity)}">
          </div>
          <div class="card stack">
            <label style="font-weight:600">Стоп при % от флора</label>
            <input class="input input--num" data-field="stopPctOfFloor" inputmode="numeric"
                   placeholder="95" value="${esc(f.stopPctOfFloor)}">
            <div class="dim" style="font-size:11px">Если флор упадёт ниже этого процента, заявка остановится.</div>
          </div>
          <div class="card stack">
            <label style="font-weight:600">Шаг перебивания, TON</label>
            <input class="input input--num" data-field="outbidStepTon" inputmode="decimal"
                   placeholder="0.01" value="${esc(f.outbidStepTon)}">
          </div>
          ${orderingFloors(o)}
          ${topOrderPlaque(o)}
          <button class="btn btn--primary" data-action="save-tool">Сохранить</button>
        </div>
      </div>`;
  }

  if (k === 'tracker' || k === 'automessage') {
    return `
      <div class="app">
        ${overlayHeader(titles[k])}
        <div class="stack">
          ${error}
          ${mrktDisclaimer()}
          <div class="card">
            <label style="font-weight:600">Название</label>
            <input class="input" data-field="title" placeholder="Моя Чиабатта" value="${esc(f.title)}">
          </div>
          ${previewTile(o)}
          ${toolFacetSection(o)}
          <div class="card stack">
            <label style="font-weight:600">Цена, TON</label>
            <div class="row">
              <input class="input input--num" data-field="minPriceTon" inputmode="decimal"
                     placeholder="от" value="${esc(f.minPriceTon)}">
              <input class="input input--num" data-field="maxPriceTon" inputmode="decimal"
                     placeholder="до" value="${esc(f.maxPriceTon)}">
            </div>
          </div>
          ${trackerSettings(f, k)}
          <button class="btn btn--primary" data-action="save-tool">Сохранить</button>
        </div>
      </div>`;
  }

  return '<div class="app"><div class="empty">Неизвестный инструмент</div></div>';
}

/* --- shell ---------------------------------------------------------------- */

const TABS = [
  ['tools', '⚙️', 'Инструменты'],
  ['feed', '🔔', 'Лента'],
  ['settings', '👤', 'Профиль'],
];

const SCREENS = {
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
        <div class="stack" style="padding-top:var(--s6);text-align:center">
          <div style="font-size:40px">🥖</div>
          <h1>Ciabatta Tools</h1>
          <p class="dim" style="margin:0">
            Инструменты для заработка на NFT-подарках.<br>Доступ по ключу.
          </p>

          <div class="card stack" style="text-align:left;margin-top:var(--s3)">
            <label for="licence" style="font-weight:600">Ключ доступа</label>
            <input id="licence" class="input" placeholder="CIAB-XXXX-XXXX-XXXX"
                   autocomplete="off" autocapitalize="characters"
                   spellcheck="false" inputmode="text">
            ${
              state.licenceError
                ? `<div style="color:var(--down);font-size:13px">${esc(state.licenceError)}</div>`
                : ''
            }
            <button class="btn btn--primary" data-action="claim"
                    ${state.licenceBusy ? 'disabled' : ''}>
              ${state.licenceBusy ? 'Проверяю…' : 'Активировать'}
            </button>
          </div>

          <p class="dim" style="font-size:13px">
            Ключ можно отправить и сообщением боту.<br>
            За покупкой — ${esc(state.me.support)}
          </p>
        </div>
      </div>`;
    return;
  }

  // Overlay layered over the current screen, filling the viewport. Tab bar hidden
  // while it is open so the Back button in the overlay header is the only exit.
  if (state.overlay) {
    root.innerHTML = screenToolConfig();
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

/** Read all [data-field] and [data-toggle-field] values from the overlay and merge
 * them into state.overlay.draft.
 *
 * Text inputs are not synced on every keystroke because that would lose focus mid-
 * word when render() replaces the tree. Instead they are captured here, just before
 * any action that triggers a re-render (chip toggle, select change, stepper, apply).
 * The licence input does the same: it is read on submit, not mirrored to state.
 */
function syncDraft() {
  if (!state.overlay) return;
  const draft = state.overlay.draft;

  // Text inputs: title, quantity, prices, stop_pct, outbid_step, offer_pct.
  document.querySelectorAll('[data-field]').forEach((el) => {
    const key = el.dataset.field;
    draft[key] = el.value.trim();
  });

  // Checkboxes: autoBuy, autoOffer.
  document.querySelectorAll('[data-toggle-field]').forEach((el) => {
    const key = el.dataset.toggleField;
    draft[key] = el.checked;
  });

  // Checkbox groups: tracker event states. Grouping here keeps unchecked values
  // out of the draft instead of leaving stale entries after a re-render.
  const groups = new Map();
  document.querySelectorAll('[data-array-field]').forEach((el) => {
    const key = el.dataset.arrayField;
    if (!groups.has(key)) groups.set(key, []);
    if (el.checked) groups.get(key).push(el.value);
  });
  for (const [key, values] of groups) draft[key] = values;
}

function isMultiKind(kind) {
  return kind === 'tracker' || kind === 'automessage';
}

/** Generate a cache key for facets scoped to a collection list. */
function facetCacheKey(collections) {
  if (!collections || collections.length === 0) return '';
  return [...collections].sort().join(',');
}

/** Load facets for the currently selected collections in the overlay.
 *
 * Cached per scope, so reopening the same collection set is instant. The draft's
 * collection array is the scope for multi-select tools; a single-choice tool's
 * draft.collection is wrapped in an array.
 */
async function loadFacets() {
  if (!state.overlay) return;
  const o = state.overlay;
  const scope = isMultiKind(o.kind)
    ? o.draft.collection
    : o.draft.collection
      ? [o.draft.collection]
      : [];
  if (scope.length !== 1) {
    o.facets = { models: [], backdrops: [], symbols: [] };
    render();
    return;
  }
  const key = facetCacheKey(scope);

  // Already cached: no fetch.
  if (state.facetCache[key]) {
    o.facets = state.facetCache[key];
    render();
    return;
  }

  o.facets = null;
  render();

  try {
    const data = await api.facets({ collection: scope.length ? scope : undefined });
    // A fast second collection tap can start a newer request. Keep the old
    // response cached, but only paint it when its scope is still current.
    if (state.overlay === o) {
      state.facetCache[key] = data;
      const currentScope = isMultiKind(o.kind)
        ? o.draft.collection
        : o.draft.collection
          ? [o.draft.collection]
          : [];
      if (facetCacheKey(currentScope) === key) o.facets = data;
    }
  } catch (err) {
    if (state.overlay === o) {
      const currentScope = isMultiKind(o.kind)
        ? o.draft.collection
        : o.draft.collection
          ? [o.draft.collection]
          : [];
      if (facetCacheKey(currentScope) === key) {
        o.facets = { models: [], backdrops: [], symbols: [] };
      }
    }
  }
  render();
}

/** Load the floor for the currently selected facet in a tool-config overlay.
 *
 * Only sniping and ordering call this; tracker has no floor reference. The floor
 * is refetched whenever collection or model changes so the +/-5% buttons nudge
 * against the right baseline.
 */
async function loadFloor() {
  if (!state.overlay || state.overlay.mode !== 'tool') return;
  const o = state.overlay;
  const f = o.draft;
  const requestKey = [f.collection, f.model, f.backdrop].join('\n');
  const stillCurrent = () =>
    state.overlay === o &&
    [o.draft.collection, o.draft.model, o.draft.backdrop].join('\n') === requestKey;

  // No collection → no floor.
  if (!f.collection) {
    o.floor = null;
    o.floorBusy = false;
    render();
    return;
  }

  o.floorBusy = true;
  render();

  try {
    const data = await api.floor({
      collection: f.collection,
      model: f.model || undefined,
      backdrop: f.backdrop || undefined,
    });
    if (stillCurrent()) o.floor = data.floor;
  } catch (err) {
    if (stillCurrent()) o.floor = null;
  }

  if (stillCurrent()) o.floorBusy = false;
  render();
}

async function loadOrderingFloors() {
  if (!state.overlay || state.overlay.mode !== 'tool' || state.overlay.kind !== 'ordering') return;
  const o = state.overlay;
  const f = o.draft;
  const requestKey = [f.collection, f.model, f.backdrop].join('\n');
  const stillCurrent = () =>
    state.overlay === o &&
    [o.draft.collection, o.draft.model, o.draft.backdrop].join('\n') === requestKey;

  if (!f.collection) {
    o.floorSummary = {};
    o.floorSummaryBusy = false;
    render();
    return;
  }

  o.floorSummaryBusy = true;
  render();
  try {
    const summary = await api.floorSummary({
      collection: f.collection,
      model: f.model || undefined,
      backdrop: f.backdrop || undefined,
    });
    if (stillCurrent()) o.floorSummary = summary;
  } catch {
    if (stillCurrent()) o.floorSummary = {};
  }
  if (stillCurrent()) {
    o.floorSummaryBusy = false;
    render();
  }
}

/** Refresh the highest competing MRKT order for the selected target. */
async function loadTopOrder() {
  if (!state.overlay || state.overlay.mode !== 'tool' || state.overlay.kind !== 'ordering') return;
  const o = state.overlay;
  const f = o.draft;
  const requestKey = [f.collection, f.model, f.backdrop].join('\n');
  const stillCurrent = () =>
    state.overlay === o &&
    [o.draft.collection, o.draft.model, o.draft.backdrop].join('\n') === requestKey;

  if (!f.collection) {
    o.topOrder = null;
    o.topOrderBusy = false;
    render();
    return;
  }

  o.topOrderBusy = true;
  render();
  try {
    const data = await api.topOrder({
      collection: f.collection,
      model: f.model || undefined,
      backdrop: f.backdrop || undefined,
    });
    if (stillCurrent()) o.topOrder = data.order;
  } catch (err) {
    if (stillCurrent()) o.topOrder = null;
  }
  if (stillCurrent()) o.topOrderBusy = false;
  render();
}

function asArray(value) {
  if (Array.isArray(value)) return [...value];
  return value ? [value] : [];
}

function openTool(kind, ciabatta = null) {
  if (state.collections === null) {
    loadCollections().then(() => {
      if (state.collections) openTool(kind, ciabatta);
    });
    return;
  }

  const filters = ciabatta?.filters ?? {};
  const multi = isMultiKind(kind);
  const defaults = {
    title: ciabatta?.title ?? '',
    collection: multi ? asArray(filters.collection) : filters.collection ?? '',
    collectionAll: multi && Boolean(filters.collection_all),
    model: multi ? asArray(filters.model) : filters.model ?? '',
    backdrop: multi ? asArray(filters.backdrop) : filters.backdrop ?? '',
    monochromeOnly: false,
    minPriceTon: ciabatta?.min_price?.ton ?? '',
    maxPriceTon: ciabatta?.max_price?.ton ?? '',
    quantity: ciabatta?.quantity ?? '',
    stopPctOfFloor: ciabatta?.stop_pct_of_floor ?? '',
    outbidStepTon: ciabatta?.outbid_step?.ton ?? '',
    autoBuy: ciabatta?.auto_buy ?? false,
    autoOffer: ciabatta?.auto_offer ?? false,
    offerPct: ciabatta?.offer_pct ?? '',
    states: asArray(filters.states).length ? asArray(filters.states) : ['upgraded', 'crafted', 'burned'],
    ownerGiftsMin: filters.owner_gifts_min ?? '',
    ownerGiftsMax: filters.owner_gifts_max ?? '',
    reputationMin: filters.reputation_min ?? '',
    reputationMax: filters.reputation_max ?? '',
    collectionQuery: '',
    modelQuery: '',
    backdropQuery: '',
    offerEnabled: filters.offer_enabled ?? false,
    offerPriceTon: filters.offer_price_ton ?? '',
    autoMessage: kind === 'automessage' || Boolean(filters.auto_message),
    writerAccount: filters.writer_account ?? 'main',
    messageTemplate: filters.message_template ?? '',
  };
  const selectedCollections = Array.isArray(defaults.collection)
    ? defaults.collection.length
    : defaults.collection ? 1 : 0;
  if (selectedCollections !== 1) {
    defaults.model = multi ? [] : '';
    defaults.backdrop = multi ? [] : '';
  }
  const selectedModels = Array.isArray(defaults.model)
    ? defaults.model.length
    : defaults.model ? 1 : 0;
  if (selectedModels === 0) defaults.backdrop = multi ? [] : '';

  state.overlay = {
    mode: 'tool',
    kind,
    ciabattaId: ciabatta?.id ?? null,
    draft: defaults,
    sectionOpen: {},
    facets: null,
    floor: null,
    floorBusy: false,
    topOrder: null,
    topOrderBusy: false,
    floorSummary: {},
    floorSummaryBusy: false,
  };
  render();
  if ((multi && defaults.collection.length) || (!multi && defaults.collection)) {
    loadFacets();
    if (!multi) {
      if (kind === 'ordering') {
        loadOrderingFloors();
        loadTopOrder();
      } else loadFloor();
    }
  }
}

async function loadCollections() {
  if (state.collections !== null) return;
  try {
    const data = await api.collections();
    state.collections = data.collections ?? [];
  } catch (err) {
    state.collections = [];
    state.error = describe(err);
  }
  render();
}

async function loadScreen(screen) {
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
    '[data-tab],[data-action],[data-tool],[data-event],[data-gift],[data-toggle],[data-chip],[data-step]'
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

  if (action === 'overlay-close') {
    haptic();
    state.overlay = null;
    render();
    return;
  }

  if (action === 'use-floor') {
    if (state.overlay?.floor) {
      state.overlay.draft.maxPriceTon = state.overlay.floor.ton;
      haptic();
      render();
    }
    return;
  }

  if (action === 'collection-sort') {
    syncDraft();
    state.collectionSort = target.dataset.sort === 'newest' ? 'newest' : 'price';
    haptic();
    render();
    return;
  }

  if (action === 'select-all' && state.overlay) {
    syncDraft();
    const group = target.dataset.selectGroup;
    let names = [];
    if (group === 'collection') {
      names = (state.collections ?? []).map((c) => c.name);
    } else if (group === 'model') {
      names = (state.overlay.facets?.models ?? []).map((item) => item.name);
    } else if (group === 'backdrop') {
      const choices = state.overlay.draft.monochromeOnly
        ? monochromeBackdrops(state.overlay.facets?.backdrops)
        : state.overlay.facets?.backdrops ?? [];
      names = choices.map((item) => item.name);
    }

    const current = state.overlay.draft[group];
    if (Array.isArray(current)) {
      const selectingAll = !(names.length && names.every((name) => current.includes(name)));
      state.overlay.draft[group] = selectingAll ? names : [];
      if (group === 'collection') {
        state.overlay.draft.collectionAll = selectingAll;
        if (state.overlay.draft.collection.length !== 1) {
          state.overlay.draft.model = [];
          state.overlay.draft.backdrop = [];
        }
      } else if (group === 'model' && state.overlay.draft.model.length === 0) {
        state.overlay.draft.backdrop = [];
      }
      haptic();
      render();
      if (group === 'collection') loadFacets();
    }
    return;
  }

  if (action === 'connect-writer') {
    const username = String(state.me?.bot_username ?? '').replace(/^@/, '');
    if (username) {
      state.writerStatus = 'Открыл чат с ботом. Заверши вход там, затем вернись в приложение.';
      openTgLink(`https://t.me/${username}?start=writer`);
    }
    else state.error = 'Имя бота не настроено';
    render();
    return;
  }

  if (action === 'edit-ciabatta') {
    const item = (state.ciabattas ?? []).find((c) => String(c.id) === target.dataset.id);
    if (item) {
      state.error = null;
      haptic();
      openTool(item.kind, item);
    }
    return;
  }

  if (action === 'delete-ciabatta') {
    const item = (state.ciabattas ?? []).find((c) => String(c.id) === target.dataset.id);
    if (!item || !confirm(`Удалить «${item.title || TOOL_LABELS[item.kind] || 'Чиабатта'}»?`)) return;
    try {
      await api.deleteCiabatta(item.id);
      state.ciabattas = state.ciabattas.filter((c) => c.id !== item.id);
      notifyHaptic('success');
      state.error = null;
    } catch (err) {
      state.error = describe(err);
    }
    render();
    return;
  }

  if (action === 'save-tool') {
    syncDraft();
    const o = state.overlay;
    const f = o.draft;

    // Capture where the button sits now: render() below replaces the tree, so
    // the crumbs must burst from a point read before it, not the stale node.
    const rect = target.getBoundingClientRect();
    const burstAt = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };

    // Build the payload. Tracker and auto-message filters are multi-select;
    // spend tools keep one exact market target.
    const filters = {};
    if (isMultiKind(o.kind)) {
      if (f.collection.length) filters.collection = f.collection;
      if (
        f.collectionAll ||
        (f.collection.length > 0 && f.collection.length === (state.collections ?? []).length)
      ) filters.collection_all = true;
      if (f.model.length) filters.model = f.model;
      if (f.backdrop.length) filters.backdrop = f.backdrop;
      if (f.states.length) filters.states = f.states;
      if (f.ownerGiftsMin) filters.owner_gifts_min = parseInt(f.ownerGiftsMin, 10);
      if (f.ownerGiftsMax) filters.owner_gifts_max = parseInt(f.ownerGiftsMax, 10);
      if (f.reputationMin) filters.reputation_min = parseInt(f.reputationMin, 10);
      if (f.reputationMax) filters.reputation_max = parseInt(f.reputationMax, 10);
      filters.offer_enabled = Boolean(f.offerEnabled);
      if (f.offerPriceTon) filters.offer_price_ton = f.offerPriceTon;
      filters.auto_message = o.kind === 'automessage';
      if (filters.auto_message) {
        filters.writer_account = f.writerAccount || 'main';
        filters.message_template = f.messageTemplate || '';
      }
    } else {
      if (f.collection) filters.collection = f.collection;
      if (f.model) filters.model = f.model;
      if (f.backdrop) filters.backdrop = f.backdrop;
    }

    const payload = {
      kind: o.kind,
      title: f.title || '',
      filters,
    };

    payload.max_price_ton = f.maxPriceTon || null;
    payload.min_price_ton = isMultiKind(o.kind) ? f.minPriceTon || null : null;
    payload.quantity = f.quantity ? parseInt(f.quantity, 10) : null;
    payload.stop_pct_of_floor = f.stopPctOfFloor ? parseInt(f.stopPctOfFloor, 10) : null;
    payload.outbid_step_ton = f.outbidStepTon || null;
    payload.offer_pct = f.offerPct ? parseInt(f.offerPct, 10) : null;
    payload.auto_buy = f.autoBuy || false;
    payload.auto_offer = f.autoOffer || false;

    try {
      if (o.ciabattaId) {
        const patchPayload = { ...payload };
        delete patchPayload.kind;
        await api.patchCiabatta(o.ciabattaId, patchPayload);
      } else {
        await api.createCiabatta(payload);
      }
      notifyHaptic('success');
      crumbBurst(burstAt.x, burstAt.y);
      state.error = null;
      state.overlay = null;
      state.screen = 'tools';
      await loadScreen('tools');
    } catch (err) {
      state.error = describe(err);
      render();
    }
    return;
  }

  // Facet pick in overlay. Tracker holds arrays (toggle in/out); sniping and
  // ordering hold one value (tap to set, tap again to clear).
  const chip = target.dataset.chip;
  if (chip && state.overlay) {
    syncDraft();
    const draft = state.overlay.draft;
    const value = target.dataset.value;
    const cur = draft[chip];

    if (Array.isArray(cur)) {
      const idx = cur.indexOf(value);
      if (idx >= 0) cur.splice(idx, 1);
      else cur.push(value);
      if (chip === 'collection') {
        draft.collectionAll =
          cur.length > 0 && cur.length === (state.collections ?? []).length;
        if (cur.length !== 1) {
          draft.model = [];
          draft.backdrop = [];
        }
      } else if (chip === 'model' && cur.length === 0) {
        draft.backdrop = [];
      }
    } else {
      // Single-select: toggle off if already chosen, else replace.
      draft[chip] = cur === value ? '' : value;
      if (chip === 'collection') {
        draft.model = '';
        draft.backdrop = '';
      } else if (chip === 'model' && !draft.model) {
        draft.backdrop = '';
      }
    }
    haptic();
    render();

    // Facets are scoped to collection, so changing the collection reloads them;
    // the floor reference follows collection/model/backdrop for spend tools.
    if (chip === 'collection') {
      loadFacets();
      if (!isMultiKind(state.overlay.kind)) {
        if (state.overlay.kind === 'ordering') {
          loadOrderingFloors();
          loadTopOrder();
        } else loadFloor();
      }
    } else if (!isMultiKind(state.overlay.kind)) {
      if (state.overlay.kind === 'ordering') {
        loadOrderingFloors();
        loadTopOrder();
      } else loadFloor();
    }
    return;
  }

  // Price stepper: adjust maxPriceTon by ±5%.
  const step = target.dataset.step;
  if (step && state.overlay) {
    syncDraft();
    const current = state.overlay.draft.maxPriceTon || state.overlay.floor?.ton || '0';
    // tonToNano: parse the decimal string into nanotons. Not exported from api.js,
    // so defined inline here.
    const tonToNano = (s) => {
      const [whole = '0', frac = ''] = String(s).split('.');
      const nano = BigInt(whole) * 1000000000n + BigInt(frac.padEnd(9, '0').slice(0, 9));
      return nano;
    };
    const nano = tonToNano(current);
    const adjusted = adjustNano(nano, parseInt(step, 10));
    state.overlay.draft.maxPriceTon = nanoToTonString(adjusted);
    haptic();
    render();
    return;
  }

  if (tool) {
    haptic();
    openTool(tool);
    return;
  }

  if (action === 'claim') {
    const input = document.getElementById('licence');
    const key = (input?.value || '').trim();
    if (!key) {
      // Focus rather than an error message: the field is empty because nothing
      // was typed yet, which is not a mistake worth naming.
      input?.focus();
      return;
    }

    // Read before the re-render: render() replaces the tree and the input with
    // it, so the value has to be captured first.
    state.licenceBusy = true;
    state.licenceError = null;
    render();

    try {
      const result = await api.claimLicence(key);
      if (result.ok) {
        notifyHaptic('success');
        // Re-fetched rather than patched locally: /api/me also reports session
        // and dry-run state, and the app needs all of it to render the tabs.
        state.me = await api.me();
        state.licenceBusy = false;
        render();
        loadScreen(state.screen);
        return;
      }
      notifyHaptic('error');
      state.licenceError = result.message || 'Ключ не подошёл';
    } catch (err) {
      state.licenceError = describe(err);
    }
    state.licenceBusy = false;
    render();
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

  if (gift !== undefined) {
    // A listing with no number has no gift page, so its slug is empty. Opening
    // t.me/nft/ with nothing after it lands on an error page; the tap is ignored
    // instead. Checked against undefined rather than truthiness so an empty slug
    // still stops here rather than falling through to the tool branch.
    if (gift) openTgLink(`https://t.me/nft/${gift}`);
    haptic();
    return;
  }
});

root.addEventListener('change', (event) => {
  if (!state.overlay) return;
  const rerenders = event.target.matches(
    '[data-toggle-field],[data-field="writerAccount"]'
  );
  syncDraft();
  if (rerenders) render();
});

root.addEventListener('input', (event) => {
  const field = event.target.dataset.searchField;
  if (!state.overlay || !field) return;
  const value = event.target.value;
  state.overlay.draft[field] = value;
  const group = field.replace(/Query$/, '');
  const needle = value.trim().toLocaleLowerCase('ru-RU');
  root.querySelectorAll(`[data-chip="${group}"]`).forEach((card) => {
    card.hidden = Boolean(needle) && !String(card.dataset.value || '')
      .toLocaleLowerCase('ru-RU')
      .includes(needle);
  });
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

  // A second-account login finishes in bot chat. Refreshing when the user comes
  // back makes success visible without forcing them to close the mini-app.
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible' || !state.me?.licensed) return;
    try {
      const next = await api.me();
      const connectedNow = !state.me.sessions.writer && next.sessions.writer;
      state.me = next;
      if (connectedNow) state.writerStatus = 'Второй аккаунт подключён.';
      render();
    } catch {}
  });
}

boot();
