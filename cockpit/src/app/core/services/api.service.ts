import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, catchError, of } from 'rxjs';
import {
  TableInfo,
  TableDataResponse,
  ColumnDef,
  Agent,
  Job,
  JobCreateRequest,
  JobProgress,
  RequirementsResponse,
  JobStatistics,
  DailyStatistics,
  AgentStatistics,
  StuckJob,
  WorkspaceOverview,
} from '../models/api.model';
import {
  JobSummary,
  AuditEntry,
  AuditResponse,
  AuditFilterCategory,
} from '../models/audit.model';
import { LLMRequest } from '../models/request.model';
import { GraphChangeResponse, GraphDelta } from '../models/graph.model';
import { ChatEntry, ChatHistoryResponse } from '../models/chat.model';
import { environment } from '../environment';

/**
 * Response for bulk audit endpoint.
 */
export interface BulkAuditResponse {
  entries: AuditEntry[];
  total: number;
  offset: number;
  limit: number;
  hasMore: boolean;
}

/**
 * Response for bulk chat endpoint.
 */
export interface BulkChatResponse {
  entries: ChatEntry[];
  total: number;
  offset: number;
  limit: number;
  hasMore: boolean;
}

/**
 * Response for bulk graph changes endpoint.
 */
export interface BulkGraphResponse {
  deltas: GraphDelta[];
  total: number;
  offset: number;
  limit: number;
  hasMore: boolean;
}

/**
 * Job version info for cache invalidation.
 */
export interface JobVersionInfo {
  version: number;
  auditEntryCount: number;
  chatEntryCount: number;
  graphDeltaCount: number;
  lastUpdate: string;
}

/**
 * HTTP client service for the cockpit API.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

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

  /**
   * Get list of jobs with optional status filter.
   */
  getJobs(status?: string, limit: number = 100): Observable<JobSummary[]> {
    let params = new HttpParams().set('limit', limit.toString());
    if (status) {
      params = params.set('status', status);
    }

    return this.http.get<JobSummary[]>(`${this.baseUrl}/jobs`, { params }).pipe(
      catchError((error) => {
        console.error('Failed to fetch jobs:', error);
        return of([]);
      }),
    );
  }

  /**
   * Get paginated audit entries for a job from MongoDB.
   */
  getJobAudit(
    jobId: string,
    page: number = 1,
    pageSize: number = 50,
    filter: AuditFilterCategory = 'all',
  ): Observable<AuditResponse> {
    const params = new HttpParams()
      .set('page', page.toString())
      .set('pageSize', pageSize.toString())
      .set('filter', filter);

    return this.http
      .get<AuditResponse>(`${this.baseUrl}/jobs/${jobId}/audit`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch audit for job ${jobId}:`, error);
          return of({
            entries: [],
            total: 0,
            page: 1,
            pageSize: 50,
            hasMore: false,
            error: error.message || 'Failed to fetch audit data',
          });
        }),
      );
  }

  /**
   * Get a single LLM request by MongoDB document ID.
   */
  getRequest(docId: string): Observable<LLMRequest | null> {
    return this.http.get<LLMRequest>(`${this.baseUrl}/requests/${docId}`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch request ${docId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Get graph changes for a job (Neo4j operations from audit trail).
   */
  getGraphChanges(jobId: string): Observable<GraphChangeResponse> {
    return this.http
      .get<GraphChangeResponse>(`${this.baseUrl}/graph/changes/${jobId}`)
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch graph changes for job ${jobId}:`, error);
          throw error;
        }),
      );
  }

  /**
   * Get the time range (first/last timestamps) for a job's audit entries.
   * @deprecated Use DataService.timeRange() computed signal instead.
   */
  getAuditTimeRange(
    jobId: string,
  ): Observable<{ start: string; end: string } | null> {
    return this.http
      .get<{ start: string; end: string } | null>(
        `${this.baseUrl}/jobs/${jobId}/audit/timerange`,
      )
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch audit time range for job ${jobId}:`, error);
          return of(null);
        }),
      );
  }

  /**
   * Get paginated chat history for a job from MongoDB.
   * Returns a clean sequential view of conversation turns.
   */
  getChatHistory(
    jobId: string,
    page: number = 1,
    pageSize: number = 50,
  ): Observable<ChatHistoryResponse> {
    const params = new HttpParams()
      .set('page', page.toString())
      .set('pageSize', pageSize.toString());

    return this.http
      .get<ChatHistoryResponse>(`${this.baseUrl}/jobs/${jobId}/chat`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch chat history for job ${jobId}:`, error);
          return of({
            entries: [],
            total: 0,
            page: 1,
            pageSize: 50,
            hasMore: false,
            error: error.message || 'Failed to fetch chat history',
          });
        }),
      );
  }

  // ===== Bulk Fetch Endpoints for Caching =====

  /**
   * Get bulk audit entries for caching in IndexedDB.
   * Returns large batches (up to 5000 entries) for efficient caching.
   */
  getJobAuditBulk(
    jobId: string,
    offset: number = 0,
    limit: number = 5000,
  ): Observable<BulkAuditResponse> {
    const params = new HttpParams()
      .set('offset', offset.toString())
      .set('limit', limit.toString());

    return this.http
      .get<BulkAuditResponse>(`${this.baseUrl}/jobs/${jobId}/audit/bulk`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch bulk audit for job ${jobId}:`, error);
          return of({
            entries: [],
            total: 0,
            offset,
            limit,
            hasMore: false,
          });
        }),
      );
  }

  /**
   * Get bulk chat entries for caching in IndexedDB.
   */
  getChatHistoryBulk(
    jobId: string,
    offset: number = 0,
    limit: number = 5000,
  ): Observable<BulkChatResponse> {
    const params = new HttpParams()
      .set('offset', offset.toString())
      .set('limit', limit.toString());

    return this.http
      .get<BulkChatResponse>(`${this.baseUrl}/jobs/${jobId}/chat/bulk`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch bulk chat for job ${jobId}:`, error);
          return of({
            entries: [],
            total: 0,
            offset,
            limit,
            hasMore: false,
          });
        }),
      );
  }

  /**
   * Get bulk graph deltas for caching in IndexedDB.
   */
  getGraphDeltasBulk(
    jobId: string,
    offset: number = 0,
    limit: number = 5000,
  ): Observable<BulkGraphResponse> {
    const params = new HttpParams()
      .set('offset', offset.toString())
      .set('limit', limit.toString());

    return this.http
      .get<BulkGraphResponse>(`${this.baseUrl}/jobs/${jobId}/graph/bulk`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch bulk graph deltas for job ${jobId}:`, error);
          return of({
            deltas: [],
            total: 0,
            offset,
            limit,
            hasMore: false,
          });
        }),
      );
  }

  /**
   * Get job data version for cache invalidation.
   */
  getJobVersion(jobId: string): Observable<JobVersionInfo | null> {
    return this.http.get<JobVersionInfo>(`${this.baseUrl}/jobs/${jobId}/version`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch job version for ${jobId}:`, error);
        return of(null);
      }),
    );
  }

  // ===== Agent Management Endpoints =====

  /**
   * Get list of registered agents.
   */
  getAgents(status?: string, limit: number = 100): Observable<Agent[]> {
    let params = new HttpParams().set('limit', limit.toString());
    if (status) {
      params = params.set('status', status);
    }

    return this.http.get<Agent[]>(`${this.baseUrl}/agents`, { params }).pipe(
      catchError((error) => {
        console.error('Failed to fetch agents:', error);
        return of([]);
      }),
    );
  }

  /**
   * Get a single agent by ID.
   */
  getAgent(agentId: string): Observable<Agent | null> {
    return this.http.get<Agent>(`${this.baseUrl}/agents/${agentId}`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch agent ${agentId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Delete (deregister) an agent.
   */
  deleteAgent(agentId: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/agents/${agentId}`).pipe(
      catchError((error) => {
        console.error(`Failed to delete agent ${agentId}:`, error);
        return of(null);
      }),
    );
  }

  // ===== Job Management Endpoints =====

  /**
   * Create a new job.
   */
  createJob(job: JobCreateRequest): Observable<Job | null> {
    return this.http.post<Job>(`${this.baseUrl}/jobs`, job).pipe(
      catchError((error) => {
        console.error('Failed to create job:', error);
        return of(null);
      }),
    );
  }

  /**
   * Get a single job by ID.
   */
  getJob(jobId: string): Observable<Job | null> {
    return this.http.get<Job>(`${this.baseUrl}/jobs/${jobId}`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch job ${jobId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Delete a job.
   */
  deleteJob(jobId: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/jobs/${jobId}`).pipe(
      catchError((error) => {
        console.error(`Failed to delete job ${jobId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Cancel a running job.
   */
  cancelJob(jobId: string): Observable<{ status: string } | null> {
    return this.http.put<{ status: string }>(`${this.baseUrl}/jobs/${jobId}/cancel`, {}).pipe(
      catchError((error) => {
        console.error(`Failed to cancel job ${jobId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Assign a job to an agent.
   */
  assignJob(jobId: string, agentId: string): Observable<{ status: string; agent_id: string; job_id: string } | null> {
    return this.http
      .post<{ status: string; agent_id: string; job_id: string }>(
        `${this.baseUrl}/jobs/${jobId}/assign/${agentId}`,
        {},
      )
      .pipe(
        catchError((error) => {
          console.error(`Failed to assign job ${jobId} to agent ${agentId}:`, error);
          return of(null);
        }),
      );
  }

  /**
   * Get requirements for a job.
   */
  getJobRequirements(
    jobId: string,
    status?: string,
    limit: number = 100,
    offset: number = 0,
  ): Observable<RequirementsResponse> {
    let params = new HttpParams().set('limit', limit.toString()).set('offset', offset.toString());
    if (status) {
      params = params.set('status', status);
    }

    return this.http
      .get<RequirementsResponse>(`${this.baseUrl}/jobs/${jobId}/requirements`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch requirements for job ${jobId}:`, error);
          return of({ requirements: [], total: 0, limit, offset });
        }),
      );
  }

  /**
   * Get job progress with ETA.
   */
  getJobProgress(jobId: string): Observable<JobProgress | null> {
    return this.http.get<JobProgress>(`${this.baseUrl}/jobs/${jobId}/progress`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch progress for job ${jobId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Get workspace overview for a job.
   */
  getJobWorkspace(jobId: string): Observable<WorkspaceOverview | null> {
    return this.http.get<WorkspaceOverview>(`${this.baseUrl}/jobs/${jobId}/workspace`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch workspace for job ${jobId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Get content of a specific workspace file.
   */
  getWorkspaceFile(jobId: string, filename: string): Observable<{ filename: string; content: string } | null> {
    return this.http
      .get<{ filename: string; content: string }>(`${this.baseUrl}/jobs/${jobId}/workspace/${filename}`)
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch workspace file ${filename} for job ${jobId}:`, error);
          return of(null);
        }),
      );
  }

  // ===== Statistics Endpoints =====

  /**
   * Get overall job statistics.
   */
  getJobStatistics(): Observable<JobStatistics | null> {
    return this.http.get<JobStatistics>(`${this.baseUrl}/stats/jobs`).pipe(
      catchError((error) => {
        console.error('Failed to fetch job statistics:', error);
        return of(null);
      }),
    );
  }

  /**
   * Get daily job statistics.
   */
  getDailyStatistics(days: number = 7): Observable<DailyStatistics[]> {
    const params = new HttpParams().set('days', days.toString());

    return this.http.get<DailyStatistics[]>(`${this.baseUrl}/stats/daily`, { params }).pipe(
      catchError((error) => {
        console.error('Failed to fetch daily statistics:', error);
        return of([]);
      }),
    );
  }

  /**
   * Get agent workforce summary.
   */
  getAgentStatistics(): Observable<AgentStatistics | null> {
    return this.http.get<AgentStatistics>(`${this.baseUrl}/stats/agents`).pipe(
      catchError((error) => {
        console.error('Failed to fetch agent statistics:', error);
        return of(null);
      }),
    );
  }

  /**
   * Get stuck jobs.
   */
  getStuckJobs(thresholdMinutes: number = 60): Observable<StuckJob[]> {
    const params = new HttpParams().set('threshold_minutes', thresholdMinutes.toString());

    return this.http.get<StuckJob[]>(`${this.baseUrl}/stats/stuck`, { params }).pipe(
      catchError((error) => {
        console.error('Failed to fetch stuck jobs:', error);
        return of([]);
      }),
    );
  }
}
