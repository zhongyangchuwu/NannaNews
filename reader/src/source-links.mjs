// A newsletter archive is provenance, not the original activity announcement.
export function originalSourceUrl(raw) {
  try {
    const url = new URL(raw);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    const host = url.hostname.toLowerCase().replace(/\.+$/, '');
    if (host === 'nik-nul.github.io') return null;
    if (host === 'zhongyangchuwu.github.io') {
      // Decode escaped path separators and letters before matching the deployed base path.
      const path = decodeURIComponent(url.pathname);
      if (/^\/nannanews(?:\/|$)/i.test(path)) return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

export function sourceUrls(raws) {
  return [...new Set(raws.map(originalSourceUrl).filter((url) => url !== null))];
}
