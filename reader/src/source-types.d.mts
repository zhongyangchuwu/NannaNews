export const SOURCE_TYPES: readonly ['官网', '微信公众号', '其他'];
export type SourceType = (typeof SOURCE_TYPES)[number];
export function sourceTypeForHost(host: string): SourceType;
export function sourceTypesForHosts(hosts: readonly string[]): SourceType[];
