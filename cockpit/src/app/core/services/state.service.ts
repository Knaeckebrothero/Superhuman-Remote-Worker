import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from './api.service';
import {
  TableInfo,
  ColumnDef,
  PaginationState,
} from '../models/api.model';

/**
 * Signals-based state management for the database table viewer.
 */
@Injectable({ providedIn: 'root' })
export class StateService {
  private readonly api = inject(ApiService);

  // Available tables
  readonly tables = signal<TableInfo[]>([]);
  readonly tablesLoading = signal<boolean>(false);

  // Selected table state
  readonly selectedTable = signal<string>('jobs');
  readonly tableData = signal<Record<string, unknown>[]>([]);
  readonly columns = signal<ColumnDef[]>([]);
  readonly isLoading = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  // Pagination state (page=-1 requests last page from API)
  readonly pagination = signal<PaginationState>({
    page: -1,
    pageSize: 50,
    total: 0,
  });

  // Computed values
  readonly totalPages = computed(() => {
    const { pageSize, total } = this.pagination();
    return Math.max(1, Math.ceil(total / pageSize));
  });

  readonly hasNextPage = computed(() => {
    return this.pagination().page < this.totalPages();
  });

  readonly hasPreviousPage = computed(() => {
    return this.pagination().page > 1;
  });

  readonly pageRange = computed(() => {
    const { page, pageSize, total } = this.pagination();
    const start = Math.min((page - 1) * pageSize + 1, total);
    const end = Math.min(page * pageSize, total);
    return { start, end, total };
  });

  /**
   * Load list of available tables.
   */
  loadTables(): void {
    this.tablesLoading.set(true);
    this.api.getTables().subscribe({
      next: (tables) => {
        this.tables.set(tables);
        this.tablesLoading.set(false);
      },
      error: (err) => {
        console.error('Failed to load tables:', err);
        this.tablesLoading.set(false);
      },
    });
  }

  /**
   * Select a table and load its data (starting from the last page).
   */
  selectTable(tableName: string): void {
    if (tableName === this.selectedTable() && this.tableData().length > 0) {
      return; // Already loaded
    }

    this.selectedTable.set(tableName);
    this.pagination.update((p) => ({ ...p, page: -1 })); // Request last page
    this.loadTableData();
  }

  /**
   * Load data for the currently selected table.
   */
  loadTableData(): void {
    const table = this.selectedTable();
    const { page, pageSize } = this.pagination();

    this.isLoading.set(true);
    this.error.set(null);

    this.api.getTableData(table, page, pageSize).subscribe({
      next: (response) => {
        this.columns.set(response.columns);
        this.tableData.set(response.rows);
        this.pagination.update((p) => ({
          ...p,
          total: response.total,
          page: response.page,
          pageSize: response.pageSize,
        }));
        this.isLoading.set(false);
      },
      error: (err) => {
        console.error('Failed to load table data:', err);
        this.error.set('Failed to load table data');
        this.tableData.set([]);
        this.columns.set([]);
        this.isLoading.set(false);
      },
    });
  }

  /**
   * Go to a specific page.
   */
  setPage(page: number): void {
    if (page < 1 || page > this.totalPages()) {
      return;
    }
    this.pagination.update((p) => ({ ...p, page }));
    this.loadTableData();
  }

  /**
   * Go to next page.
   */
  nextPage(): void {
    if (this.hasNextPage()) {
      this.setPage(this.pagination().page + 1);
    }
  }

  /**
   * Go to previous page.
   */
  previousPage(): void {
    if (this.hasPreviousPage()) {
      this.setPage(this.pagination().page - 1);
    }
  }

  /**
   * Go to first page.
   */
  firstPage(): void {
    this.setPage(1);
  }

  /**
   * Go to last page.
   */
  lastPage(): void {
    this.setPage(this.totalPages());
  }

  /**
   * Refresh current table data.
   */
  refresh(): void {
    this.loadTableData();
  }
}
