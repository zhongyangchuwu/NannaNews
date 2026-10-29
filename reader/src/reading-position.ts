import { useEffect, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigationType } from 'react-router';
import type { ReaderState } from './types';
import { parseFilters, serializeFilters } from './filter';

const STORAGE_KEY = 'nannanews.reading-positions.v1';
const MAX_POSITIONS = 40;
const ITEM_ID = /^item-[a-f0-9]{24}$/;
const OCCURRENCE_ID = /^occ-[a-f0-9]{24}$/;
const isItem = (path: string) => /^\/items\/item-[a-f0-9]{24}$/.test(path);
const isList = (path: string) => ['/', '/activities', '/history', '/favorites'].includes(path)
  || /^\/issues\/issue-[a-f0-9]{24}$/.test(path);

type Position = { y: number; offset: number; item: string; occurrence: string };
type Route = { key: string; path: string; handled: boolean };

function listKey(path: string, search: string): string {
  const params = new URLSearchParams(search);
  let query = params;
  try { query = serializeFilters(parseFilters(params)); }
  catch { /* ReaderProvider reports invalid filters; restoration must not replace its error page. */ }
  query.sort();
  return path + (query.size ? `?${query}` : '');
}

function readPositions(): Map<string, Position> {
  const positions = new Map<string, Position>();
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    if (!stored || stored.length > 200_000) return positions;
    const entries: unknown = JSON.parse(stored);
    if (!Array.isArray(entries)) return positions;
    for (const entry of entries.slice(-MAX_POSITIONS)) {
      if (!Array.isArray(entry) || entry.length !== 2) continue;
      const [key, value] = entry;
      if (typeof key !== 'string' || key.length > 4_000 || !value || typeof value !== 'object') continue;
      const url = new URL(key, 'https://reader.invalid');
      if (url.origin !== 'https://reader.invalid' || !isList(url.pathname) || listKey(url.pathname, url.search) !== key) continue;
      if (!Number.isFinite(value.y) || value.y < 0 || value.y > 100_000_000
        || !Number.isFinite(value.offset) || Math.abs(value.offset) > 10_000_000
        || !ITEM_ID.test(value.item) || !OCCURRENCE_ID.test(value.occurrence)) continue;
      positions.set(key, { y: value.y, offset: value.offset, item: value.item, occurrence: value.occurrence });
    }
  } catch { /* Storage is optional; in-memory restoration still works. */ }
  return positions;
}

export function useReadingPosition(state: ReaderState) {
  const location = useLocation();
  const navigationType = useNavigationType();
  const positions = useRef<Map<string, Position> | null>(null);
  const route = useRef<Route | null>(null);
  const returnIntent = useRef<string | null>(null);
  const pending = useRef<Position | null>(null);
  const highlight = useRef<{ element: HTMLElement; timer: number } | null>(null);

  useEffect(() => {
    positions.current = readPositions();
    const previousRestoration = window.history.scrollRestoration;
    window.history.scrollRestoration = 'manual';
    const capture = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>('a[href]') : null;
      if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download')) return;
      const href = new URL(anchor.href, window.location.href);
      if (href.origin !== window.location.origin || href.pathname !== window.location.pathname || !href.hash.startsWith('#/')) return;
      const destination = new URL(href.hash.slice(1), 'https://reader.invalid');
      returnIntent.current = anchor.hasAttribute('data-reader-return') && isList(destination.pathname)
        ? listKey(destination.pathname, destination.search) : null;
      const current = new URL(window.location.hash.slice(1) || '/', 'https://reader.invalid');
      if (!isList(current.pathname) || !isItem(destination.pathname)) return;
      const article = anchor.closest<HTMLElement>('article[data-item-id][data-occurrence-id]');
      const item = article?.dataset.itemId;
      const occurrence = article?.dataset.occurrenceId;
      if (!article || !item || !occurrence || !ITEM_ID.test(item) || !OCCURRENCE_ID.test(occurrence)) return;
      const key = listKey(current.pathname, current.search);
      const cache = positions.current!;
      cache.delete(key);
      cache.set(key, { y: window.scrollY, offset: article.getBoundingClientRect().top, item, occurrence });
      if (cache.size > MAX_POSITIONS) cache.delete(cache.keys().next().value!);
      try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...cache])); }
      catch { /* Keep the in-memory position when browser storage is denied. */ }
    };
    document.addEventListener('click', capture, true);
    return () => {
      document.removeEventListener('click', capture, true);
      window.history.scrollRestoration = previousRestoration;
      if (highlight.current) {
        window.clearTimeout(highlight.current.timer);
        delete highlight.current.element.dataset.readingRestored;
      }
    };
  }, []);

  useLayoutEffect(() => {
    let current = route.current;
    if (!current || current.key !== location.key) {
      const key = listKey(location.pathname, location.search);
      const returning = returnIntent.current === key || navigationType === 'POP' && current && isItem(current.path);
      pending.current = returning && isList(location.pathname) ? positions.current?.get(key) || null : null;
      returnIntent.current = null;
      current = { key: location.key, path: location.pathname, handled: false };
      route.current = current;
    }
    if (current.handled || state.status !== 'ready' || isList(location.pathname) && state.searching) return;
    current.handled = true;
    // The year archive owns its own year-heading restoration.
    if (location.pathname === '/issues') return;
    const saved = pending.current;
    pending.current = null;
    if (!saved) {
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
      return;
    }
    // Run after the filtered list has committed, never against the outgoing detail DOM.
    const article = document.querySelector<HTMLElement>(`article[data-item-id="${saved.item}"][data-occurrence-id="${saved.occurrence}"]`);
    const top = article ? window.scrollY + article.getBoundingClientRect().top - saved.offset : saved.y;
    window.scrollTo({ top: Math.max(0, top), left: 0, behavior: 'instant' });
    if (article) {
      article.querySelector<HTMLElement>('h3 a, h2 a, a')?.focus({ preventScroll: true });
      if (highlight.current) {
        window.clearTimeout(highlight.current.timer);
        delete highlight.current.element.dataset.readingRestored;
      }
      article.dataset.readingRestored = 'true';
      highlight.current = { element: article, timer: window.setTimeout(() => {
        delete article.dataset.readingRestored;
        highlight.current = null;
      }, 2_500) };
    }
  }, [location.key, location.pathname, location.search, navigationType, state.status, state.searching]);
}
