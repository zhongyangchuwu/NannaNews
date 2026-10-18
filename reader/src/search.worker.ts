import MiniSearch from 'minisearch';
import { searchOptions, tokenize } from './tokenize.mjs';
import type { SearchErrorCode, SearchWorkerReply, SearchWorkerRequest } from './search';

const scope = self as unknown as {
  onmessage: ((event: MessageEvent<SearchWorkerRequest>) => void) | null;
  postMessage(reply: SearchWorkerReply): void;
};

class IndexError extends Error {
  constructor(readonly code: SearchErrorCode, message: string) { super(message); }
}

let indexUrl: string | null = null;
let indexPromise: Promise<MiniSearch> | null = null;
let latestId = 0;

async function loadIndex(): Promise<MiniSearch> {
  if (!indexUrl) throw new IndexError('INDEX_URL', '搜索索引尚未初始化。');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30_000);
  let serialized: string;
  try {
    const response = await fetch(indexUrl, { signal: controller.signal, credentials: 'omit', mode: 'same-origin', redirect: 'error' });
    if (!response.ok) throw new IndexError('INDEX_FETCH', '无法读取静态搜索索引，请重试加载。');
    serialized = await response.text();
  } catch (error) {
    if (error instanceof IndexError) throw error;
    throw new IndexError('INDEX_FETCH', '搜索索引下载失败或超时，请重试加载。');
  } finally { clearTimeout(timeout); }
  try { return MiniSearch.loadJSON(serialized, searchOptions); }
  catch { throw new IndexError('INDEX_INVALID', '静态搜索索引损坏或版本不兼容，请重试加载。'); }
}

async function search(id: number, q: string): Promise<void> {
  try {
    // Loading/deserialize happens once, entirely off the UI thread. No runtime addAll.
    indexPromise ??= loadIndex();
    const index = await indexPromise;
    if (id !== latestId) return;
    const ids = tokenize(q).length === 0 ? [] : index.search(q).map((result) => String(result.id));
    if (id === latestId) scope.postMessage({ type: 'result', id, ids });
  } catch (error) {
    if (id !== latestId) return;
    const failure = error instanceof IndexError ? error : new IndexError('INDEX_INVALID', '搜索索引无法处理查询，请重试加载。');
    scope.postMessage({ type: 'error', id, error: { code: failure.code, message: failure.message } });
  }
}

scope.onmessage = (event) => {
  const message = event.data;
  if (message.type === 'init') {
    indexUrl = message.indexUrl;
  } else if (message.type === 'search') {
    latestId = message.id;
    void search(message.id, message.q);
  }
};
