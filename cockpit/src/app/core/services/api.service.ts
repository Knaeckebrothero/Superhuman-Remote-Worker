import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, catchError, of } from 'rxjs';
import { TableInfo, TableDataResponse, ColumnDef } from '../models/api.model';

/**
 * HTTP client service for the cockpit API.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = 'http://localhost:8080/api';

  /**
   * Get list of available tables with row counts.
   */
  getTables(): Observable<TableInfo[]> {
    return this.http.get<TableInfo[]>(`${this.baseUrl}/tables`).pipe(
      catchError((error) => {
        console.error('Failed to fetch tables:', error);
        return of([]);
      }),
    );
  }

  /**
   * Get paginated data from a table.
   */
  getTableData(
    tableName: string,
    page: number = 1,
    pageSize: number = 50,
  ): Observable<TableDataResponse> {
    const params = new HttpParams()
      .set('page', page.toString())
      .set('pageSize', pageSize.toString());

    return this.http
      .get<TableDataResponse>(`${this.baseUrl}/tables/${tableName}`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch data for table ${tableName}:`, error);
          return of({
            columns: [],
            rows: [],
            total: 0,
            page: 1,
            pageSize: 50,
          });
        }),
      );
  }

  /**
   * Get column definitions for a table.
   */
  getTableSchema(tableName: string): Observable<ColumnDef[]> {
    return this.http
      .get<ColumnDef[]>(`${this.baseUrl}/tables/${tableName}/schema`)
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch schema for table ${tableName}:`, error);
          return of([]);
        }),
      );
  }
}
