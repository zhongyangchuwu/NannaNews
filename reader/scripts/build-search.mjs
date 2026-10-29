import { lstat, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import MiniSearch from 'minisearch';
import { searchOptions } from '../src/tokenize.mjs';

const defaultDataDir = fileURLToPath(new URL('../public/data/', import.meta.url));
const SOURCE_ID = /^source-[a-f0-9]{24}$/;

async function secureFile(root, relative, version, allowMissing = false) {
  if (typeof relative !== 'string' || isAbsolute(relative) || !/^[A-Za-z0-9._/-]+$/.test(relative)
    || !relative.startsWith(`versions/${version}/`) || !relative.endsWith('.json')) {
    throw new Error('Manifest file paths must be versioned relative JSON paths.');
  }
  const segments = relative.split('/');
  if (segments.some((part) => part === '' || part === '.' || part === '..')) throw new Error('Manifest file path escapes the data root.');
  let path = root;
  for (let i = 0; i < segments.length; i += 1) {
    path = join(path, segments[i]);
    let stat;
    try { stat = await lstat(path); }
    catch (error) {
      if (allowMissing && i === segments.length - 1 && error.code === 'ENOENT') return path;
      throw error;
    }
    if (stat.isSymbolicLink() || (i < segments.length - 1 ? !stat.isDirectory() : !stat.isFile())) {
      throw new Error('Manifest paths must not use symlinks or unexpected file types.');
    }
  }
  return path;
}

export async function buildSearch(dataDir = defaultDataDir) {
  const requestedRoot = resolve(dataDir);
  const rootStat = await lstat(requestedRoot);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) throw new Error('Data root must be a real directory.');
  const root = await realpath(requestedRoot);
  const manifestPath = join(root, 'manifest.json');
  const manifestStat = await lstat(manifestPath);
  if (!manifestStat.isFile() || manifestStat.isSymbolicLink()) throw new Error('Manifest must be a regular file.');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  const factsOnly = manifest.public_facts_only === true;
  if (manifest.schema_version !== 2 || manifest.generator !== 'webnews-static-v2'
    || typeof manifest.data_version !== 'string' || !/^[a-f0-9]{64}$/.test(manifest.data_version)
    || manifest.public_facts_only !== undefined && typeof manifest.public_facts_only !== 'boolean'
    || (factsOnly ? manifest.generated_at !== undefined
      : typeof manifest.generated_at !== 'string' || !Number.isFinite(Date.parse(manifest.generated_at)))) {
    throw new Error('Unsupported or invalid public-data manifest.');
  }
  for (const key of ['items', 'occurrences', 'issues', 'documents', 'images', 'pdfs']) {
    if (!Number.isSafeInteger(manifest.counts?.[key]) || manifest.counts[key] < 0) throw new Error('Invalid public-data counts.');
  }
  const paths = {};
  const used = new Set();
  for (const key of ['catalog', 'issues', 'search_documents', 'search_index']) {
    paths[key] = await secureFile(root, manifest.files?.[key], manifest.data_version, key === 'search_index');
    if (used.has(paths[key])) throw new Error('Manifest file paths must be distinct.');
    used.add(paths[key]);
  }
  const [documents, catalog, issues] = await Promise.all([
    readFile(paths.search_documents, 'utf8').then(JSON.parse),
    readFile(paths.catalog, 'utf8').then(JSON.parse),
    readFile(paths.issues, 'utf8').then(JSON.parse),
  ]);
  if (!Array.isArray(documents) || documents.length !== manifest.counts.occurrences
    || !Array.isArray(catalog.items) || catalog.items.length !== manifest.counts.items
    || !Array.isArray(catalog.occurrences) || catalog.occurrences.length !== manifest.counts.occurrences
    || !Array.isArray(issues) || issues.length !== manifest.counts.issues) {
    throw new Error('Public-data row counts do not match the manifest.');
  }
  const ids = new Set();
  for (const document of documents) {
    if (!document || typeof document.id !== 'string' || !document.id || typeof document.title !== 'string'
      || typeof document.text !== 'string' || ids.has(document.id)) throw new Error('Invalid or duplicate search document.');
    ids.add(document.id);
  }
  const occurrenceIds = new Set();
  const itemIds = new Set(catalog.items.map((item) => item.id));
  if (itemIds.size !== catalog.items.length || [...itemIds].some((id) => typeof id !== 'string' || !id)) {
    throw new Error('Invalid or duplicate catalog item.');
  }
  for (const occurrence of catalog.occurrences) {
    const standalone = occurrence?.kind === 'standalone';
    const validReferences = standalone
      ? occurrence.issue_id === null && occurrence.document_id === null && occurrence.issue_date === null
        && occurrence.activity_id === occurrence.item_id && SOURCE_ID.test(occurrence.source_id)
      : ['summary', 'detail', 'notice'].includes(occurrence?.kind)
        && typeof occurrence.issue_id === 'string' && typeof occurrence.document_id === 'string';
    if (!ids.has(occurrence.id) || occurrenceIds.has(occurrence.id) || !itemIds.has(occurrence.item_id)
      || !validReferences || (occurrence.activity_id !== undefined && occurrence.activity_id !== occurrence.item_id)) {
      throw new Error('Catalog and search occurrence identities do not match.');
    }
    occurrenceIds.add(occurrence.id);
  }
  const documentIds = new Set();
  const issueIds = new Set();
  for (const issue of issues) {
    if (!issue || typeof issue.id !== 'string' || !issue.id || issueIds.has(issue.id) || !Array.isArray(issue.documents)) {
      throw new Error('Invalid or duplicate public issue.');
    }
    issueIds.add(issue.id);
    for (const document of issue.documents) {
      if (!document || typeof document.id !== 'string' || !document.id || documentIds.has(document.id)) {
        throw new Error('Invalid or duplicate public document.');
      }
      documentIds.add(document.id);
    }
  }
  if (documentIds.size !== manifest.counts.documents || catalog.occurrences.some((occurrence) =>
    occurrence.kind === 'standalone' ? occurrence.issue_id !== null || occurrence.document_id !== null
      : !issueIds.has(occurrence.issue_id) || !documentIds.has(occurrence.document_id))) {
    throw new Error('Public issue/document counts or occurrence references do not match.');
  }
  const index = new MiniSearch(searchOptions);
  index.addAll(documents);
  const serialized = JSON.stringify(index);
  const temporary = join(dirname(paths.search_index), `.search-index-${process.pid}-${crypto.randomUUID()}.tmp`);
  try {
    await writeFile(temporary, serialized, { encoding: 'utf8', flag: 'wx' });
    // Check again before publishing; never overwrite a symlink target or another manifest input.
    await secureFile(root, manifest.files.search_index, manifest.data_version, true);
    await rename(temporary, paths.search_index);
  } finally { await rm(temporary, { force: true }); }
  return { occurrences: documents.length, bytes: Buffer.byteLength(serialized), data_version: manifest.data_version,
    search_index: manifest.files.search_index };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length > 3) {
    console.error('Usage: node build-search.mjs [data-dir]');
    process.exitCode = 1;
  } else {
    try { console.log(JSON.stringify(await buildSearch(process.argv[2]))); }
    catch (error) { console.error(`Search index build failed: ${error.message}`); process.exitCode = 1; }
  }
}
