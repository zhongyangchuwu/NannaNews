import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stagePublicFacts } from './stage-public-facts.mjs';

const target = fileURLToPath(new URL('../public/data/', import.meta.url));
const args = process.argv.slice(2);
const ifMissing = args[0] === '--if-missing';
const sources = args.slice(ifMissing ? 1 : 0);
if (sources.length > 1) throw new Error('用法：npm run stage:data -- [--if-missing] [公开导出目录或 manifest.json 的公开 HTTP(S) URL]');
const input = sources[0] || process.env.STATIC_READER_DATA_SOURCE;
if (input && !ifMissing && process.argv.length > 3 || input && ifMissing && process.argv.length > 4) {
  throw new Error('用法：npm run stage:data -- [--if-missing] [公开导出目录或 manifest.json 的公开 HTTP(S) URL]');
}
try {
  const existing = await lstat(target);
  if (ifMissing && existing.isDirectory() && !existing.isSymbolicLink()) {
    console.log(JSON.stringify({ output: 'public/data', staged: false, reason: 'already-exists' }));
    process.exit(0);
  }
  throw new Error('目标 reader/public/data 已存在；为避免误删，不会自动替换。请使用空工作树构建或先移走已生成目录。');
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

async function stage(relative, buffer) {
  if (paths.has(relative)) return;
  if (paths.size >= 10000) throw new Error('公开数据资源数超过构建上限。');
  paths.add(relative);
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


try {
  const manifest = JSON.parse((await obtain('manifest.json')).toString('utf8'));
  if (manifest.schema_version !== 2 || manifest.generator !== 'webnews-static-v2'
    || !/^[a-f0-9]{64}$/.test(manifest.data_version) || !manifest.files || !manifest.counts) throw new Error('不是兼容的公开刊物数据导出。');
  if (Object.hasOwn(manifest.files, 'issue_catalogs') !== Object.hasOwn(manifest.files, 'item_index')) {
    throw new Error('公开数据的分期目录和消息索引必须同时提供。');
  }
  const version = manifest.data_version;
  for (const name of ['catalog', 'issues', 'search_documents', 'search_index']) safePath(manifest.files[name], version);
  await mkdir(staging, { recursive: true });
  const [catalog, issues, searchDocuments] = await Promise.all([
    stageJson(manifest.files.catalog), stageJson(manifest.files.issues), stageJson(manifest.files.search_documents),
  ]);
  if (!Array.isArray(catalog.items) || !Array.isArray(catalog.occurrences) || !Array.isArray(issues) || !Array.isArray(searchDocuments)
    || catalog.items.length !== manifest.counts.items || catalog.occurrences.length !== manifest.counts.occurrences
    || issues.length !== manifest.counts.issues || searchDocuments.length !== manifest.counts.occurrences) throw new Error('公开数据数量与清单不一致。');
  const itemRows = new Map(catalog.items.map((item) => [item.id, { item, count: 0, latest: false }]));
  const issueRows = new Map(issues.map((issue) => [issue.id, { issue, count: 0 }]));
  const documents = new Map();
  for (const issue of issues) for (const document of issue.documents) {
    if (documents.has(document.id)) throw new Error('公开数据包含重复文档。');
    documents.set(document.id, issue.id);
  }
  if (itemRows.size !== catalog.items.length || issueRows.size !== issues.length
    || documents.size !== manifest.counts.documents) throw new Error('公开数据标识或文档数量不一致。');
  const occurrenceIds = new Set();
  for (const row of catalog.occurrences) {
    const item = itemRows.get(row.item_id);
    if (!item || occurrenceIds.has(row.id)) throw new Error('公开数据包含无效或重复消息记录。');
    occurrenceIds.add(row.id);
    item.count++;
    item.latest ||= row.id === item.item.latest_occurrence_id;
    if (row.kind === 'standalone') {
      if (row.issue_id !== null || row.document_id !== null) throw new Error('独立收录不能伪造期次引用。');
    } else {
      const issue = issueRows.get(row.issue_id);
      if (!issue || documents.get(row.document_id) !== row.issue_id) throw new Error('公开数据期次引用无效。');
      issue.count++;
    }
  }
  if ([...itemRows.values()].some(({ item, count, latest }) => count !== item.occurrence_count || !latest)
    || [...issueRows.values()].some(({ issue, count }) => count !== issue.occurrence_count)) throw new Error('公开数据消息或期次数量不一致。');
  const searchIds = new Set(searchDocuments.map((document) => document?.id));
  if (searchIds.size !== occurrenceIds.size || [...occurrenceIds].some((id) => !searchIds.has(id))) {
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
  // This directory is the operator/build input, not the deployed browser data.
  // Old additive projections are regenerated as normalized v3 data by build-public.
  manifest.files = Object.fromEntries(['catalog', 'issues', 'search_documents', 'search_index']
    .map((key) => [key, manifest.files[key]]));
  await stage('manifest.json', Buffer.from(`${JSON.stringify(manifest)}\n`));
  await rename(staging, target);
  console.log(JSON.stringify({ data_version: version, files: paths.size, bytes, counts: manifest.counts, output: 'public/data' }));
} catch (error) {
  await rm(staging, { recursive: true, force: true });
  throw error;
}
