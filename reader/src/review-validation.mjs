const STAMP_KEYS = ['revision', 'status', 'reviewed_at', 'reason'];
const DETAIL_KEYS = [...STAMP_KEYS, 'activity'];

// Match Python's Unicode-code-point limits without allocating an array of characters.
export function withinTextLimit(value, limit) {
  if (value.length <= limit) return true;
  let count = 0;
  for (const character of value) {
    if (++count > limit) return false;
  }
  return true;
}

export function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  if (year < 1 || month < 1 || month > 12 || day < 1) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return day <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
}

export function manualReviewStampData(value, detail = false) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const keys = detail ? DETAIL_KEYS : STAMP_KEYS;
  if (Object.keys(value).length !== keys.length || !keys.every((key) => Object.hasOwn(value, key))) return false;
  return typeof value.revision === 'string' && /^[a-f0-9]{64}$/.test(value.revision)
    && (value.status === 'verified' || value.status === 'needs_review')
    && typeof value.reason === 'string' && value.reason.trim().length > 0 && withinTextLimit(value.reason, 2000)
    && typeof value.reviewed_at === 'string'
    && /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value.reviewed_at)
    && validDay(value.reviewed_at.slice(0, 10)) && Number.isFinite(Date.parse(value.reviewed_at));
}
