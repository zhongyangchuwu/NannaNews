import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stagePublicFacts } from './stage-public-facts.mjs';
import { catalogProjections } from './catalog-projections.mjs';

const target = fileURLToPath(new URL('../public/data/', import.meta.url));
const args = process.argv.slice(2);
const ifMissing = args[0] === '--if-missing';
const sources = args.slice(ifMissing ? 1 : 0);
if (sources.length > 1) throw new Error('用法：npm run stage:data -- [--if-missing] [公开导出目录或 manifest.json 的公开 HTTP(S) URL]');
let input = sources[0] || process.env.STATIC_READER_DATA_SOURCE;
if (input && !ifMissing && process.argv.length > 3 || input && ifMissing && process.argv.length > 4) {
  throw new Error('用法：npm run stage:data -- [--if-missing] [公开导出目录或 manifest.json 的公开 HTTP(S) URL]');
}
let upgrade = false;
try {
  const existing = await lstat(target);
  if (ifMissing && existing.isDirectory() && !existing.isSymbolicLink()) {
    upgrade = true;
    input = target;
  } else {
    throw new Error('目标 reader/public/data 已存在；为避免误删，不会自动替换。请使用空工作树构建或先移走已生成目录。');
  }
} catch (error) { if (error.code !== 'ENOENT') throw error; }
if (!input) {
  const result = await stagePublicFacts(target);
  console.log(JSON.stringify({ ...result, output: 'public/data', source: 'activity-facts' }));
  process.exit(0);
}
const remote = /^https?:\/\//i.test(input);
const base = remote ? new URL(input) : null;
if (base && (base.username || base.password || base.search || base.hash || !base.pathname.endsWith('/manifest.json'))) {
  throw new Error('数据源必须是无凭据、无查询参数的公开 manifest.json URL。');
}
const source = remote ? null : await realpath(path.resolve(input));
if (source && (await lstat(path.resolve(input))).isSymbolicLink()) throw new Error('数据目录不能是符号链接。');
const staging = `${target.replace(/\/$/, '')}.stage-${randomUUID()}`;
const paths = new Set();
let bytes = 0;

function safePath(value, version) {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9_./-]+$/.test(value) || value.startsWith('/')
    || value.split('/').some((part) => !part || part === '.' || part === '..')
    || (version && !value.startsWith(`versions/${version}/`))) throw new Error('公开数据包含不安全或版本不一致的资源路径。');
  return value;
}

async function obtain(relative, limit = 256 * 1024 * 1024) {
  safePath(relative);
  let buffer;
  if (base) {
    const url = new URL(relative, new URL('./', base));
    if (url.origin !== base.origin || !url.pathname.startsWith(new URL('./', base).pathname)) throw new Error('数据资源越过公开数据根目录。');
    const response = await fetch(url, { redirect: 'error', credentials: 'omit', signal: AbortSignal.timeout(30_000) });
    if (!response.ok) throw new Error(`公开数据下载失败：${relative} (${response.status})`);
    const declared = Number(response.headers.get('content-length'));
    if (declared > limit) throw new Error(`公开资源过大：${relative}`);
    const chunks = [];
    let length = 0;
    for await (const chunk of response.body) {
      length += chunk.byteLength;
      if (length > limit) throw new Error(`公开资源过大：${relative}`);
      chunks.push(chunk);
    }
    buffer = Buffer.concat(chunks, length);
  } else {
    let filename = source;
    for (const component of relative.split('/')) {
      filename = path.join(filename, component);
      const info = await lstat(filename);
      if (info.isSymbolicLink()) throw new Error('公开数据不能包含符号链接。');
    }
    const info = await lstat(filename);
    if (!info.isFile() || info.size > limit) throw new Error(`公开资源不是大小允许的普通文件：${relative}`);
    buffer = await readFile(filename);
  }
  return buffer;
}

async function stageJson(relative) {
  const buffer = await obtain(relative);
  const value = JSON.parse(buffer.toString('utf8'));
  await stage(relative, buffer);
  return value;
}

async function stage(relative, buffer, generated = false) {
  if (paths.has(relative)) return;
  if (paths.size >= 10000) throw new Error('公开数据资源数超过构建上限。');
  paths.add(relative);
  if (upgrade && !generated) return;
  const filename = path.join(staging, relative);
  await mkdir(path.dirname(filename), { recursive: true });
  await writeFile(filename, buffer, { flag: 'wx' });
  bytes += buffer.byteLength;
}

async function eachLimited(values, action) {
  let index = 0;
  await Promise.all(Array.from({ length: Math.min(6, values.length) }, async () => {
    while (index < values.length) await action(values[index++]);
  }));
}
async function existingProjectionsComplete(manifest, version) {
  const catalogs = manifest.files.issue_catalogs;
  if (!catalogs || typeof catalogs !== 'object' || Array.isArray(catalogs)
    || typeof manifest.files.item_index !== 'string') return false;
  const issues = JSON.parse((await obtain(manifest.files.issues)).toString('utf8'));
  if (!Array.isArray(issues) || issues.length !== manifest.counts.issues) return false;
  const ids = new Set();
  for (const issue of issues) {
    if (!issue || typeof issue.id !== 'string' || ids.has(issue.id) || !Object.hasOwn(catalogs, issue.id)) return false;
    ids.add(issue.id);
  }
  if (Object.keys(catalogs).length !== ids.size) return false;
  const reserved = new Set(['catalog', 'issues', 'search_documents', 'search_index'].map((key) => manifest.files[key]));
  const seen = new Set();
  for (const relative of [manifest.files.item_index, ...Object.values(catalogs)]) {
    try { safePath(relative, version); } catch { return false; }
    if (!relative.endsWith('.json') || seen.has(relative) || reserved.has(relative)) return false;
    seen.add(relative);
    let filename = source;
    const components = relative.split('/');
    for (const [index, component] of components.entries()) {
      filename = path.join(filename, component);
      let info;
      try { info = await lstat(filename); } catch (error) {
        if (error.code === 'ENOENT') return false;
        throw error;
      }
      if (info.isSymbolicLink()) throw new Error('公开数据不能包含符号链接。');
      if (index === components.length - 1 ? !info.isFile() || info.size === 0 : !info.isDirectory()) return false;
    }
  }
  return true;
}


try {
  // Read the existing manifest before creating a staging tree so an already
  // compatible reader does not repeatedly copy or hash its public assets.
  const manifest = JSON.parse((await obtain('manifest.json')).toString('utf8'));
  if (manifest.schema_version !== 2 || manifest.generator !== 'webnews-static-v2'
    || !/^[a-f0-9]{64}$/.test(manifest.data_version) || !manifest.files || !manifest.counts) throw new Error('不是兼容的公开刊物数据导出。');
  if (Object.hasOwn(manifest.files, 'issue_catalogs') !== Object.hasOwn(manifest.files, 'item_index')) {
    throw new Error('公开数据的分期目录和消息索引必须同时提供。');
  }
  const version = manifest.data_version;
  for (const name of ['catalog', 'issues', 'search_documents', 'search_index']) safePath(manifest.files[name], version);
  if (upgrade && Object.hasOwn(manifest.files, 'item_index') && await existingProjectionsComplete(manifest, version)) {
    console.log(JSON.stringify({ output: 'public/data', staged: false, reason: 'already-exists' }));
    process.exit(0);
  }
  await mkdir(staging, { recursive: true });
  const [catalog, issues, searchDocuments] = await Promise.all([
    stageJson(manifest.files.catalog), stageJson(manifest.files.issues), stageJson(manifest.files.search_documents),
  ]);
  if (!Array.isArray(catalog.items) || !Array.isArray(catalog.occurrences) || !Array.isArray(issues) || !Array.isArray(searchDocuments)
    || catalog.items.length !== manifest.counts.items || catalog.occurrences.length !== manifest.counts.occurrences
    || issues.length !== manifest.counts.issues || searchDocuments.length !== manifest.counts.occurrences) throw new Error('公开数据数量与清单不一致。');
  const projections = catalogProjections(catalog, issues, version);
  if (projections.documentCount !== manifest.counts.documents) throw new Error('公开数据文档数量与清单不一致。');
  const searchIds = new Set(searchDocuments.map((document) => document?.id));
  if (searchIds.size !== projections.occurrenceIds.size || [...projections.occurrenceIds].some((id) => !searchIds.has(id))) {
    throw new Error('搜索文档与消息目录不一致。');
  }
  const catalogItems = new Map(catalog.items.map((item) => [item.id, item]));
  const catalogOccurrences = new Map(catalog.occurrences.map((occurrence) => [occurrence.id, occurrence.item_id]));
  const detailItems = new Set();
  const detailOccurrences = new Set();
  const shardPaths = [...new Set(catalog.items.map((item) => safePath(item.detail_path, version)))];
  const assets = new Map();
  for (const issue of issues) for (const document of issue.documents) {
    if (document.pdf_path) assets.set(safePath(document.pdf_path, version), 'pdf');
  }
  let itemCount = 0;
  let occurrenceCount = 0;
  await eachLimited(shardPaths, async (relative) => {
    const shard = await stageJson(relative);
    if (typeof shard !== 'object' || shard === null || Array.isArray(shard)) throw new Error('消息分片格式无效。');
    for (const [id, item] of Object.entries(shard)) {
      if (id !== item.id || !Array.isArray(item.occurrences) || detailItems.has(id)
        || catalogItems.get(id)?.detail_path !== relative) throw new Error('消息分片标识无效。');
      detailItems.add(id);
      itemCount++;
      occurrenceCount += item.occurrences.length;
      for (const occurrence of item.occurrences) {
        if (catalogOccurrences.get(occurrence.id) !== id || detailOccurrences.has(occurrence.id)) throw new Error('消息分片条目不一致。');
        detailOccurrences.add(occurrence.id);
        for (const image of occurrence.images) assets.set(safePath(image.path, version), 'image');
        for (const image of occurrence.primary_images ?? []) assets.set(safePath(image.path, version), 'image');
      }
    }
  });
  if (itemCount !== manifest.counts.items || occurrenceCount !== manifest.counts.occurrences) throw new Error('消息分片与清单不一致。');
  const pdfCount = [...assets.values()].filter((kind) => kind === 'pdf').length;
  if (pdfCount !== manifest.counts.pdfs || assets.size - pdfCount !== manifest.counts.images) throw new Error('公开资源数量与清单不一致。');
  await eachLimited([...assets], async ([relative, kind]) => {
    const match = /^([a-f0-9]{64})\.(png|jpe?g|webp|gif|pdf)$/.exec(path.basename(relative));
    if (!match || (kind === 'pdf') !== (match[2] === 'pdf')) throw new Error('公开资源必须使用内容哈希文件名和安全格式。');
    const buffer = await obtain(relative, 64 * 1024 * 1024);
    if (createHash('sha256').update(buffer).digest('hex') !== match[1]) throw new Error('公开资源内容哈希不匹配。');
    await stage(relative, buffer);
  });
  for (const [relative, value] of Object.entries(projections.files)) {
    if (paths.has(relative) || relative === manifest.files.search_index) throw new Error('生成的目录路径与公开资源冲突。');
    await stage(relative, Buffer.from(`${JSON.stringify(value)}\n`), true);
  }
  manifest.files.issue_catalogs = projections.issueCatalogs;
  manifest.files.item_index = projections.itemIndex;
  await stage('manifest.json', Buffer.from(`${JSON.stringify(manifest)}\n`), true);
  if (upgrade) {
    // Publish projections first and atomically replace the manifest last. The old
    // manifest remains usable if validation or copying fails before that point.
    for (const relative of [...Object.keys(projections.files), 'manifest.json']) {
      const parts = relative.split('/');
      let directory = target;
      for (const part of parts.slice(0, -1)) {
        directory = path.join(directory, part);
        try { await mkdir(directory); } catch (error) { if (error.code !== 'EEXIST') throw error; }
        const info = await lstat(directory);
        if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('公开数据不能包含符号链接。');
      }
      const destination = path.join(target, relative);
      try {
        const info = await lstat(destination);
        if (!info.isFile() || info.isSymbolicLink()) throw new Error('公开数据不能包含符号链接。');
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      await rename(path.join(staging, relative), destination);
    }
    await rm(staging, { recursive: true, force: true });
    console.log(JSON.stringify({ output: 'public/data', staged: false, reason: 'already-exists' }));
    process.exit(0);
  }
  await rename(staging, target);
  console.log(JSON.stringify({ data_version: version, files: paths.size, bytes, counts: manifest.counts, output: 'public/data' }));
} catch (error) {
  await rm(staging, { recursive: true, force: true });
  throw error;
}
