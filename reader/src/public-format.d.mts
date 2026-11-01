import type { Catalog, CatalogOccurrence, PublicIssue, PublicItemDetail, Manifest } from './types';

export interface WireMessage { id: string; aliases: string[]; latest_occurrence_id: string; occurrence_count: number; fields: Record<string, unknown[]> }
export interface WirePartition { messages: WireMessage[]; owners: Record<string, string>; occurrences: WireOccurrence[] }
export interface WireOccurrence { id: string; item_id: string; document_id: string | null; kind: CatalogOccurrence['kind']; order: number; changes?: Record<string, number> }
/** detail_path, when present, points into versions/<data_version>/reader-v3/details/. */
export interface WireItem { id: string; aliases: string[]; latest_occurrence_id: string; occurrence_count: number; owner: string; occurrences: { id: string; part: string }[]; detail_path?: string }
export interface WireExtras { fields: Record<string, unknown[]>; occurrences: Record<string, Record<string, number>> }
export function encodePartitionedFacts(catalog: Catalog, issues: PublicIssue[], details: Record<string, PublicItemDetail>, manifest: Manifest): { partitions: Record<string, WirePartition>; itemIndex: WireItem[]; detailShards: Record<string, Record<string, WireExtras>> };
export function decodePartition(partition: WirePartition, key: string, owners: Map<string, WireMessage>): CatalogOccurrence[];
export function decodeItemDetail(entry: WireItem, records: CatalogOccurrence[], extras?: WireExtras): PublicItemDetail;
