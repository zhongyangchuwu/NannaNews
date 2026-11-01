import { createHash, randomUUID } from 'node:crypto';
import { copyFile, link, lstat, mkdir, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import MiniSearch from 'minisearch';
import { searchOptions } from '../src/tokenize.mjs';
import { encodePartitionedFacts } from '../src/public-format.mjs';

const defaultSource = fileURLToPath(new URL('../public/data/', import.meta.url));
const defaultOutput = fileURLToPath(new URL('../.reader-site/data/', import.meta.url));
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const fail = (message) => { throw new Error(`Public build: ${message}`); };
function safePath(value, version) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_./-]+$/.test(value) || !value.startsWith(`versions/${version}/`) || value.split('/').some((p) => !p || p === '.' || p === '..')) fail('unsafe or unversioned path');
  return value;
}
async function noSymlinks(absolute, allowMissing = false) {
  const parsed = path.parse(absolute); let current = parsed.root;
  for (const part of absolute.slice(parsed.root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    let stat;
    try { stat = await lstat(current); } catch (error) { if (allowMissing && error.code === 'ENOENT') return; throw error; }
    if (stat.isSymbolicLink()) fail('symlink in path');
  }
}
async function file(root, relative, version) {
  if (relative !== 'manifest.json') safePath(relative, version);
  const filename = path.join(root, relative);
  await noSymlinks(filename);
  const stat = await lstat(filename);
  if (!stat.isFile() || stat.size > 256 * 1024 * 1024) fail('source is not a permitted regular file');
  return filename;
}
async function json(root, relative, version) { return JSON.parse(await readFile(await file(root, relative, version), 'utf8')); }
function manifestData(manifest) {
  if (!object(manifest) || manifest.schema_version !== 2 || manifest.generator !== 'webnews-static-v2' || !/^[a-f0-9]{64}$/.test(manifest.data_version) || !object(manifest.files) || !object(manifest.counts) || (manifest.public_facts_only !== undefined && typeof manifest.public_facts_only !== 'boolean') || (manifest.content_rules_version !== undefined && typeof manifest.content_rules_version !== 'string') || (manifest.public_facts_only === true ? manifest.generated_at !== undefined : typeof manifest.generated_at !== 'string' || !Number.isFinite(Date.parse(manifest.generated_at)))) fail('invalid source manifest');
  for (const name of ['items','occurrences','issues','documents','images','pdfs']) if (!Number.isSafeInteger(manifest.counts[name]) || manifest.counts[name] < 0) fail('invalid counts');
  const paths = ['catalog','issues','search_documents','search_index'].map((key) => safePath(manifest.files[key], manifest.data_version));
  if (new Set(paths).size !== paths.length || paths.some((p) => !p.endsWith('.json'))) fail('source file paths');
}
async function ownedOutput(root) {
  const stat = await lstat(root);
  if (!stat.isDirectory() || stat.isSymbolicLink()) fail('output must be a directory');
  const names = await readdir(root);
  if (!names.length) return;
  const manifest = await json(root, 'manifest.json');
  if (manifest?.schema_version !== 3 || manifest.generator !== 'nannanews-reader-v3' || !/^[a-f0-9]{64}$/.test(manifest.data_version) || !object(manifest.files) || !object(manifest.files.partitions)) fail('output is not owned v3 data');
  const version = manifest.data_version;
  const prefix = `versions/${version}`;
  if (manifest.files.issues !== `${prefix}/issues.json` || manifest.files.item_index !== `${prefix}/item-index.json` || manifest.files.search_index !== `${prefix}/search-index.json`) fail('invalid owned file layout');
  for (const [key, value] of Object.entries(manifest.files.partitions)) if (!/^[A-Za-z0-9_-]+$/.test(key) || value !== `${prefix}/facts/${key}.json`) fail('invalid owned partition layout');
  const expected = new Set(['manifest.json']);
  const add = (v) => { safePath(v, version); if (expected.has(v)) fail('duplicate owned path'); expected.add(v); };
  add(manifest.files.issues); add(manifest.files.item_index); add(manifest.files.search_index);
  for (const value of Object.values(manifest.files.partitions)) add(value);
  const entries = await json(root, manifest.files.item_index, version);
  if (!Array.isArray(entries)) fail('invalid owned item index');
  for (const entry of entries) if (entry.detail_path) {
    if (typeof entry.id !== 'string' || !entry.id.startsWith('item-') || entry.detail_path !== `${prefix}/details/${entry.id.slice(5, 7)}.json`) fail('invalid owned detail layout');
    if (!expected.has(entry.detail_path)) add(entry.detail_path);
  }
  // Assets are permitted only when referenced by the generated issue/detail JSON.
  const assets = new Set();
  function scan(value) {
    if (Array.isArray(value)) value.forEach(scan);
    else if (object(value)) for (const [k,v] of Object.entries(value)) {
      if ((k === 'path' || k === 'pdf_path') && typeof v === 'string' && v.startsWith(`versions/${version}/`)) assets.add(v);
      else scan(v);
    }
  }
  scan(await json(root, manifest.files.issues, version));
  for (const relative of [...expected]) if (relative.includes('/details/')) scan(await json(root, relative, version));
  for (const asset of assets) {
    const match = /^([a-f0-9]{64})\.(png|jpe?g|webp|gif|pdf)$/.exec(path.basename(asset));
    if (!match) fail('invalid owned asset');
    const contents = await readFile(await file(root, asset, version));
    if (createHash('sha256').update(contents).digest('hex') !== match[1]) fail('invalid owned asset hash');
    add(asset);
  }
  async function walk(dir, prefix = '') {
    for (const entry of await readdir(dir, {withFileTypes:true})) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isSymbolicLink()) fail('symlink in owned output');
      if (entry.isDirectory()) {
        if (![...expected].some((name) => name.startsWith(`${relative}/`))) fail('unrelated output directory');
        await walk(path.join(dir, entry.name), relative);
      } else if (!entry.isFile() || !expected.delete(relative)) fail('unrelated output file');
    }
  }
  await walk(root);
  if (expected.size) fail('incomplete owned output');
}
export async function buildPublic(sourceDir = defaultSource, outputDir = defaultOutput) {
  const source = path.resolve(sourceDir), output = path.resolve(outputDir);
  if (source === output || source.startsWith(`${output}${path.sep}`) || output.startsWith(`${source}${path.sep}`)) fail('source and output overlap');
  await noSymlinks(source); if (!(await lstat(source)).isDirectory()) fail('source must be a directory');
  await noSymlinks(output, true);
  let existing = false;
  try { await lstat(output); existing = true; } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (existing) await ownedOutput(output);
  const manifest = await json(source, 'manifest.json'); manifestData(manifest);
  const version = manifest.data_version, prefix = `versions/${version}`;
  const [catalog, issues, documents, indexText] = await Promise.all([
    json(source, manifest.files.catalog, version), json(source, manifest.files.issues, version),
    json(source, manifest.files.search_documents, version), file(source, manifest.files.search_index, version).then((p) => readFile(p, 'utf8')),
  ]);
  if (!object(catalog) || !Array.isArray(catalog.items) || !Array.isArray(catalog.occurrences) || !Array.isArray(issues) || !Array.isArray(documents) || documents.length !== manifest.counts.occurrences) fail('source shape');
  const details = Object.create(null), paths = new Set(), reserved = new Set(Object.values(manifest.files).filter((v) => typeof v === 'string'));
  const itemPaths = new Map(catalog.items.map((item) => [item.id, item.detail_path]));
  for (const item of catalog.items) { const p = safePath(item.detail_path, version); if (!p.endsWith('.json') || reserved.has(p)) fail('detail path'); paths.add(p); }
  for (const relative of paths) {
    const shard = await json(source, relative, version);
    if (!object(shard)) fail('detail shard');
    for (const [id, detail] of Object.entries(shard)) {
      if (Object.hasOwn(details, id) || detail?.id !== id || itemPaths.get(id) !== relative) fail('detail shard membership');
      details[id] = detail;
    }
  }
  const encoded = encodePartitionedFacts(catalog, issues, details, manifest);
  const ids = new Set(catalog.occurrences.map((row) => row.id));
  if (ids.size !== catalog.occurrences.length || documents.some((doc) => !object(doc) || typeof doc.id !== 'string' || !ids.delete(doc.id) || typeof doc.title !== 'string' || typeof doc.text !== 'string') || ids.size) fail('search document identities');
  const built = new MiniSearch(searchOptions); built.addAll(documents);
  // The index must represent exactly these documents, including full text, not merely matching IDs.
  if (JSON.stringify(JSON.parse(indexText)) !== JSON.stringify(built)) fail('search index does not match source documents or format');
  const assets = new Map();
  function asset(relative, kind) {
    safePath(relative, version);
    const match = /^([a-f0-9]{64})\.(png|jpe?g|webp|gif|pdf)$/.exec(path.basename(relative));
    if (!match || (kind === 'pdf') !== (match[2] === 'pdf') || assets.has(relative) && assets.get(relative).kind !== kind || reserved.has(relative) || paths.has(relative)) fail('invalid asset path');
    assets.set(relative, {kind, hash:match[1]});
  }
  for (const issue of issues) for (const doc of issue.documents) if (doc.pdf_path) asset(doc.pdf_path, 'pdf');
  for (const detail of Object.values(details)) for (const row of detail.occurrences) for (const image of [...(row.images ?? []), ...(row.primary_images ?? [])]) asset(image.path, 'image');
  if ([...assets.values()].filter((a) => a.kind === 'pdf').length !== manifest.counts.pdfs || [...assets.values()].filter((a) => a.kind === 'image').length !== manifest.counts.images) fail('asset counts');
  await mkdir(path.dirname(output), {recursive:true}); await noSymlinks(path.dirname(output));
  const stage = `${output}.stage-${randomUUID()}`, backup = `${output}.backup-${randomUUID()}`;
  let moved = false, published = false, bytes = 0, files = 0;
  try {
    await mkdir(stage);
    async function put(relative, data) {
      const dest = path.join(stage, relative); await mkdir(path.dirname(dest), {recursive:true});
      const buffer = Buffer.from(data); await writeFile(dest, buffer, {flag:'wx'}); bytes += buffer.length; files++;
    }
    const wireFiles = {issues:`${prefix}/issues.json`,item_index:`${prefix}/item-index.json`,partitions:Object.create(null),search_index:`${prefix}/search-index.json`};
    await put(wireFiles.issues, JSON.stringify(issues)); await put(wireFiles.item_index, JSON.stringify(encoded.itemIndex)); await put(wireFiles.search_index, indexText);
    for (const [key, partition] of Object.entries(encoded.partitions)) { const relative = `${prefix}/facts/${key}.json`; wireFiles.partitions[key] = relative; await put(relative, JSON.stringify(partition)); }
    for (const [bucket, shard] of Object.entries(encoded.detailShards)) await put(`${prefix}/details/${bucket}.json`, JSON.stringify(shard));
    for (const [relative, info] of assets) {
      const src = await file(source, relative, version); const buffer = await readFile(src);
      if (buffer.length > 64 * 1024 * 1024 || createHash('sha256').update(buffer).digest('hex') !== info.hash) fail('asset hash or size');
      const dest = path.join(stage, relative); await mkdir(path.dirname(dest), {recursive:true});
      try { await link(src, dest); } catch (error) { if (error.code !== 'EXDEV') throw error; await copyFile(src, dest); }
      bytes += buffer.length; files++;
    }
    const outputManifest = {schema_version:3,generator:'nannanews-reader-v3',data_version:version};
    for (const key of ['content_rules_version','public_facts_only','generated_at']) if (Object.hasOwn(manifest, key)) outputManifest[key] = manifest[key];
    outputManifest.counts = manifest.counts; outputManifest.files = wireFiles;
    await put('manifest.json', JSON.stringify(outputManifest));
    if (existing) { await ownedOutput(output); await rename(output, backup); moved = true; }
    await rename(stage, output); published = true;
    if (moved) await rm(backup, {recursive:true});
    return {data_version:version, counts:manifest.counts, files, bytes, output};
  } finally {
    if (!published && moved) await rename(backup, output);
    await rm(stage, {recursive:true, force:true});
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length > 4) { console.error('Usage: node build-public.mjs [source-dir] [output-dir]'); process.exitCode = 1; }
  else try { console.log(JSON.stringify(await buildPublic(process.argv[2], process.argv[3]))); }
  catch (error) { console.error(`Public build failed: ${error.message}`); process.exitCode = 1; }
}
