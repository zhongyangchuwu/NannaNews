export type SearchErrorCode = 'INDEX_URL' | 'INDEX_FETCH' | 'INDEX_INVALID' | 'WORKER_FAILED' | 'DISPOSED' | 'SUPERSEDED';

export class SearchError extends Error {
  constructor(readonly code: SearchErrorCode, message: string) { super(message); this.name = 'SearchError'; }
}

export type SearchWorkerRequest = { type: 'init'; indexUrl: string } | { type: 'search'; id: number; q: string };
export type SearchWorkerReply = { type: 'result'; id: number; ids: string[] }
  | { type: 'error'; id: number; error: { code: SearchErrorCode; message: string } };

type PendingSearch = { id: number; resolve: (ids: string[]) => void; reject: (error: SearchError) => void };

export class SearchClient {
  private readonly worker: Worker;
  private pending: PendingSearch | null = null;
  private nextId = 0;
  private disposed = false;
  private failure: SearchError | null = null;

  constructor(indexUrl: string) {
    let url: URL;
    try { url = new URL(indexUrl, globalThis.location.href); }
    catch { throw new SearchError('INDEX_URL', '搜索索引地址无效。'); }
    if (url.origin !== globalThis.location.origin || !['http:', 'https:'].includes(url.protocol)) {
      throw new SearchError('INDEX_URL', '搜索索引必须来自当前静态站点。');
    }
    try { this.worker = new Worker(new URL('./search.worker.ts', import.meta.url), { type: 'module' }); }
    catch { throw new SearchError('WORKER_FAILED', '无法启动搜索线程，请重新加载页面。'); }
    this.worker.onmessage = (event: MessageEvent<SearchWorkerReply>) => {
      const reply = event.data;
      if (!this.pending || reply.id !== this.pending.id) return;
      const pending = this.pending;
      this.pending = null;
      if (reply.type === 'result' && Array.isArray(reply.ids) && reply.ids.every((id) => typeof id === 'string')) {
        pending.resolve(reply.ids);
      } else if (reply.type === 'error' && reply.error) {
        pending.reject(new SearchError(reply.error.code, reply.error.message));
      } else {
        pending.reject(new SearchError('WORKER_FAILED', '搜索线程返回了无效结果。'));
      }
    };
    const fail = (): void => {
      this.failure = new SearchError('WORKER_FAILED', '搜索线程无法运行，请重试加载。');
      this.pending?.reject(this.failure);
      this.pending = null;
      this.worker.terminate();
    };
    this.worker.onerror = (event) => { event.preventDefault(); fail(); };
    this.worker.onmessageerror = fail;
    try { this.worker.postMessage({ type: 'init', indexUrl: url.href } satisfies SearchWorkerRequest); }
    catch { this.worker.terminate(); throw new SearchError('WORKER_FAILED', '无法初始化搜索线程。'); }
  }

  search(q: string): Promise<string[]> {
    if (this.disposed) return Promise.reject(new SearchError('DISPOSED', '搜索已关闭。'));
    if (this.failure) return Promise.reject(this.failure);
    this.pending?.reject(new SearchError('SUPERSEDED', '搜索已被较新的查询替代。'));
    const id = ++this.nextId;
    const { promise, resolve, reject } = Promise.withResolvers<string[]>();
    this.pending = { id, resolve, reject };
    try { this.worker.postMessage({ type: 'search', id, q } satisfies SearchWorkerRequest); }
    catch {
      this.pending = null;
      reject(new SearchError('WORKER_FAILED', '无法向搜索线程发送查询。'));
    }
    return promise;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.pending?.reject(new SearchError('DISPOSED', '搜索已关闭。'));
    this.pending = null;
    this.worker.terminate();
  }
}
