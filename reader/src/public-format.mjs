// Pure v3 wire codec. Only public catalog fields are eligible for fact pools.
const FACTS = ['title','category','preview','issue_date','source_urls','source_hosts','colleges','dates','activity_id','source_id','expiry_date','image_count','source_corrections','content_status','activity_facts','manual_review'];
const EXTRAS = ['body_text','date_review','original_dates','college_mentions','images','primary_images','locator','table_cells'];
const ROUTING = ['id','item_id','issue_id','document_id','kind'];
const ITEM_ID = /^item-[a-f0-9]{24}$/;
const OCC_ID = /^occ-[a-f0-9]{24}$/;
const ISSUE_ID = /^issue-[a-f0-9]{24}$/;
const DOCUMENT_ID = /^[a-f0-9]{64}$/;
const validId = (pattern, value) => typeof value === 'string' && pattern.test(value);
const validPart = (value) => value === 'standalone' || validId(ISSUE_ID, value);
const has = (v, k) => Object.hasOwn(v, k);
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const fail = (message) => { throw new Error(`Invalid public data: ${message}`); };
const keyOf = (value) => JSON.stringify(value, (_, v) => object(v) ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, v[k]])) : v);
const equal = (a, b) => keyOf(a) === keyOf(b);
const partOf = (row) => row.issue_id === null ? 'standalone' : row.issue_id;
const nonempty = (v) => v !== null && v !== undefined && v !== '' && (!Array.isArray(v) || v.length > 0) && (!object(v) || Object.keys(v).length > 0);
const pick = (value, keys) => Object.fromEntries(keys.filter((k) => has(value, k)).map((k) => [k, value[k]]));
function minimalDate(date) {
  if (!object(date)) fail('date');
  return pick(date, ['kind','normalized_date','resolution','literal_source']);
}
function factValue(key, value) {
  if (key === 'dates') {
    if (!Array.isArray(value)) fail('dates');
    const dates = value.map(minimalDate);
    if (!equal(value, dates)) fail('catalog date contains non-catalog fields');
    return dates;
  }
  if (key === 'manual_review') {
    if (!object(value)) fail('manual review');
    const stamp = pick(value, ['revision','status','reviewed_at','reason']);
    if (!equal(value, stamp)) fail('catalog manual review contains non-stamp fields');
    return stamp;
  }
  if (key === 'activity_facts' && (!object(value) || Object.keys(value).some((k) => !['venue','audience','participation','time'].includes(k)))) fail('catalog activity facts');
  if (key === 'source_corrections' && (!Array.isArray(value) || value.some((v) => !object(v) || Object.keys(v).some((k) => !['original_url','corrected_url','reason','title','sha256','fetched_at'].includes(k))))) fail('catalog source corrections');
  return value;
}
function pool(fields, key, value) {
  const values = fields[key] ??= [];
  const index = values.findIndex((entry) => equal(entry, value));
  if (index !== -1) return index;
  values.push(value);
  return values.length - 1;
}
function detailExtras(row, detail) {
  const extra = {};
  const overrides = {};
  const absent = [];
  for (const key of FACTS) {
    if (key === 'dates') continue;
    let value = detail[key];
    if (key === 'manual_review' && object(value)) {
      if (has(value, 'activity')) extra.manual_activity = value.activity;
      value = pick(value, ['revision','status','reviewed_at','reason']);
    }
    if (!has(detail, key)) { if (has(row, key)) absent.push(key); }
    else if (!has(row, key) || !equal(value, row[key])) overrides[key] = value;
  }
  // An omitted original_dates falls back to dates in ItemDetail; [] does not.
  if (!has(detail, 'original_dates')) absent.push('original_dates');
  if (Object.keys(overrides).length) extra.fact_overrides = overrides;
  if (absent.length) extra.absent_fields = absent;
  if (!Array.isArray(detail.dates)) fail('detail dates');
  const minimal = detail.dates.map(minimalDate);
  if (!equal(minimal, row.dates)) extra.dates = detail.dates;
  else {
    const additions = detail.dates.map((date) => {
      const result = {};
      if (date.raw_text !== (date.normalized_date ?? '')) result.raw_text = date.raw_text;
      if (date.evidence !== '') result.evidence = date.evidence;
      return result;
    });
    if (additions.some((entry) => Object.keys(entry).length)) extra.date_details = additions;
  }
  for (const key of EXTRAS) if (has(detail, key) && nonempty(detail[key])) extra[key] = detail[key];
  return extra;
}
export function encodePartitionedFacts(catalog, issues, details, manifest) {
  if (!object(catalog) || !Array.isArray(catalog.items) || !Array.isArray(catalog.occurrences) || !Array.isArray(issues) || !object(details) || !object(manifest?.counts)) fail('root shape');
  const counts = manifest.counts;
  if (catalog.items.length !== counts.items || catalog.occurrences.length !== counts.occurrences || issues.length !== counts.issues) fail('counts');
  const partitions = Object.create(null), issueMap = new Map(), docs = new Map(), items = new Map(), aliases = new Set(), rows = new Map();
  for (const issue of issues) {
    if (!object(issue) || !validId(ISSUE_ID, issue.id) || issueMap.has(issue.id) || !Array.isArray(issue.documents) || !Number.isSafeInteger(issue.occurrence_count) || issue.occurrence_count < 0) fail('issue');
    issueMap.set(issue.id, { issue, count: 0 });
    partitions[issue.id] = { messages: [], owners: Object.create(null), occurrences: [] };
    for (const doc of issue.documents) { if (!object(doc) || !validId(DOCUMENT_ID, doc.id) || docs.has(doc.id)) fail('document'); docs.set(doc.id, issue.id); }
  }
  if (docs.size !== counts.documents) fail('document count');
  for (const item of catalog.items) {
    if (!object(item) || !validId(ITEM_ID, item.id) || items.has(item.id) || !Array.isArray(item.aliases) || !validId(OCC_ID, item.latest_occurrence_id) || !Number.isSafeInteger(item.occurrence_count) || item.occurrence_count < 1) fail('item');
    items.set(item.id, { item, rows: [] });
  }
  for (const {item} of items.values()) for (const alias of item.aliases) {
    if (!validId(ITEM_ID, alias) || items.has(alias) || aliases.has(alias)) fail('alias'); aliases.add(alias);
  }
  for (const [order, row] of catalog.occurrences.entries()) {
    if (!object(row) || !validId(OCC_ID, row.id) || !validId(ITEM_ID, row.item_id) || rows.has(row.id) || !items.has(row.item_id)) fail('occurrence identity');
    if (row.kind === 'standalone') {
      if (row.issue_id !== null || row.document_id !== null || row.issue_date !== null || row.activity_id !== row.item_id || !/^source-[a-f0-9]{24}$/.test(row.source_id)) fail('standalone references');
      partitions.standalone ??= { messages: [], owners: Object.create(null), occurrences: [] };
    } else {
      if (!['summary','detail','notice'].includes(row.kind) || !issueMap.has(row.issue_id) || docs.get(row.document_id) !== row.issue_id) fail('issue/document reference');
      issueMap.get(row.issue_id).count++;
    }
    if (row.source_id !== undefined && !/^source-[a-f0-9]{24}$/.test(row.source_id)) fail('source reference');
    if (row.activity_id !== undefined && row.activity_id !== row.item_id) fail('activity reference');
    for (const key of FACTS) if (has(row, key)) factValue(key, row[key]);
    rows.set(row.id, { row, order }); items.get(row.item_id).rows.push(row);
  }
  for (const {issue, count} of issueMap.values()) if (issue.occurrence_count !== count) fail('issue count');
  if (Object.keys(details).length !== items.size) fail('detail count');
  const itemIndex = [], detailShards = Object.create(null);
  for (const {item, rows: itemRows} of items.values()) {
    const latest = rows.get(item.latest_occurrence_id)?.row;
    if (itemRows.length !== item.occurrence_count || !latest || latest.item_id !== item.id) fail('item count/latest');
    const detail = details[item.id];
    if (!object(detail) || detail.id !== item.id || detail.latest_occurrence_id !== item.latest_occurrence_id || !Array.isArray(detail.occurrences) || detail.occurrences.length !== itemRows.length) fail('detail identity');
    const owner = partOf(latest), fields = Object.create(null), entity = {id:item.id, aliases:item.aliases, latest_occurrence_id:item.latest_occurrence_id, occurrence_count:item.occurrence_count, fields};
    for (const row of [latest, ...itemRows.filter((r) => r !== latest)]) for (const key of FACTS) if (has(row, key)) pool(fields, key, row[key]);
    partitions[owner].messages.push(entity);
    const entry = {id:item.id, aliases:item.aliases, latest_occurrence_id:item.latest_occurrence_id, occurrence_count:item.occurrence_count, owner, occurrences:[]};
    const seen = new Set(), extras = {fields:Object.create(null), occurrences:Object.create(null)};
    for (const occurrence of detail.occurrences) {
      const source = rows.get(occurrence?.id)?.row;
      if (!source || source.item_id !== item.id || seen.has(source.id) || ROUTING.some((key) => !equal(source[key], occurrence[key]))) fail('detail occurrence membership');
      seen.add(source.id); entry.occurrences.push({id:source.id, part:partOf(source)});
      const values = detailExtras(source, occurrence);
      if (Object.keys(values).length) {
        const refs = Object.create(null);
        for (const [key, value] of Object.entries(values)) refs[key] = pool(extras.fields, key, value);
        extras.occurrences[source.id] = refs;
      }
    }
    if (seen.size !== itemRows.length) fail('detail membership');
    if (Object.keys(extras.occurrences).length) {
      const bucket = item.id.slice(5, 7);
      (detailShards[bucket] ??= Object.create(null))[item.id] = extras;
      entry.detail_path = `versions/${manifest.data_version}/reader-v3/details/${bucket}.json`;
    }
    itemIndex.push(entry);
    for (const row of itemRows) {
      const part = partitions[partOf(row)]; part.owners[item.id] = owner;
      const changes = Object.create(null);
      for (const key of Object.keys(fields)) {
        const index = has(row, key) ? fields[key].findIndex((value) => equal(value, row[key])) : -1;
        if (index !== 0) changes[key] = index;
      }
      const record = {id:row.id,item_id:row.item_id,document_id:row.document_id,kind:row.kind,order:rows.get(row.id).order};
      if (Object.keys(changes).length) record.changes = changes;
      part.occurrences.push(record);
    }
  }
  for (const part of Object.values(partitions)) part.occurrences.sort((a,b) => a.order-b.order);
  return {partitions, itemIndex, detailShards};
}
export function decodePartition(partition, key, owners) {
  if (!object(partition) || !Array.isArray(partition.messages) || !object(partition.owners) || !Array.isArray(partition.occurrences) || !validPart(key) || !(owners instanceof Map)) fail('partition');
  for (const [id, owner] of Object.entries(partition.owners)) if (!validId(ITEM_ID, id) || !validPart(owner)) fail('partition owner');
  const validated = new Set();
  const validateEntity = (entity) => {
    if (validated.has(entity)) return;
    if (!object(entity) || !validId(ITEM_ID, entity.id) || !object(entity.fields) || !Array.isArray(entity.aliases) || entity.aliases.some((a) => !validId(ITEM_ID, a)) || !validId(OCC_ID, entity.latest_occurrence_id) || !Number.isSafeInteger(entity.occurrence_count) || entity.occurrence_count < 1) fail('owner entity');
    for (const [field, values] of Object.entries(entity.fields)) {
      if (!FACTS.includes(field) || !Array.isArray(values) || !values.length) fail('field pool');
      for (const value of values) factValue(field, value);
    }
    validated.add(entity);
  };
  const messageIds = new Set();
  for (const entity of partition.messages) {
    validateEntity(entity);
    if (messageIds.has(entity.id)) fail('duplicate owner entity');
    messageIds.add(entity.id);
  }
  const ids = new Set(), orders = new Set();
  return partition.occurrences.map((record) => {
    if (!object(record) || !validId(OCC_ID, record.id) || ids.has(record.id) || !validId(ITEM_ID, record.item_id) || !has(partition.owners, record.item_id) || !validPart(partition.owners[record.item_id]) || !Number.isSafeInteger(record.order) || record.order < 0 || orders.has(record.order) || (key === 'standalone' ? record.kind !== 'standalone' || record.document_id !== null : !['summary','detail','notice'].includes(record.kind) || !validId(DOCUMENT_ID, record.document_id))) fail('partition row');
    ids.add(record.id); orders.add(record.order);
    const entity = owners.get(record.item_id);
    validateEntity(entity);
    if (entity.id !== record.item_id) fail('owner entity');
    const changes = record.changes ?? {};
    if (!object(changes) || Object.keys(changes).some((field) => !has(entity.fields, field))) fail('changes');
    const row = {id:record.id,item_id:record.item_id,issue_id:key === 'standalone' ? null : key,document_id:record.document_id,kind:record.kind};
    for (const [field, values] of Object.entries(entity.fields)) {
      if (!FACTS.includes(field) || !Array.isArray(values) || values.length === 0) fail('field pool');
      const index = has(changes, field) ? changes[field] : 0;
      if (!Number.isSafeInteger(index) || index < -1 || index >= values.length) fail('field index');
      if (index !== -1) row[field] = values[index];
    }
    return row;
  });
}
export function decodeItemDetail(entry, records, extras) {
  if (!object(entry) || !Array.isArray(entry.occurrences) || !Array.isArray(records) || records.length !== entry.occurrences.length || (extras !== undefined && (!object(extras) || !object(extras.fields) || !object(extras.occurrences)))) fail('detail wire');
  if (!validId(ITEM_ID, entry.id) || !validId(OCC_ID, entry.latest_occurrence_id) || !validPart(entry.owner) || !Array.isArray(entry.aliases) || entry.aliases.some((alias) => !validId(ITEM_ID, alias)) || entry.occurrences.some((ref) => !object(ref) || !validId(OCC_ID, ref.id) || !validPart(ref.part))) fail('detail identity');
  if (extras !== undefined) {
    const allowed = [...EXTRAS,'dates','date_details','fact_overrides','absent_fields','manual_activity'];
    const occurrenceIds = new Set(entry.occurrences.map((entry) => entry.id));
    for (const [key, values] of Object.entries(extras.fields)) if (!allowed.includes(key) || !Array.isArray(values) || !values.length) fail('detail pool');
    for (const [id, refs] of Object.entries(extras.occurrences)) {
      if (!occurrenceIds.has(id) || !object(refs)) fail('detail occurrence refs');
      for (const [key, index] of Object.entries(refs)) if (!has(extras.fields, key) || !Number.isSafeInteger(index) || index < 0 || index >= extras.fields[key].length) fail('detail pool index');
    }
  }
  const occurrences = records.map((record, index) => {
    if (!object(record) || !validId(OCC_ID, record.id) || record.id !== entry.occurrences[index]?.id || record.item_id !== entry.id || partOf(record) !== entry.occurrences[index]?.part || (record.issue_id === null ? record.kind !== 'standalone' || record.document_id !== null : !validId(ISSUE_ID, record.issue_id) || !['summary','detail','notice'].includes(record.kind) || !validId(DOCUMENT_ID, record.document_id))) fail('detail records');
    const values = {};
    const refs = extras?.occurrences[record.id] ?? {};
    if (!object(refs)) fail('detail refs');
    for (const [key, ref] of Object.entries(refs)) {
      if (![...EXTRAS,'dates','date_details','fact_overrides','absent_fields','manual_activity'].includes(key) || !Array.isArray(extras.fields[key]) || !Number.isSafeInteger(ref) || ref < 0 || ref >= extras.fields[key].length) fail('detail pool index');
      values[key] = extras.fields[key][ref];
    }
    const row = {...record, body_text:'', date_review:null, original_dates:[], college_mentions:[], images:[], primary_images:[], locator:{}, table_cells:[]};
    if (values.fact_overrides !== undefined) {
      if (!object(values.fact_overrides) || Object.keys(values.fact_overrides).some((key) => !FACTS.includes(key) || key === 'dates')) fail('detail overrides');
      Object.assign(row, values.fact_overrides);
    }
    if (values.absent_fields !== undefined) {
      if (!Array.isArray(values.absent_fields) || values.absent_fields.some((key) => key !== 'original_dates' && (!FACTS.includes(key) || key === 'dates'))) fail('detail absent fields');
      for (const key of values.absent_fields) delete row[key];
    }
    if (!Array.isArray(row.dates)) fail('detail dates');
    row.dates = values.dates ?? row.dates.map((date, i) => ({...date, raw_text:date.normalized_date ?? '', evidence:'', ...(values.date_details?.[i] ?? {})}));
    for (const key of EXTRAS) if (has(values, key)) row[key] = values[key];
    if (has(values, 'manual_activity')) {
      if (!object(row.manual_review)) fail('manual activity stamp');
      row.manual_review = {...row.manual_review, activity:values.manual_activity};
    }
    return row;
  });
  return {id:entry.id, latest_occurrence_id:entry.latest_occurrence_id, occurrences};
}
