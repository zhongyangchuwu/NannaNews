import type { ManualReview } from './types';

export function withinTextLimit(value: string, limit: number): boolean;
export function validDay(value: string): boolean;
export function manualReviewStampData(value: unknown, detail?: boolean): value is ManualReview;
