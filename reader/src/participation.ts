import type { ActivityField } from './types';

const HTTP_URL_TOKEN = /https?:\/\/[^\s<>"）)」』，。；、]+/gi;

export function participationUrl(field: ActivityField | undefined): string | null {
  if (!field?.value) return null;
  const cited = new Set<string>();
  for (const citation of field.evidence) {
    for (const match of citation.text.matchAll(HTTP_URL_TOKEN)) cited.add(match[0]);
  }
  for (const match of field.value.matchAll(HTTP_URL_TOKEN)) {
    if (!cited.has(match[0])) continue;
    try {
      const url = new URL(match[0]);
      if (url.protocol === 'https:' || url.protocol === 'http:') return match[0];
    } catch { /* A malformed token is not a participation link. */ }
  }
  return null;
}
