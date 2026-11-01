import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import MiniSearch from 'minisearch';
import { searchOptions } from '../src/tokenize.mjs';
import { encodePartitionedFacts, decodePartition, decodeItemDetail } from '../src/public-format.mjs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
const run = promisify(execFile);
const builder = fileURLToPath(new URL('../scripts/build-public.mjs', import.meta.url));
const buildPublic = (source: string, output: string) => run(process.execPath, [builder, source, output]);
import type { Catalog, Manifest, PublicIssue, PublicItemDetail, PublicOccurrence } from '../src/types';

const version = 'a'.repeat(64), prefix = `versions/${version}`;
const item = `item-${'1'.repeat(24)}`, other = `item-${'2'.repeat(24)}`;
const ids = [`occ-${'1'.repeat(24)}`, `occ-${'2'.repeat(24)}`, `occ-${'3'.repeat(24)}`];
const issueNew = `issue-${'1'.repeat(24)}`, issueOld = `issue-${'2'.repeat(24)}`, issueEmpty = `issue-${'3'.repeat(24)}`;
const documentNew = '1'.repeat(64), documentOld = '2'.repeat(64), alias = `item-${'3'.repeat(24)}`;
const source = `source-${'a'.repeat(24)}`;
const dates = [{kind:'event', normalized_date:'2026-01-02', resolution:'explicit' as const}];
function fixture() {
  const base = {item_id:item, issue_id:issueNew, document_id:documentNew, kind:'detail' as const, title:'Latest', category:null, preview:'p', issue_date:'2026-01-01', source_urls:['https://example.org'], source_hosts:['example.org'], colleges:[], dates, expiry_date:null, image_count:0};
  const rows = [
    {id:ids[0], ...base, content_status:'verified' as const},
    {id:ids[1], ...base, issue_id:issueOld, document_id:documentOld, title:'Earlier', category:'event', source_urls:['https://old.example.org']},
    {id:ids[2], ...base, item_id:other, issue_id:null, document_id:null, issue_date:null, kind:'standalone' as const, activity_id:other, source_id:source},
  ];
  const catalog: Catalog = {items:[{id:item,aliases:[alias],latest_occurrence_id:ids[0],occurrence_count:2,detail_path:`${prefix}/items/11.json`},{id:other,aliases:[],latest_occurrence_id:ids[2],occurrence_count:1,detail_path:`${prefix}/items/22.json`}], occurrences:rows};
  const issue = (id:string, doc:string, count:number): PublicIssue => ({id,title:id,issue_date:null,issue_number:null,occurrence_count:count,documents:doc ? [{id:doc,format:'html',filename:'source.html',claimed_issue_date:null,issue_date:null,pdf_path:null,source_urls:[],warnings:[]}] : []});
  const issues = [issue(issueNew,documentNew,1),issue(issueOld,documentOld,1),issue(issueEmpty,'',0)];
  const full = (row: typeof rows[number]): PublicOccurrence => ({...row, body_text:'',date_review:null,dates:row.dates.map((date) => ({...date,raw_text:date.normalized_date ?? '',evidence:''})),original_dates:[],college_mentions:[],images:[],primary_images:[],locator:{},table_cells:[]});
  const latest = full(rows[0]); latest.body_text = 'secret keyword in approved body'; latest.title = 'Detail title'; latest.dates[0].evidence = 'public evidence';
  delete latest.content_status;
  const earlier = full(rows[1]); earlier.body_text = latest.body_text; earlier.category = null;
  const details: Record<string,PublicItemDetail> = {[item]:{id:item,latest_occurrence_id:ids[0],occurrences:[earlier,latest]},[other]:{id:other,latest_occurrence_id:ids[2],occurrences:[full(rows[2])]}};
  const manifest: Manifest = {schema_version:2,generator:'webnews-static-v2',data_version:version,generated_at:'2026-01-01T00:00:00Z',counts:{items:2,occurrences:3,issues:3,documents:2,images:0,pdfs:0},files:{catalog:`${prefix}/catalog.json`,issues:`${prefix}/issues.json`,search_documents:`${prefix}/search-documents.json`,search_index:`${prefix}/search-index.json`}};
  return {catalog,issues,details,manifest};
}
function decoded(input = fixture()) {
  const wire = encodePartitionedFacts(input.catalog,input.issues,input.details,input.manifest);
  const owners = new Map(Object.values(wire.partitions).flatMap((p) => p.messages.map((m) => [m.id,m] as const)));
  const records = new Map(Object.entries(wire.partitions).flatMap(([key, part]) => decodePartition(part,key,owners).map((row) => [row.id,row] as const)));
  return {wire, records};
}

describe('partitioned public facts', () => {
  it('round trips catalog facts, global order, detail order and genuine variants without duplicate defaults', () => {
    const input = fixture(); const {wire,records} = decoded(input);
    for (const row of input.catalog.occurrences) expect(records.get(row.id)).toEqual(row);
    expect(wire.partitions[issueNew].messages.map((v) => v.id)).toEqual([item]);
    expect(wire.partitions[issueOld].owners[item]).toBe(issueNew);
    expect(wire.partitions[issueEmpty].occurrences).toEqual([]);
    const orders = Object.values(wire.partitions).flatMap((p) => p.occurrences).sort((a,b) => a.order-b.order).map((r) => r.id);
    expect(orders).toEqual(ids);
    const entry = wire.itemIndex[0], extras = wire.detailShards['11'][item];
    expect(entry.detail_path).toBe(`${prefix}/reader-v3/details/11.json`);
    expect(extras.fields.body_text).toEqual(['secret keyword in approved body']);
    expect(decodeItemDetail(entry, entry.occurrences.map((r) => records.get(r.id)!), extras)).toEqual(input.details[item]);
    expect(wire.itemIndex[1].detail_path).toBeUndefined();
    expect(decodeItemDetail(wire.itemIndex[1], [records.get(ids[2])!])).toEqual(input.details[other]);
  });
  it('preserves absent versus explicitly empty original dates with populated dates', () => {
    const input = fixture();
    const compact = decoded(input);
    expect(compact.wire.itemIndex[1].detail_path).toBeUndefined();
    expect(decodeItemDetail(compact.wire.itemIndex[1], [compact.records.get(ids[2])!]).occurrences[0].original_dates).toEqual([]);
    delete input.details[other].occurrences[0].original_dates;
    const absent = decoded(input), entry = absent.wire.itemIndex[1];
    const detail = decodeItemDetail(entry, [absent.records.get(ids[2])!], absent.wire.detailShards['22'][other]);
    expect(detail.occurrences[0].dates).toHaveLength(1);
    expect(Object.hasOwn(detail.occurrences[0], 'original_dates')).toBe(false);
    expect(detail).toEqual(input.details[other]);
  });
  it('preserves manual activity, date variants and optional absent versus null facts', () => {
    const input = fixture();
    const stamp = {revision:'b'.repeat(64),status:'verified' as const,reviewed_at:'2026-01-01T00:00:00Z',reason:'reviewed'};
    const activity = {title:'Manual',summary:'Summary',content:'Content',category:null,time:null,venue:null,organizer:null,audience:null,participation:null,source_urls:[],dates:[]};
    input.catalog.occurrences[0].manual_review = stamp;
    const detail = input.details[item].occurrences[1];
    detail.manual_review = {...stamp,activity};
    detail.dates = [{kind:'deadline',normalized_date:null,resolution:'ambiguous',raw_text:'soon',evidence:'manual evidence'}];
    detail.category = 'changed';
    const {wire,records} = decoded(input); const entry = wire.itemIndex[0];
    expect(decodeItemDetail(entry, entry.occurrences.map((r) => records.get(r.id)!), wire.detailShards['11'][item])).toEqual(input.details[item]);
  });
  it('encodes an empty source without synthetic standalone data', () => {
    const input = fixture(); input.catalog.items = []; input.catalog.occurrences = []; input.issues = []; input.details = {};
    input.manifest.counts = {items:0,occurrences:0,issues:0,documents:0,images:0,pdfs:0};
    const wire = encodePartitionedFacts(input.catalog,input.issues,input.details,input.manifest);
    expect(Object.keys(wire.partitions)).toEqual([]); expect(wire.itemIndex).toEqual([]); expect(Object.keys(wire.detailShards)).toEqual([]);
  });
  it('rejects invalid source and wire identities rather than producing unreadable data', () => {
    const mutations: ((input: {catalog: Catalog; issues: PublicIssue[]}) => void)[] = [
      (input) => { input.issues[0].id = 'issue-new'; },
      (input) => { input.issues[0].documents[0].id = 'doc-new'; },
      (input) => { input.catalog.items[0].id = 'item-invalid'; },
      (input) => { input.catalog.items[0].aliases = ['alias']; },
      (input) => { input.catalog.items[0].latest_occurrence_id = 'occ-invalid'; },
      (input) => { input.catalog.occurrences[0].id = 'occ-invalid'; },
    ];
    for (const mutate of mutations) {
      const input = fixture(); mutate(input);
      expect(() => encodePartitionedFacts(input.catalog,input.issues,input.details,input.manifest)).toThrow();
    }
    const {wire,records} = decoded(); const part = wire.partitions[issueNew];
    const owners = new Map(part.messages.map((message) => [message.id,message]));
    expect(() => decodePartition(part,'issue-new',owners)).toThrow();
    part.occurrences[0].document_id = 'doc-new';
    expect(() => decodePartition(part,issueNew,owners)).toThrow();
    const entry = wire.itemIndex[0]; entry.aliases = ['alias'];
    expect(() => decodeItemDetail(entry,entry.occurrences.map((ref) => records.get(ref.id)!))).toThrow();
  });
  it('rejects invalid membership, issue counts, and wire indices', () => {
    const input = fixture(); input.issues[0].occurrence_count = 2;
    expect(() => encodePartitionedFacts(input.catalog,input.issues,input.details,input.manifest)).toThrow();
    const {wire} = decoded(); const part = wire.partitions[issueNew];
    const owners = new Map(part.messages.map((m) => [m.id,m]));
    part.occurrences[0].changes = {title:999};
    expect(() => decodePartition(part,issueNew,owners)).toThrow();
    const valid = decoded(); const entry = valid.wire.itemIndex[0];
    const records = entry.occurrences.map((ref) => valid.records.get(ref.id)!);
    expect(() => decodeItemDetail(entry,records,{fields:{body_text:['x']},occurrences:{unknown:{body_text:0}}})).toThrow();
    expect(() => decodeItemDetail(entry,records,{fields:{unknown:['x']},occurrences:{}})).toThrow();
  });
});

async function dataset(root:string) {
  const input = fixture(); const sourceDir = join(root,'source'), outputDir = join(root,'site');
  async function put(relative:string, value:unknown) { const file = join(sourceDir,relative); await mkdir(join(file,'..'),{recursive:true}); await writeFile(file, JSON.stringify(value)); }
  const docs = input.catalog.occurrences.map((r) => ({id:r.id,title:r.title,text:r.id === ids[1] ? 'deep fulltext keyword' : ''}));
  const index = new MiniSearch(searchOptions); index.addAll(docs);
  await put('manifest.json',input.manifest); await put(input.manifest.files.catalog,input.catalog); await put(input.manifest.files.issues,input.issues);
  await put(input.manifest.files.search_documents,docs); await put(input.manifest.files.search_index,index);
  await put(`${prefix}/items/11.json`,{[item]:input.details[item]}); await put(`${prefix}/items/22.json`,{[other]:input.details[other]});
  return {sourceDir,outputDir,input,put};
}
describe('public packer', () => {
  it('publishes only v3 files, preserves actual full-text index and safely replaces owned output', async () => {
    const root = await mkdtemp(join(tmpdir(),'public-pack-'));
    try {
      const {sourceDir,outputDir,input} = await dataset(root); await mkdir(outputDir);
      const sourceIssues = await readFile(join(sourceDir,input.manifest.files.issues));
      await buildPublic(sourceDir,outputDir); await buildPublic(sourceDir,outputDir);
      expect(await readFile(join(sourceDir,input.manifest.files.issues))).toEqual(sourceIssues);
      const manifest = JSON.parse(await readFile(join(outputDir,'manifest.json'),'utf8'));
      expect(manifest.schema_version).toBe(3); expect(manifest.files.catalog).toBeUndefined(); expect(manifest.files.search_documents).toBeUndefined();
      expect(manifest.data_version).toBe(version);
      expect(manifest.files.issues).toBe(`${prefix}/reader-v3/issues.json`);
      expect(manifest.files.item_index).toBe(`${prefix}/reader-v3/item-index.json`);
      expect(manifest.files.search_index).toBe(`${prefix}/reader-v3/search-index.json`);
      expect(manifest.files.partitions[issueNew]).toBe(`${prefix}/reader-v3/facts/${issueNew}.json`);
      const publishedItems = JSON.parse(await readFile(join(outputDir,manifest.files.item_index),'utf8'));
      expect(publishedItems[0].detail_path).toBe(`${prefix}/reader-v3/details/11.json`);
      expect(await readdir(join(outputDir,prefix))).toEqual(['reader-v3']);
      const search = MiniSearch.loadJSON(await readFile(join(outputDir,manifest.files.search_index),'utf8'), searchOptions);
      expect(search.search('fulltext').map((r) => r.id)).toEqual([ids[1]]);
      await writeFile(join(outputDir,'unrelated'),'keep');
      await expect(buildPublic(sourceDir,outputDir)).rejects.toThrow();
      expect(await readFile(join(outputDir,'unrelated'),'utf8')).toBe('keep');
    } finally { await rm(root,{recursive:true,force:true}); }
  });
  it('copies only hash-validated referenced assets and rejects symlinks and stale search', async () => {
    const root = await mkdtemp(join(tmpdir(),'public-pack-'));
    try {
      const {sourceDir,outputDir,input,put} = await dataset(root);
      const content = Buffer.from('public image'); const hash = createHash('sha256').update(content).digest('hex'); const asset = `${prefix}/images/${hash}.png`;
      input.details[item].occurrences[0].images = [{id:'image',path:asset,mime:'image/png',width:null,height:null,page:null,caption:null}];
      input.manifest.counts.images = 1; await put('manifest.json',input.manifest); await put(`${prefix}/items/11.json`,{[item]:input.details[item]});
      await mkdir(join(sourceDir,prefix,'images'),{recursive:true}); await writeFile(join(sourceDir,asset),content);
      await buildPublic(sourceDir,outputDir); expect(await readFile(join(outputDir,asset))).toEqual(content);
      await writeFile(join(sourceDir,input.manifest.files.search_index),'{}');
      await expect(buildPublic(sourceDir,outputDir)).rejects.toThrow();
      const linked = join(root,'link'); await symlink(sourceDir,linked); await expect(buildPublic(linked,join(root,'another'))).rejects.toThrow();
    } finally { await rm(root,{recursive:true,force:true}); }
  });
});
