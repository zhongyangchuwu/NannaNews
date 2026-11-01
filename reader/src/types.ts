export type DateResolution = 'explicit' | 'contextual' | 'year_unknown' | 'invalid' | 'ambiguous';
export type DateKind = 'event' | 'deadline' | 'listed' | 'source';
export type ReaderMode = 'all' | 'current' | 'favorites';
export type ReaderView = 'edition' | 'activities' | 'history' | 'favorites' | 'issues' | 'issue' | 'item';
export type ContentStatus = 'verified' | 'human_verified' | 'conflict' | 'unconfirmed' | 'unreviewed' | 'legacy' | 'source_corrected' | 'needs_review';
export interface CatalogDate {
  kind: string;
  normalized_date: string | null;
  resolution: DateResolution;
  literal_source?: 'primary' | 'bulletin' | 'manual';
}
export interface PublicDate extends CatalogDate { raw_text: string; evidence: string }
export interface PublicCollege { name: string; relation: 'mentioned' | 'audience'; evidence: string }
export interface PublicImage {
  id: string; path: string; mime: string;
  width: number | null; height: number | null; page: number | null; caption: string | null;
  source_url?: string;
}
export interface CatalogActivityFacts {
  venue: string | null; audience: string | null; participation: string | null; time: string | null;
}
export interface ManualActivity {
  title: string; summary: string; content: string; category: string | null;
  time: string | null; venue: string | null; organizer: string | null;
  audience: string | null; participation: string | null;
  source_urls: string[];
  dates: { kind: 'event' | 'deadline' | 'registration' | 'application'; date: string }[];
}
export interface ManualReview {
  revision: string; status: 'verified' | 'needs_review'; reviewed_at: string; reason: string;
}
export interface CatalogOccurrence {
  id: string; item_id: string; issue_id: string | null; document_id: string | null;
  kind: 'summary' | 'detail' | 'notice' | 'standalone'; title: string; category: string | null;
  preview: string; issue_date: string | null;
  source_urls: string[]; source_hosts: string[]; colleges: string[]; dates: CatalogDate[];
  activity_id?: string; source_id?: string;
  expiry_date: string | null; image_count: number;
  source_corrections?: SourceCorrection[];
  content_status?: ContentStatus;
  activity_facts?: CatalogActivityFacts;
  manual_review?: ManualReview;
}
export interface ActivityCitation { url: string; text: string; image_index: number | null }
export interface ActivityField { value: string | null; evidence: ActivityCitation[] }
export interface ActivityCollege {
  name: string; relation: 'associated' | 'audience'; evidence: ActivityCitation;
}
export interface ActivityReview {
  title: ActivityField; summary: ActivityField; content: ActivityField; venue: ActivityField;
  organizer: ActivityField; audience: ActivityField; participation: ActivityField; time: ActivityField;
  colleges: ActivityCollege[]; image_indices: number[];
}
export interface SourceReview {
  status: 'source_verified' | 'source_conflict' | 'source_unavailable' | 'source_mismatch' | 'secondary_only';
  reason: string;
  sources: {
    url: string; resolved_url: string; title: string; sha256: string;
    fetched_at: string; published_date: string | null; quotes: string[];
  }[];
  activity?: ActivityReview | null;
}
export interface SourceCorrection {
  original_url: string; corrected_url: string; reason: string;
  title: string; sha256: string; fetched_at: string;
}
export interface PublicOccurrence extends Omit<CatalogOccurrence, 'dates' | 'manual_review'> {
  manual_review?: ManualReview & { activity?: ManualActivity };
  date_review: { status: string; reason: string; rules_version?: string; claims?: PublicDate[]; source_review: SourceReview } | null;
  body_text: string; dates: PublicDate[]; original_dates?: PublicDate[]; college_mentions: PublicCollege[];
  images: PublicImage[]; primary_images?: PublicImage[]; locator: { page?: number; anchor?: string };
  table_cells: { header: string; text: string }[];
}
export interface CatalogItem { id: string; aliases: string[]; latest_occurrence_id: string; occurrence_count: number; detail_path?: string }
export interface PublicItemDetail { id: string; latest_occurrence_id: string; occurrences: PublicOccurrence[] }
export interface Catalog { items: CatalogItem[]; occurrences: CatalogOccurrence[] }
export interface PublicDocument {
  id: string; format: 'html' | 'pdf'; filename: string;
  claimed_issue_date: string | null; issue_date: string | null; pdf_path: string | null;
  source_urls: string[]; warnings: string[];
}
export interface PublicIssue {
  id: string; title: string; issue_date: string | null; issue_number: number | null;
  occurrence_count: number; documents: PublicDocument[];
}
interface ManifestBase {
  data_version: string; generated_at?: string; content_rules_version?: string; public_facts_only?: boolean;
  counts: { items: number; occurrences: number; issues: number; documents: number; images: number; pdfs: number };
}
export type Manifest = ManifestBase & (
  | { schema_version: 2; generator: 'webnews-static-v2'; files: { catalog: string; issues: string; search_documents: string; search_index: string; issue_catalogs?: Record<string, string>; item_index?: string } }
  | { schema_version: 3; generator: 'nannanews-reader-v3'; files: { issues: string; item_index: string; partitions: Record<string, string>; search_index: string } }
);
export interface Filters {
  q: string; category: string[]; college: string[]; source: string[];
  issue_from: string; issue_to: string; date_type: DateKind; date_from: string; date_to: string;
  date_status: 'all' | 'known' | 'unknown'; following: boolean; page: number;
}
export interface QueryResult {
  records: CatalogOccurrence[]; matchingRecords: CatalogOccurrence[];
  total: number; page: number; pages: number; per_page: 20;
}
export interface QueryOptions {
  mode: ReaderMode; issueId?: string; favorites: Set<string>; followedColleges: Set<string>;
  searchHits: string[] | null; asOf?: string;
}
export interface ReaderState {
  status: 'loading' | 'ready' | 'error'; error: string | null;
  manifest: Manifest | null; issues: PublicIssue[]; view: ReaderView; mode: ReaderMode;
  filters: Filters; records: CatalogOccurrence[]; total: number; page: number; pages: number;
  perPage: number; searching: boolean;
  editionIssue: PublicIssue | null; editionRecords: CatalogOccurrence[]; today: string;
  selectedIssue: PublicIssue | null; selectedItem: PublicItemDetail | null;
  selectedOccurrence: PublicOccurrence | null;
  detailStatus: 'idle' | 'loading' | 'ready' | 'error'; detailError: string | null;
  favorites: Set<string>; followedColleges: Set<string>;
  preferenceError: string | null;
  options: { categories: string[]; colleges: string[]; sources: string[] };
  setFilters(patch: Partial<Filters>): void; resetFilters(): void; setPage(page: number): void;
  toggleFavorite(id: string): void; toggleFollowedCollege(name: string): void;
  selectOccurrence(id: string): void; retry(): void;
  itemHref(id: string, occurrenceId?: string): string; issueHref(id: string): string; assetUrl(path: string): string;
  listHref: string;
}
