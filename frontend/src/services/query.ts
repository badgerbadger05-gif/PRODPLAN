export type IsoDate = string; // YYYY-MM-DD

export interface PlanRangeParams {
  date_from?: IsoDate;
  date_to?: IsoDate;
  item_id?: number;
  production_kind_id?: number;
}

export interface PaginationParams {
  page?: number;
  limit?: number;
}

export interface SortingParams {
  sort_by?: string;
  sort_dir?: 'asc' | 'desc';
}

export interface PlanQueryParams extends PlanRangeParams, PaginationParams, SortingParams {}

export function normalizeDateRange(input: { date_from?: string; date_to?: string }): { date_from?: IsoDate; date_to?: IsoDate } {
  const { date_from, date_to } = input;
  let normalizedFrom: IsoDate | undefined;
  let normalizedTo: IsoDate | undefined;

  if (date_from) {
    const trimmed = date_from.trim();
    if (trimmed.length >= 10) {
      normalizedFrom = trimmed.substring(0, 10);
    }
  }

  if (date_to) {
    const trimmed = date_to.trim();
    if (trimmed.length >= 10) {
      normalizedTo = trimmed.substring(0, 10);
    }
  }

  // Swap if from > to
  if (normalizedFrom && normalizedTo && normalizedFrom > normalizedTo) {
    [normalizedFrom, normalizedTo] = [normalizedTo, normalizedFrom];
  }

  return { date_from: normalizedFrom, date_to: normalizedTo };
}

export function buildPagedQuery(
  base: Record<string, any>,
  paginationAndSort: PaginationParams & SortingParams
): Record<string, any> {
  const result = { ...base };
  const { page, limit, sort_by, sort_dir } = paginationAndSort;

  if (limit !== undefined && limit !== null) {
    result.limit = limit;
  }

  if (page !== undefined && page !== null && limit !== undefined && limit !== null) {
    result.offset = (page - 1) * limit;
  }

  if (sort_by) {
    result.sort_by = sort_by;
  }

  if (sort_dir) {
    result.sort_dir = sort_dir;
  }

  return result;
}

export function buildPlanRangeQuery(params: PlanQueryParams): Record<string, any> {
  const { date_from, date_to, item_id, production_kind_id, page, limit, sort_by, sort_dir } = params;

  // Normalize dates
  const normalizedDates = normalizeDateRange({ date_from, date_to });

  // Build base object
  const base: Record<string, any> = {};

  if (normalizedDates.date_from) {
    base.date_from = normalizedDates.date_from;
  }

  if (normalizedDates.date_to) {
    base.date_to = normalizedDates.date_to;
  }

  if (item_id !== undefined && item_id !== null) {
    base.item_id = item_id;
  }

  if (production_kind_id !== undefined && production_kind_id !== null) {
    base.production_kind_id = production_kind_id;
  }

  // Build pagination and sorting
  const paginationAndSort: PaginationParams & SortingParams = { page, limit, sort_by, sort_dir };
  const result = buildPagedQuery(base, paginationAndSort);

  // Remove undefined/null values
  Object.keys(result).forEach(key => {
    if (result[key] === undefined || result[key] === null || result[key] === '') {
      delete result[key];
    }
  });

  // Explicitly forbid bucket_type
  if ('bucket_type' in result) {
    delete result.bucket_type;
  }

  return result;
}