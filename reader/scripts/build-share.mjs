import { lstat, mkdir, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises';
import { isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { manualReviewStampData } from '../src/review-validation.mjs';

const defaultOutputRoot = fileURLToPath(new URL('../dist/', import.meta.url));
const defaultDataRoot = join(defaultOutputRoot, 'data');
const ITEM_ID = /^item-[a-f0-9]{24}$/;
const OCCURRENCE_ID = /^occ-[a-f0-9]{24}$/;
const ISSUE_ID = /^issue-[a-f0-9]{24}$/;
const SOURCE_ID = /^source-[a-f0-9]{24}$/;
const VERSION = /^[a-f0-9]{64}$/;
const COUNTS = ['items', 'occurrences', 'issues', 'documents', 'images', 'pdfs'];

function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function secureDataFile(root, relative, version) {
  if (typeof relative !== 'string' || isAbsolute(relative) || !/^[A-Za-z0-9._/-]+$/.test(relative)
    || !relative.startsWith(`versions/${version}/`) || !relative.endsWith('.json')) {
    throw new Error('Manifest file paths must be versioned relative JSON paths.');
  }
  const segments = relative.split('/');
  if (segments.some((part) => part === '' || part === '.' || part === '..')) {
    throw new Error('Manifest file path escapes the data root.');
  }
  let path = root;
  for (let index = 0; index < segments.length; index += 1) {
    path = join(path, segments[index]);
    const stat = await lstat(path);
    if (stat.isSymbolicLink() || (index < segments.length - 1 ? !stat.isDirectory() : !stat.isFile())) {
      throw new Error('Manifest paths must not use symlinks or unexpected file types.');
    }
  }
  return path;
}

function visibleText(value, limit = 360) {
  const text = value.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit - 1).trimEnd()}…` : text;
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function displayDate(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

function createPage({ title, description, summary, context, warning, href, linkLabel }) {
  const pageTitle = `${title}｜南哪消息`;
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(pageTitle)}</title>
  <meta name="description" content="${escapeHtml(description)}">
  <meta property="og:type" content="article">
  <meta property="og:title" content="${escapeHtml(pageTitle)}">
  <meta property="og:description" content="${escapeHtml(description)}">
</head>
<body>
  <main>
    <p>南哪消息 · 校园消息网页刊物</p>
    <h1>${escapeHtml(title)}</h1>
    ${context ? `<p>${escapeHtml(context)}</p>` : ''}
    ${warning ? `<p>${escapeHtml(warning)}</p>` : ''}
    <p>${escapeHtml(summary)}</p>
    <p><a id="reader-link" href="${escapeHtml(href)}">${escapeHtml(linkLabel)}</a></p>
    <p>如未自动打开，请点击上方链接继续阅读。</p>
  </main>
  <script>location.replace(document.getElementById('reader-link').href);</script>
</body>
</html>
`;
}

async function ensureOutputDirectory(root, segments, knownDirectories) {
  let path = root;
  for (const segment of segments) {
    path = join(path, segment);
    if (knownDirectories.has(path)) continue;
    try {
      await mkdir(path);
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
    const stat = await lstat(path);
    if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error('Share output directories must be real directories.');
    knownDirectories.add(path);
  }
  return path;
}

async function writePage(root, segments, html, knownDirectories) {
  const directory = await ensureOutputDirectory(root, segments.slice(0, -1), knownDirectories);
  const path = join(directory, segments.at(-1));
  try {
    const stat = await lstat(path);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new Error('Share output pages must be regular files.');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const temporary = join(directory, `.share-${process.pid}-${crypto.randomUUID()}.tmp`);
  try {
    await writeFile(temporary, html, { encoding: 'utf8', flag: 'wx' });
    await rename(temporary, path);
  } finally { await rm(temporary, { force: true }); }
}

export async function buildShare({ dataRoot = defaultDataRoot, outputRoot = defaultOutputRoot, subset = null } = {}) {
  if (subset !== null && (!isObject(subset) || Object.keys(subset).sort().join(',') !== 'issue_ids,item_ids'
    || !Array.isArray(subset.item_ids) || !Array.isArray(subset.issue_ids)
    || subset.item_ids.some((id) => typeof id !== 'string' || !ITEM_ID.test(id))
    || subset.issue_ids.some((id) => typeof id !== 'string' || !ISSUE_ID.test(id)))) {
    throw new Error('Invalid share-page subset.');
  }
  const selectedItems = subset === null ? null : new Set(subset.item_ids);
  const selectedIssues = subset === null ? null : new Set(subset.issue_ids);
  const requestedDataRoot = resolve(dataRoot);
  const dataStat = await lstat(requestedDataRoot);
  if (!dataStat.isDirectory() || dataStat.isSymbolicLink()) throw new Error('Public data root must be a real directory.');
  const data = await realpath(requestedDataRoot);
  const manifestPath = join(data, 'manifest.json');
  const manifestStat = await lstat(manifestPath);
  if (!manifestStat.isFile() || manifestStat.isSymbolicLink()) throw new Error('Public manifest must be a regular file.');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  const factsOnly = isObject(manifest) && manifest.public_facts_only === true;
  if (!isObject(manifest) || manifest.schema_version !== 2 || manifest.generator !== 'webnews-static-v2'
    || typeof manifest.data_version !== 'string' || !VERSION.test(manifest.data_version)
    || manifest.public_facts_only !== undefined && typeof manifest.public_facts_only !== 'boolean'
    || (factsOnly ? manifest.generated_at !== undefined
      : typeof manifest.generated_at !== 'string' || !Number.isFinite(Date.parse(manifest.generated_at)))) {
    throw new Error('Unsupported or invalid public-data manifest.');
  }
  for (const name of COUNTS) {
    if (!Number.isSafeInteger(manifest.counts?.[name]) || manifest.counts[name] < 0) throw new Error('Invalid public-data counts.');
  }

  const [catalogPath, issuesPath] = await Promise.all([
    secureDataFile(data, manifest.files?.catalog, manifest.data_version),
    secureDataFile(data, manifest.files?.issues, manifest.data_version),
  ]);
  const [catalog, issues] = await Promise.all([
    readFile(catalogPath, 'utf8').then(JSON.parse),
    readFile(issuesPath, 'utf8').then(JSON.parse),
  ]);
  if (!isObject(catalog) || !Array.isArray(catalog.items) || catalog.items.length !== manifest.counts.items
    || !Array.isArray(catalog.occurrences) || catalog.occurrences.length !== manifest.counts.occurrences
    || !Array.isArray(issues) || issues.length !== manifest.counts.issues) {
    throw new Error('Public catalog and issue counts do not match the manifest.');
  }

  const itemIds = new Set();
  for (const item of catalog.items) {
    if (!isObject(item) || typeof item.id !== 'string' || !ITEM_ID.test(item.id) || itemIds.has(item.id)) {
      throw new Error('Invalid or duplicate public catalog item ID.');
    }
    itemIds.add(item.id);
  }
  const issueById = new Map();
  const occurrenceCounts = new Map();
  const nonverifiedCounts = new Map();
  for (const issue of issues) {
    if (!isObject(issue) || typeof issue.id !== 'string' || !ISSUE_ID.test(issue.id) || issueById.has(issue.id)
      || typeof issue.title !== 'string' || (issue.issue_date !== null && typeof issue.issue_date !== 'string')
      || !Number.isSafeInteger(issue.occurrence_count) || issue.occurrence_count < 0) {
      throw new Error('Invalid or duplicate public issue.');
    }
    issueById.set(issue.id, issue);
    occurrenceCounts.set(issue.id, 0);
    nonverifiedCounts.set(issue.id, 0);
  }

  if (selectedItems && [...selectedItems].some((id) => !itemIds.has(id))) throw new Error('Unknown share-page item.');
  if (selectedIssues && [...selectedIssues].some((id) => !issueById.has(id))) throw new Error('Unknown share-page issue.');
  const occurrenceIds = new Set();
  const outputStat = await lstat(outputRoot);
  if (!outputStat.isDirectory() || outputStat.isSymbolicLink()) throw new Error('Share output must be a real directory.');
  const output = await realpath(outputRoot);
  const knownDirectories = new Set([output]);
  let writtenOccurrences = 0;
  let writtenIssues = 0;
  for (const occurrence of catalog.occurrences) {
    const standalone = isObject(occurrence) && occurrence.kind === 'standalone';
    const referencesValid = isObject(occurrence) && (standalone
      ? occurrence.issue_id === null && occurrence.document_id === null && occurrence.issue_date === null
        && occurrence.activity_id === occurrence.item_id && SOURCE_ID.test(occurrence.source_id)
      : typeof occurrence?.issue_id === 'string' && issueById.has(occurrence.issue_id)
        && typeof occurrence.document_id === 'string' && VERSION.test(occurrence.document_id));
    if (!isObject(occurrence) || typeof occurrence.id !== 'string' || !OCCURRENCE_ID.test(occurrence.id)
      || occurrenceIds.has(occurrence.id) || typeof occurrence.item_id !== 'string' || !itemIds.has(occurrence.item_id)
      || !['summary', 'detail', 'notice', 'standalone'].includes(occurrence.kind) || !referencesValid
      || typeof occurrence.title !== 'string' || typeof occurrence.preview !== 'string'
      || (occurrence.content_status !== undefined && typeof occurrence.content_status !== 'string')) {
      throw new Error('Invalid, duplicate, or unreferenced public occurrence.');
    }
    occurrenceIds.add(occurrence.id);
    const humanVerified = occurrence.content_status === 'human_verified'
      && manualReviewStampData(occurrence.manual_review) && occurrence.manual_review.status === 'verified';
    const checked = occurrence.content_status === 'verified' || humanVerified;
    if (!standalone) {
      occurrenceCounts.set(occurrence.issue_id, occurrenceCounts.get(occurrence.issue_id) + 1);
      if (!checked) nonverifiedCounts.set(occurrence.issue_id, nonverifiedCounts.get(occurrence.issue_id) + 1);
    }
    if (selectedItems && !selectedItems.has(occurrence.item_id)) continue;

    const issue = standalone ? null : issueById.get(occurrence.issue_id);
    const title = visibleText(occurrence.title, 180) || '校园消息';
    const summary = visibleText(occurrence.preview) || title;
    const issueDate = standalone ? null : displayDate(occurrence.issue_date) || displayDate(issue.issue_date);
    const context = standalone ? '独立收录' : issueDate ? `刊期：${issueDate}` : '刊期未标注';
    const warning = humanVerified ? '此条消息已由人工核实。' : checked
      ? null : '此条消息尚未标记为已核实；请谨慎阅读并以原刊为准。';
    const description = visibleText([warning, summary, context].filter(Boolean).join(' '));
    const spaHref = `../../../../#/items/${occurrence.item_id}?occ=${occurrence.id}`;
    const html = createPage({ title, description, summary, context, warning, href: spaHref, linkLabel: '在阅读器中打开这条消息' });
    await writePage(output, ['share', 'items', occurrence.item_id, occurrence.id, 'index.html'], html, knownDirectories);
    writtenOccurrences += 1;
  }
  if (occurrenceIds.size !== manifest.counts.occurrences) throw new Error('Not all public occurrences were exported.');

  for (const issue of issues) {
    if (selectedIssues && !selectedIssues.has(issue.id)) continue;
    const count = occurrenceCounts.get(issue.id);
    const nonverified = nonverifiedCounts.get(issue.id);
    const issueDate = displayDate(issue.issue_date);
    const context = issueDate ? `刊期：${issueDate}` : '刊期未标注';
    const summary = `${count} 条校园消息。`;
    const warning = nonverified > 0
      ? `本期有 ${nonverified} 条消息尚未标记为已核实；请留意各条消息的核实提示。`
      : null;
    const description = visibleText([context, summary, warning].filter(Boolean).join(' '));
    const title = visibleText(issue.title, 180) || '南哪消息刊物';
    const spaHref = `../../../#/issues/${issue.id}`;
    const html = createPage({ title, description, summary, context, warning, href: spaHref, linkLabel: '在阅读器中打开本期' });
    await writePage(output, ['share', 'issues', issue.id, 'index.html'], html, knownDirectories);
    writtenIssues += 1;
  }

  console.log(`Generated ${writtenOccurrences} message-occurrence and ${writtenIssues} issue share pages.`);
  return { occurrences: writtenOccurrences, issues: writtenIssues };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = {};
    const seen = new Set();
    for (let index = 2; index < process.argv.length; index += 2) {
      const flag = process.argv[index];
      const value = process.argv[index + 1];
      if (!['--data-root', '--output-root', '--subset-file'].includes(flag) || !value || seen.has(flag)) {
        throw new Error('Usage: node build-share.mjs [--data-root DIR] [--output-root DIR] [--subset-file JSON]');
      }
      seen.add(flag);
      if (flag === '--data-root') options.dataRoot = resolve(value);
      else if (flag === '--output-root') options.outputRoot = resolve(value);
      else options.subset = JSON.parse(await readFile(resolve(value), 'utf8'));
    }
    await buildShare(options);
  } catch (error) {
    console.error(`Share page build failed: ${error.message}`);
    process.exitCode = 1;
  }
}
