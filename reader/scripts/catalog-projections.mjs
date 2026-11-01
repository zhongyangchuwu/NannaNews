// Build only the public catalog projection, never detail bodies or evidence.
const occurrenceKeys = ['id', 'item_id', 'issue_id', 'document_id', 'kind', 'title', 'category', 'preview',
  'issue_date', 'source_urls', 'source_hosts', 'colleges', 'activity_id', 'source_id', 'expiry_date',
  'image_count', 'content_status'];

function pick(value, keys) {
  return Object.fromEntries(keys.filter((key) => Object.hasOwn(value, key)).map((key) => [key, value[key]]));
}

function occurrenceProjection(row) {
  const result = pick(row, occurrenceKeys);
  if (!Array.isArray(row.dates)) throw new Error('Catalog occurrence dates are invalid.');
  result.dates = row.dates.map((date) => pick(date, ['kind', 'normalized_date', 'resolution', 'literal_source']));
  if (row.source_corrections !== undefined) {
    if (!Array.isArray(row.source_corrections)) throw new Error('Catalog source corrections are invalid.');
    result.source_corrections = row.source_corrections.map((correction) => pick(correction,
      ['original_url', 'corrected_url', 'reason', 'title', 'sha256', 'fetched_at']));
  }
  if (row.activity_facts !== undefined) result.activity_facts = pick(row.activity_facts, ['venue', 'audience', 'participation', 'time']);
  if (row.manual_review !== undefined) result.manual_review = pick(row.manual_review, ['revision', 'status', 'reviewed_at', 'reason']);
  return result;
}

export function catalogProjections(catalog, issues, version) {
  const prefix = `versions/${version}/`;
  const items = new Map();
  const aliases = new Set();
  for (const item of catalog.items) {
    if (!item || typeof item.id !== 'string' || !item.id || items.has(item.id) || !Array.isArray(item.aliases)
      || typeof item.latest_occurrence_id !== 'string' || !Number.isSafeInteger(item.occurrence_count) || item.occurrence_count < 1) {
      throw new Error('Invalid or duplicate catalog item.');
    }
    items.set(item.id, { item: pick(item, ['id', 'aliases', 'latest_occurrence_id', 'occurrence_count', 'detail_path']), count: 0, latest: false });
  }
  for (const { item } of items.values()) for (const alias of item.aliases) {
    if (typeof alias !== 'string' || !alias || items.has(alias) || aliases.has(alias)) throw new Error('Invalid or duplicate catalog alias.');
    aliases.add(alias);
  }
  const issueRows = new Map();
  const documentIssues = new Map();
  for (const issue of issues) {
    if (!issue || typeof issue.id !== 'string' || !/^[a-zA-Z0-9_-]+$/.test(issue.id) || issueRows.has(issue.id)
      || !Number.isSafeInteger(issue.occurrence_count) || issue.occurrence_count < 0 || !Array.isArray(issue.documents)) {
      throw new Error('Invalid or duplicate issue.');
    }
    issueRows.set(issue.id, { issue, occurrences: [], itemIds: new Set() });
    for (const document of issue.documents) {
      if (!document || typeof document.id !== 'string' || !document.id || documentIssues.has(document.id)) throw new Error('Invalid or duplicate issue document.');
      documentIssues.set(document.id, issue.id);
    }
  }
  const occurrenceIds = new Set();
  for (const row of catalog.occurrences) {
    const entry = row && items.get(row.item_id);
    if (!entry || typeof row.id !== 'string' || !row.id || occurrenceIds.has(row.id)) throw new Error('Invalid catalog occurrence membership.');
    occurrenceIds.add(row.id);
    entry.count++;
    if (row.id === entry.item.latest_occurrence_id) entry.latest = true;
    if (row.kind === 'standalone') {
      if (row.issue_id !== null || row.document_id !== null) throw new Error('Invalid standalone occurrence.');
    } else {
      const issue = issueRows.get(row.issue_id);
      if (!issue || documentIssues.get(row.document_id) !== row.issue_id) throw new Error('Invalid occurrence issue membership.');
      issue.occurrences.push(occurrenceProjection(row));
      issue.itemIds.add(row.item_id);
    }
  }
  for (const { item, count, latest } of items.values()) {
    if (count !== item.occurrence_count || !latest) throw new Error('Catalog item occurrence counts or latest occurrence do not match.');
  }
  const issueCatalogs = Object.create(null);
  const files = Object.create(null);
  for (const [id, row] of issueRows) {
    if (row.occurrences.length !== row.issue.occurrence_count) throw new Error('Catalog issue occurrence counts do not match.');
    const relative = `${prefix}issue-catalogs/${id}.json`;
    issueCatalogs[id] = relative;
    files[relative] = { items: [...row.itemIds].map((itemId) => items.get(itemId).item),
      occurrences: row.occurrences };
  }
  const itemIndex = `${prefix}item-index.json`;
  files[itemIndex] = [...items.values()].map(({ item }) => item);
  return { issueCatalogs, itemIndex, files, occurrenceIds, documentCount: documentIssues.size };
}
