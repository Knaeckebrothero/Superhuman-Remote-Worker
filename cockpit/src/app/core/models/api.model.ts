/**
 * API data models for the Debug Cockpit.
 */

/**
 * Table metadata from the API.
 */
export interface TableInfo {
  name: string;
  rowCount: number;
}

/**
 * Column definition for a database table.
 */
export interface ColumnDef {
  name: string;
  type: 'string' | 'number' | 'boolean' | 'date' | 'json' | 'binary';
  nullable: boolean;
}

/**
 * Paginated table data response.
 */
export interface TableDataResponse {
  columns: ColumnDef[];
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  pageSize: number;
}

/**
 * Pagination state.
 */
export interface PaginationState {
  page: number;
  pageSize: number;
  total: number;
}
