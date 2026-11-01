import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

const VERSION = /^[a-f0-9]{64}$/;
const ITEM_ID = /^item-[a-f0-9]{24}$/;
const OCCURRENCE_ID = /^occ-[a-f0-9]{24}$/;
const ISSUE_ID = /^issue-[a-f0-9]{24}$/;
const SOURCE_ID = /^source-[a-f0-9]{24}$/;
const STATUSES = new Set(['verified', 'human_verified', 'conflict', 'unconfirmed', 'unreviewed', 'legacy', 'source_corrected', 'needs_review']);
const DATE_RESOLUTIONS = new Set(['explicit', 'contextual', 'year_unknown', 'invalid', 'ambiguous']);
const ACTIVITY_FACT_KEYS = ['audience', 'participation', 'time', 'venue'];
const FACT_KEYS = ['activity_facts', 'activity_id', 'category', 'colleges', 'content_status', 'dates', 'document_id',
  'expiry_date', 'id', 'image_count', 'issue_date', 'issue_id', 'item_id', 'kind', 'manual_review', 'preview',
  'source_hosts', 'source_id', 'source_urls', 'title'];

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function isObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function exactKeys(value, keys) {
  return isObject(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function publicUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.hostname.endsWith('.local')
      && !['localhost', '127.0.0.1', '::1'].includes(url.hostname.toLowerCase());
  } catch {
    return false;
  }
}

function verifyFacts(facts) {
  assert(exactKeys(facts, ['schema_version', 'data_version', 'items', 'issues'])
    && facts.schema_version === 1 && VERSION.test(facts.data_version)
    && Array.isArray(facts.items) && Array.isArray(facts.issues), 'Public activity facts use an unsupported or invalid schema.');
  const { data_version: version, ...body } = facts;
  assert(createHash('sha256').update(stable(body)).digest('hex') === version, 'Public activity facts do not match their content version.');

  const issueById = new Map();
  const documents = new Map();
  for (const issue of facts.issues) {
    assert(isObject(issue) && ISSUE_ID.test(issue.id) && !issueById.has(issue.id)
      && typeof issue.title === 'string' && (issue.issue_date === null || typeof issue.issue_date === 'string')
      && (issue.issue_number === null || Number.isSafeInteger(issue.issue_number))
      && Number.isSafeInteger(issue.occurrence_count) && issue.occurrence_count >= 0
      && Array.isArray(issue.documents), 'Public activity facts contain an invalid issue.');
    issueById.set(issue.id, issue);
    for (const document of issue.documents) {
      assert(exactKeys(document, ['id', 'format', 'filename', 'claimed_issue_date', 'issue_date', 'pdf_path', 'source_urls', 'warnings'])
        && VERSION.test(document.id) && ['html', 'pdf'].includes(document.format)
        && typeof document.filename === 'string' && (document.claimed_issue_date === null || typeof document.claimed_issue_date === 'string')
        && (document.issue_date === null || typeof document.issue_date === 'string') && document.pdf_path === null
        && Array.isArray(document.source_urls) && document.source_urls.every(publicUrl)
        && Array.isArray(document.warnings) && document.warnings.every((warning) => typeof warning === 'string')
        && !documents.has(document.id), 'Public activity facts contain an invalid document reference.');
      documents.set(document.id, issue.id);
    }
  }

  const itemIds = new Set();
  const occurrenceIds = new Set();
  const byIssue = new Map([...issueById.keys()].map((id) => [id, 0]));
  const catalogItems = [];
  const catalogOccurrences = [];
  const detailsByShard = new Map();
  const searchDocuments = [];
  for (const item of facts.items) {
    assert(isObject(item) && ITEM_ID.test(item.id) && !itemIds.has(item.id)
      && OCCURRENCE_ID.test(item.latest_occurrence_id) && Array.isArray(item.aliases)
      && item.aliases.every((alias) => ITEM_ID.test(alias)) && Number.isSafeInteger(item.occurrence_count)
      && item.occurrence_count > 0 && Array.isArray(item.occurrences)
      && item.occurrences.length === item.occurrence_count, 'Public activity facts contain an invalid activity.');
    itemIds.add(item.id);
    const bucket = item.id.slice(5, 7);
    const detailPath = `versions/${version}/items/${bucket}.json`;
    catalogItems.push({ id: item.id, aliases: item.aliases, latest_occurrence_id: item.latest_occurrence_id,
      occurrence_count: item.occurrence_count, detail_path: detailPath });
    const detail = { id: item.id, latest_occurrence_id: item.latest_occurrence_id, occurrences: [] };
    for (const fact of item.occurrences) {
      assert(isObject(fact) && FACT_KEYS.includes('id') && Object.keys(fact).every((key) => FACT_KEYS.includes(key))
        && OCCURRENCE_ID.test(fact.id) && !occurrenceIds.has(fact.id) && fact.item_id === item.id
        && ['summary', 'detail', 'notice', 'standalone'].includes(fact.kind)
        && typeof fact.title === 'string' && typeof fact.preview === 'string'
        && (fact.category === null || typeof fact.category === 'string')
        && (fact.issue_date === null || typeof fact.issue_date === 'string')
        && Array.isArray(fact.source_urls) && fact.source_urls.every(publicUrl)
        && Array.isArray(fact.source_hosts) && fact.source_hosts.every((host) => typeof host === 'string')
        && Array.isArray(fact.colleges) && fact.colleges.every((name) => typeof name === 'string')
        && Array.isArray(fact.dates) && fact.dates.every((date) => isObject(date)
          && Object.keys(date).every((key) => ['kind', 'normalized_date', 'resolution', 'literal_source'].includes(key))
          && typeof date.kind === 'string' && (date.normalized_date === null || /^\d{4}-\d{2}-\d{2}$/.test(date.normalized_date))
          && DATE_RESOLUTIONS.has(date.resolution)
          && (date.literal_source === undefined || ['primary', 'bulletin', 'manual'].includes(date.literal_source)))
        && (fact.expiry_date === null || typeof fact.expiry_date === 'string')
        && Number.isSafeInteger(fact.image_count) && fact.image_count >= 0
        && (fact.content_status === null || STATUSES.has(fact.content_status)), 'Public activity facts contain an invalid occurrence.');
      const standalone = fact.kind === 'standalone';
      assert(standalone
        ? fact.issue_id === null && fact.document_id === null && fact.issue_date === null
          && fact.activity_id === item.id && SOURCE_ID.test(fact.source_id)
        : ISSUE_ID.test(fact.issue_id) && issueById.has(fact.issue_id)
          && VERSION.test(fact.document_id) && documents.get(fact.document_id) === fact.issue_id,
      'Public activity facts contain an invalid issue-linked or standalone reference.');
      assert(!fact.activity_id || fact.activity_id === item.id, 'Public activity facts contain an inconsistent stable activity ID.');
      if (fact.source_id !== undefined) assert(SOURCE_ID.test(fact.source_id), 'Public activity facts contain an invalid stable source ID.');
      if (fact.activity_facts !== undefined) {
        assert(exactKeys(fact.activity_facts, ACTIVITY_FACT_KEYS)
          && ACTIVITY_FACT_KEYS.every((key) => fact.activity_facts[key] === null || typeof fact.activity_facts[key] === 'string'),
        'Public activity facts contain invalid reviewed activity fields.');
      }
      if (fact.manual_review !== undefined) {
        assert(exactKeys(fact.manual_review, ['revision', 'status', 'reviewed_at', 'reason'])
          && VERSION.test(fact.manual_review.revision) && ['verified', 'needs_review'].includes(fact.manual_review.status)
          && typeof fact.manual_review.reviewed_at === 'string' && Number.isFinite(Date.parse(fact.manual_review.reviewed_at))
          && typeof fact.manual_review.reason === 'string' && fact.manual_review.reason.trim().length > 0,
        'Public activity facts contain an invalid review stamp.');
      }
      const row = { ...fact };
      catalogOccurrences.push(row);
      const detailDates = fact.dates.map((date) => ({ kind: date.kind, raw_text: date.normalized_date ?? '',
        normalized_date: date.normalized_date, resolution: date.resolution, evidence: '',
        ...(date.literal_source ? { literal_source: date.literal_source } : {}) }));
      detail.occurrences.push({ ...row, body_text: '', dates: detailDates, original_dates: [],
        date_review: null, college_mentions: [], images: [], primary_images: [], locator: {}, table_cells: [] });
      occurrenceIds.add(fact.id);
      if (fact.issue_id !== null) byIssue.set(fact.issue_id, byIssue.get(fact.issue_id) + 1);
      const text = [fact.title, fact.preview, fact.category, ...Object.values(fact.activity_facts ?? {}),
        ...fact.dates.map((date) => date.normalized_date), ...fact.source_urls].filter(Boolean).join('\n');
      searchDocuments.push({ id: fact.id, title: fact.title, text });
    }
    assert(item.occurrences.some((fact) => fact.id === item.latest_occurrence_id), 'Public activity facts latest occurrence is missing.');
    detailsByShard.set(detailPath, [...(detailsByShard.get(detailPath) ?? []), [item.id, detail]]);
  }
  assert(facts.items.every((item) => item.aliases.every((alias) => !itemIds.has(alias)))
    && facts.items.every((item) => item.aliases.length === new Set(item.aliases).size), 'Public activity facts contain duplicate item aliases.');
  for (const [issueId, count] of byIssue) assert(issueById.get(issueId).occurrence_count === count, 'Public activity facts issue counts do not match.');

  const orderedIssues = facts.issues.map((issue) => ({ ...issue })).sort((left, right) => {
    const dateOrder = (right.issue_date ?? '').localeCompare(left.issue_date ?? '');
    if (dateOrder) return dateOrder;
    if (left.issue_number !== right.issue_number) {
      if (left.issue_number === null) return 1;
      if (right.issue_number === null) return -1;
      return right.issue_number - left.issue_number;
    }
    return left.title.localeCompare(right.title, 'zh-CN');
  });
  return { catalog: { items: catalogItems, occurrences: catalogOccurrences },
    issues: orderedIssues, detailsByShard, searchDocuments,
    counts: { items: catalogItems.length, occurrences: catalogOccurrences.length, issues: orderedIssues.length,
      documents: documents.size, images: 0, pdfs: 0 } };
}

async function writeJson(root, relative, value) {
  const destination = path.join(root, ...relative.split('/'));
  await mkdir(path.dirname(destination), { recursive: true });
  const bytes = Buffer.from(`${stable(value)}\n`, 'utf8');
  await writeFile(destination, bytes, { flag: 'wx', mode: 0o644 });
  return bytes.length;
}

export async function stagePublicFacts(target, factsPath = new URL('../public/activity-facts.json', import.meta.url)) {
  const requestedTarget = path.resolve(target);
  const publicRoot = path.dirname(requestedTarget);
  const publicStat = await lstat(publicRoot);
  assert(publicStat.isDirectory() && !publicStat.isSymbolicLink(), 'Reader public directory must be a real directory.');
  const factsStat = await lstat(factsPath);
  assert(factsStat.isFile() && !factsStat.isSymbolicLink(), 'Tracked public activity facts are required to build a standalone reader.');
  try { await lstat(requestedTarget); throw new Error('reader/public/data already exists; staging never replaces it.'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const facts = JSON.parse(await readFile(factsPath, 'utf8'));
  const { catalog, issues, detailsByShard, searchDocuments, counts } = verifyFacts(facts);
  const staging = `${requestedTarget}.stage-${randomUUID()}`;
  let bytes = 0;
  try {
    await mkdir(staging);
    const prefix = `versions/${facts.data_version}/`;
    const files = {
      'catalog.json': catalog,
      'issues.json': issues,
      'search-documents.json': searchDocuments,
    };
    for (const [relative, value] of Object.entries(files)) bytes += await writeJson(staging, prefix + relative, value);
    for (const [relative, details] of detailsByShard) {
      bytes += await writeJson(staging, prefix + relative.slice(`versions/${facts.data_version}/`.length), Object.fromEntries(details));
    }
    const manifest = {
      schema_version: 2, generator: 'webnews-static-v2', data_version: facts.data_version,
      content_rules_version: 'public-facts-v1', public_facts_only: true,
      counts, files: { catalog: `${prefix}catalog.json`, issues: `${prefix}issues.json`,
        search_documents: `${prefix}search-documents.json`, search_index: `${prefix}search-index.json` },
    };
    bytes += await writeJson(staging, 'manifest.json', manifest);
    await rename(staging, requestedTarget);
    return { data_version: facts.data_version, counts, bytes, files: Object.keys(files).length + detailsByShard.size + 1, output: requestedTarget };
  } catch (error) {
    await rm(staging, { recursive: true, force: true });
    throw error;
  }
}
