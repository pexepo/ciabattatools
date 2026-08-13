/* API client and Telegram bridge.
 *
 * A native ES module, loaded straight by the browser: this project has no
 * bundler, which is why nothing here imports from a package.
 *
 * Money never becomes a JavaScript number. The API sends every amount as
 * {nano, ton} -- an integer and a decimal string -- and both forms are kept as
 * they arrived. Parsing "0.1" into a float and echoing it back is how a bid ends
 * up a nanoton off, so the string is what travels back.
 */

// Telegram's injected bridge. Absent when the page is opened outside Telegram,
// so every use goes through optional chaining rather than assuming it exists.
export const tg = window.Telegram?.WebApp ?? null;

export class ApiError extends Error {
  constructor(status, payload) {
    // Prefer the server's own wording: it is written for the user, in Russian,
    // and a generic "request failed" would replace something better.
    const detail = payload?.detail;
    const message =
      (typeof detail === 'string' && detail) || detail?.message || `HTTP ${status}`;
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
    // Names the three cases the UI handles differently: no licence, no Telegram
    // session, everything else.
    this.code = detail?.code || detail?.error || null;
  }

  get needsLicence() {
    return this.status === 403 && this.code === 'no_licence';
  }

  get needsSession() {
    return this.status === 409 && this.code === 'no_session';
  }

  get isAuth() {
    return this.status === 401;
  }
}

function initData() {
  // Telegram signs this blob and the server verifies it, so it is the app's only
  // credential. Empty outside Telegram, which the server rejects with 401 --
  // the correct outcome rather than a confusing one.
  return tg?.initData ?? '';
}

async function request(path, { method = 'GET', body, query, signal } = {}) {
  const url = new URL(path, window.location.origin);

  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === '') continue;
    // Arrays repeat the key (?model=A&model=B), which is how the API reads
    // multi-select facets. A comma-joined string would arrive as one name.
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== undefined && item !== null && item !== '') {
          url.searchParams.append(key, item);
        }
      }
    } else {
      url.searchParams.set(key, value);
    }
  }

  const headers = { 'X-Init-Data': initData() };
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (cause) {
    // An abort is the caller's own doing -- a superseded search, a closed screen
    // -- and must not surface as a failure.
    if (cause.name === 'AbortError') throw cause;
    throw new ApiError(0, { detail: 'Нет соединения' });
  }

  if (response.status === 204) return null;

  // A proxy or a crashed worker can return HTML with a 200, so the body is
  // parsed defensively rather than trusted to be JSON.
  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: 'Некорректный ответ сервера' };
    }
  }

  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export const api = {
  me: () => request('/api/me'),

  collections: () => request('/api/collections'),

  catalog: ({
    collection,
    model,
    backdrop,
    minPriceTon,
    maxPriceTon,
    cheapestFirst = true,
    count = 30,
    cursor = '',
    signal,
  } = {}) =>
    request('/api/catalog', {
      signal,
      query: {
        collection,
        model,
        backdrop,
        min_price_ton: minPriceTon,
        max_price_ton: maxPriceTon,
        cheapest_first: String(cheapestFirst),
        count,
        cursor,
      },
    }),

  floor: ({ collection, model, backdrop, signal } = {}) =>
    request('/api/floor', { signal, query: { collection, model, backdrop } }),

  // Only the top order exists: MRKT publishes no order book, so this returns the
  // one figure needed to outbid, plus a `source` saying how it was obtained. A
  // caller about to spend on it must check `trustworthy` first.
  topOrder: ({ collection, model, signal } = {}) =>
    request('/api/orders/top', { signal, query: { collection, model } }),

  ciabattas: () => request('/api/ciabattas'),

  createCiabatta: (payload) =>
    request('/api/ciabattas', { method: 'POST', body: payload }),

  patchCiabatta: (id, payload) =>
    request(`/api/ciabattas/${id}`, { method: 'PATCH', body: payload }),

  deleteCiabatta: (id) => request(`/api/ciabattas/${id}`, { method: 'DELETE' }),

  events: ({ limit = 50, beforeId } = {}) =>
    request('/api/events', { query: { limit, before_id: beforeId } }),

  setDryRun: (enabled) =>
    request('/api/settings/dry-run', {
      method: 'POST',
      query: { enabled: String(enabled) },
    }),
};

/* --- money ---------------------------------------------------------------- */

/** Display an amount. Takes the API's {nano, ton} object, never a number. */
export function fmtTon(amount, { dash = '—' } = {}) {
  if (!amount) return dash;
  // The server already rendered this at the right precision; reformatting here
  // would be a second chance to get it wrong.
  return `${amount.ton} TON`;
}

/**
 * Adjust a price by a percentage, for the +/-5% buttons.
 *
 * Integer arithmetic in BigInt, rounding to a whole nanoton, so repeated taps
 * cannot accumulate a fractional remainder the market would reject.
 */
export function adjustNano(nano, percent) {
  const base = BigInt(nano);
  // BigInt has no fractions, so the percentage becomes a scaled integer ratio:
  // multiply first, divide last.
  const scaled = (base * BigInt(Math.round((100 + percent) * 100))) / 10000n;
  return scaled < 0n ? 0n : scaled;
}

/** nanotons -> a decimal string suitable for sending back as *_ton. */
export function nanoToTonString(nano) {
  const value = BigInt(nano);
  const whole = value / 1000000000n;
  const frac = (value % 1000000000n).toString().padStart(9, '0');
  // Trailing zeros trimmed, but at least two places kept so the field reads as
  // a price rather than a count.
  const trimmed = frac.replace(/0+$/, '').padEnd(2, '0');
  return `${whole}.${trimmed}`;
}

/** Signed percentage with an arrow, so hue is never the only cue. */
export function pct(value) {
  if (value === null || value === undefined) return { text: '—', dir: 0 };
  const rounded = Math.round(value * 10) / 10;
  if (rounded === 0) return { text: '0%', dir: 0 };
  const dir = rounded > 0 ? 1 : -1;
  return { text: `${dir > 0 ? '↑' : '↓'} ${Math.abs(rounded)}%`, dir };
}

/* --- Telegram ------------------------------------------------------------- */

export function haptic(style = 'light') {
  // Absent on desktop and older clients; failing silently is correct because
  // haptics are an enhancement, not a dependency.
  try {
    tg?.HapticFeedback?.impactOccurred(style);
  } catch {}
}

export function notifyHaptic(type = 'success') {
  try {
    tg?.HapticFeedback?.notificationOccurred(type);
  } catch {}
}

/** Open a t.me link through Telegram so it stays inside the client. */
export function openTgLink(url) {
  if (tg?.openTelegramLink) {
    tg.openTelegramLink(url);
  } else {
    window.open(url, '_blank', 'noopener');
  }
}

export function initTelegram() {
  if (!tg) return;
  tg.ready();
  tg.expand();
  // Without this the header stays Telegram's default blue while the body follows
  // the user's theme, which reads as a rendering bug.
  try {
    tg.setHeaderColor('secondary_bg_color');
  } catch {}
  // Stops a stray swipe-down closing the app mid-configuration. Newer clients
  // only, hence the optional call.
  try {
    tg.disableVerticalSwipes?.();
  } catch {}
}
