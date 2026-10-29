import { useEffect, useRef, useState } from 'react';
import { Button } from './button';

const SHARE_PATH = /^share\/(?:items\/item-[a-f0-9]{24}\/occ-[a-f0-9]{24}|issues\/issue-[a-f0-9]{24})\/$/;

type Feedback = { kind: 'copied' | 'manual'; message: string; content?: string };

function shareUrl(path: string): URL {
  if (!SHARE_PATH.test(path)) throw new TypeError('Invalid share path.');
  const configuredBase = import.meta.env.BASE_URL;
  const base = configuredBase.endsWith('/') ? configuredBase : `${configuredBase}/`;
  const baseUrl = new URL(base, window.location.href);
  const url = new URL(path, baseUrl);
  if (url.origin !== window.location.origin || !url.pathname.startsWith(baseUrl.pathname)) {
    throw new TypeError('Share path escapes the reader base path.');
  }
  return url;
}


export function ShareButton({ title, text, path, label }: {
  title: string;
  text: string;
  path: string;
  label?: string;
}) {
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const manualCopy = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (feedback?.kind === 'manual') {
      manualCopy.current?.focus();
      manualCopy.current?.select();
    }
  }, [feedback]);

  async function share() {
    setFeedback(null);
    let url: URL;
    try {
      url = shareUrl(path);
    } catch {
      setFeedback({ kind: 'manual', message: '无法生成安全的分享链接。' });
      return;
    }
    const content = [title, text, url.href].filter(Boolean).join('\n');

    if (typeof navigator.share === 'function') {
      try {
        await navigator.share({ title, text, url: url.href });
        return;
      } catch (error) {
        if (typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError') return;
      }
    }

    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API is unavailable.');
      await navigator.clipboard.writeText(content);
      setFeedback({ kind: 'copied', message: '分享内容已复制到剪贴板。' });
    } catch {
      setFeedback({ kind: 'manual', message: '无法自动复制，请手动复制以下分享内容。', content });
    }
  }

  return (
    <div className="share-control">
      <Button type="button" variant="outline" size="sm" onClick={share}>{label ?? '分享'}</Button>
      {feedback && <p className="share-feedback" role={feedback.kind === 'manual' ? 'alert' : 'status'}>{feedback.message}</p>}
      {feedback?.kind === 'manual' && feedback.content && <textarea
        ref={manualCopy}
        className="share-manual-copy"
        aria-label="可手动复制的分享内容"
        readOnly
        rows={4}
        value={feedback.content}
      />}
    </div>
  );
}
