import {inject, Injectable} from '@angular/core';
import {HttpClient, HttpParams} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {ToastService} from './toast.service';
import {
    Agent,
    AgentStatistics,
    ColumnDef,
    DailyStatistics,
    Datasource,
    DatasourceCreateRequest,
    DatasourceTestResult,
    DatasourceUpdateRequest,
    Expert,
    ExpertDetail,
    Job,
    JobCreateRequest,
    JobProgress,
    JobStatistics,
    KnowledgeListResponse,
    KnowledgeNoteDetail,
    KnowledgeSearchResponse,
    KnowledgeSummary,
    MemoryListResponse,
    MemoryStats,
    Project,
    ProjectCreateRequest,
    ProjectDatasource,
    ProjectMember,
    ProjectMemberAddRequest,
    ProjectMemberUpdateRequest,
    ProjectRepository,
    ProjectRepositoryCreateRequest,
    ProjectRepositoryUpdateRequest,
    ProjectUpdateRequest,
    PromoteRequest,
    StuckJob,
    TableDataResponse,
    TableInfo,
    User,
    WorkspaceOverview,
} from '../models/api.model';
import {UploadInfo, UploadResponse} from '../models/file.model';
import {AuditEntry, AuditFilterCategory, AuditResponse, JobSummary,} from '../models/audit.model';
import {LLMRequest} from '../../debug/request.model';
import {GraphChangeResponse, GraphDelta} from '../../debug/graph.model';
import {ChatEntry, ChatHistoryResponse} from '../models/chat.model';
import {PendingActionCounts, ThreadDetail} from '../models/action.model';
import {environment} from '../environment';

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
 * IDE session status from the orchestrator.
 */
export interface IdeSessionStatus {
  status: 'unavailable' | 'available' | 'restoring' | 'active' | 'idle' | 'expired' | 'failed';
  code_server_url?: string | null;
    gitea_url?: string | null;
  snapshot_type?: string;
  estimated_seconds?: number;
  expires_at?: string;
  started_at?: string;
  source?: string;
  restore_type?: 'vm' | 'container';
  error?: string;
}

/**
 * Snapshot storage statistics from the orchestrator.
 */
export interface SnapshotStorageStats {
  available: boolean;
  total_snapshots: number;
  total_size_bytes: number;
  gc_pending_count: number;
  gc_pending_size_bytes: number;
  error?: string;
}

/**
 * HTTP client service for the cockpit API.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly toast = inject(ToastService);
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
   * Get list of jobs with optional status and user filter.
   */
  getJobs(status?: string, limit: number = 100, userId?: string): Observable<JobSummary[]> {
    let params = new HttpParams().set('limit', limit.toString());
    if (status) {
      params = params.set('status', status);
    }
    if (userId) {
      params = params.set('user_id', userId);
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

  // ===== User Endpoints =====

  /**
   * Get list of users.
   */
  getUsers(): Observable<User[]> {
    return this.http.get<User[]>(`${this.baseUrl}/users`).pipe(
      catchError(() => of([])),
    );
  }

  /**
   * Create a new user.
   */
  createUser(body: { display_name: string; avatar_color?: string }): Observable<User | null> {
    return this.http.post<User>(`${this.baseUrl}/users`, body).pipe(
      catchError(() => of(null)),
    );
  }

  /**
   * Update a user.
   */
  updateUser(id: string, body: { display_name?: string; avatar_color?: string }): Observable<{ status: string } | null> {
    return this.http.put<{ status: string }>(`${this.baseUrl}/users/${id}`, body).pipe(
      catchError(() => of(null)),
    );
  }

  /**
   * Delete a user.
   */
  deleteUser(id: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/users/${id}`).pipe(
      catchError(() => of(null)),
    );
  }

  // ===== Expert Discovery Endpoints =====

  /**
   * Get list of available expert configurations.
   */
  getExperts(): Observable<Expert[]> {
    return this.http.get<Expert[]>(`${this.baseUrl}/experts`).pipe(
      catchError(() => of([])),
    );
  }

  /**
   * Get full expert detail including merged config and instructions.
   */
  getExpertDetail(expertId: string): Observable<ExpertDetail | null> {
    return this.http.get<ExpertDetail>(`${this.baseUrl}/experts/${expertId}`).pipe(
      catchError(() => of(null)),
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

  // ===== Datasource Management Endpoints =====

  /**
   * Get list of datasources with optional filters.
   */
  getDatasources(jobId?: string, type?: string): Observable<Datasource[]> {
    let params = new HttpParams();
    if (jobId) {
      params = params.set('job_id', jobId);
    }
    if (type) {
      params = params.set('type', type);
    }

    return this.http.get<Datasource[]>(`${this.baseUrl}/datasources`, { params }).pipe(
      catchError((error) => {
        console.error('Failed to fetch datasources:', error);
        return of([]);
      }),
    );
  }

  /**
   * Get a single datasource by ID.
   */
  getDatasource(id: string): Observable<Datasource | null> {
    return this.http.get<Datasource>(`${this.baseUrl}/datasources/${id}`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch datasource ${id}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Create a new datasource.
   */
  createDatasource(ds: DatasourceCreateRequest): Observable<Datasource | null> {
    return this.http.post<Datasource>(`${this.baseUrl}/datasources`, ds).pipe(
      catchError((error) => {
        console.error('Failed to create datasource:', error);
        return of(null);
      }),
    );
  }

  /**
   * Update a datasource.
   */
  updateDatasource(id: string, ds: DatasourceUpdateRequest): Observable<{ status: string } | null> {
    return this.http.put<{ status: string }>(`${this.baseUrl}/datasources/${id}`, ds).pipe(
      catchError((error) => {
        console.error(`Failed to update datasource ${id}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Delete a datasource.
   */
  deleteDatasource(id: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/datasources/${id}`).pipe(
      catchError((error) => {
        console.error(`Failed to delete datasource ${id}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Test connectivity to a datasource.
   */
  testDatasource(id: string): Observable<DatasourceTestResult | null> {
    return this.http.post<DatasourceTestResult>(`${this.baseUrl}/datasources/${id}/test`, {}).pipe(
      catchError((error) => {
        console.error(`Failed to test datasource ${id}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Get resolved datasources for a job.
   */
  getJobDatasources(jobId: string): Observable<Datasource[]> {
    return this.http.get<Datasource[]>(`${this.baseUrl}/jobs/${jobId}/datasources`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch datasources for job ${jobId}:`, error);
        return of([]);
      }),
    );
  }

  // ===== Project Datasources (N:M) =====

  /**
   * List datasources linked to a project.
   */
  getProjectDatasources(projectId: string): Observable<ProjectDatasource[]> {
    return this.http.get<ProjectDatasource[]>(`${this.baseUrl}/projects/${projectId}/datasources`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch project datasources:`, error);
        return of([]);
      }),
    );
  }

  /**
   * Link a datasource to a project.
   */
  linkProjectDatasource(projectId: string, datasourceId: string): Observable<{ status: string } | null> {
    return this.http.post<{ status: string }>(`${this.baseUrl}/projects/${projectId}/datasources/${datasourceId}`, {}).pipe(
      catchError((error) => {
        console.error(`Failed to link datasource:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Update project-level settings for a linked datasource.
   */
  updateProjectDatasource(
    projectId: string,
    datasourceId: string,
    body: { read_only?: boolean | null; description?: string | null },
  ): Observable<{ status: string } | null> {
    return this.http.patch<{ status: string }>(`${this.baseUrl}/projects/${projectId}/datasources/${datasourceId}`, body).pipe(
      catchError((error) => {
        console.error(`Failed to update project datasource:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Unlink a datasource from a project.
   */
  unlinkProjectDatasource(projectId: string, datasourceId: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/projects/${projectId}/datasources/${datasourceId}`).pipe(
      catchError((error) => {
        console.error(`Failed to unlink datasource:`, error);
        return of(null);
      }),
    );
  }

  // ===== File Upload Endpoints =====

  /**
   * Upload files for job creation.
   * @param files Files to upload
   * @returns Observable with upload response containing upload_id
   */
  uploadFiles(files: File[]): Observable<UploadResponse | null> {
    const formData = new FormData();
    files.forEach((file) => formData.append('files', file, file.name));

    return this.http.post<UploadResponse>(`${this.baseUrl}/uploads`, formData).pipe(
      catchError((error) => {
        console.error('Failed to upload files:', error);
        return of(null);
      }),
    );
  }

  /**
   * Upload a config file (YAML) for job creation.
   * Config files override default agent settings.
   * @param file Single YAML config file
   * @returns Observable with upload response containing upload_id
   */
  uploadConfig(file: File): Observable<UploadResponse | null> {
    const formData = new FormData();
    formData.append('files', file, file.name);

    const params = new HttpParams().set('upload_type', 'config');

    return this.http.post<UploadResponse>(`${this.baseUrl}/uploads`, formData, { params }).pipe(
      catchError((error) => {
        console.error('Failed to upload config file:', error);
        return of(null);
      }),
    );
  }

  /**
   * Upload an instructions file (Markdown/Text) for job creation.
   * Instructions files replace the default agent instructions.
   * @param file Single .md or .txt instructions file
   * @returns Observable with upload response containing upload_id
   */
  uploadInstructions(file: File): Observable<UploadResponse | null> {
    const formData = new FormData();
    formData.append('files', file, file.name);

    const params = new HttpParams().set('upload_type', 'instructions');

    return this.http.post<UploadResponse>(`${this.baseUrl}/uploads`, formData, { params }).pipe(
      catchError((error) => {
        console.error('Failed to upload instructions file:', error);
        return of(null);
      }),
    );
  }

  /**
   * Get information about an upload.
   */
  getUploadInfo(uploadId: string): Observable<UploadInfo | null> {
    return this.http.get<UploadInfo>(`${this.baseUrl}/uploads/${uploadId}`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch upload info for ${uploadId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Delete an upload and all its files.
   */
  deleteUpload(uploadId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/uploads/${uploadId}`).pipe(
      catchError((error) => {
        console.error(`Failed to delete upload ${uploadId}:`, error);
        throw error;
      }),
    );
  }

  // ===== Job Management Endpoints =====

  /**
   * Create a new job.
   */
  createJob(job: JobCreateRequest): Observable<Job | null> {
    return this.http.post<Job>(`${this.baseUrl}/jobs`, job).pipe(
      tap(() => this.toast.success('Job created')),
      catchError((error) => {
        console.error('Failed to create job:', error);
        this.toast.error('Failed to create job');
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
      tap(() => this.toast.success('Job deleted')),
      catchError((error) => {
        console.error(`Failed to delete job ${jobId}:`, error);
        this.toast.error('Failed to delete job');
        return of(null);
      }),
    );
  }

  /**
   * Cancel a running job.
   */
  cancelJob(jobId: string): Observable<{ status: string } | null> {
    return this.http.put<{ status: string }>(`${this.baseUrl}/jobs/${jobId}/cancel`, {}).pipe(
      tap(() => this.toast.success('Job cancelled')),
      catchError((error) => {
        console.error(`Failed to cancel job ${jobId}:`, error);
        this.toast.error('Failed to cancel job');
        return of(null);
      }),
    );
  }

  /**
   * Pause a running job. The agent will cooperatively pause at the next safe point.
   */
  pauseJob(jobId: string): Observable<{ status: string } | null> {
    return this.http.put<{ status: string }>(`${this.baseUrl}/jobs/${jobId}/pause`, {}).pipe(
      tap(() => this.toast.success('Job paused')),
      catchError((error) => {
        console.error(`Failed to pause job ${jobId}:`, error);
        this.toast.error('Failed to pause job');
        return of(null);
      }),
    );
  }

  /**
   * Resume a failed job from its checkpoint.
   * @param jobId The job ID to resume
   * @param feedback Optional feedback to inject before resuming
   * @param agentId Optional agent ID override if original agent is offline
   */
  resumeJob(
    jobId: string,
    feedback?: string,
    agentId?: string,
  ): Observable<{ status: string; job_id: string; agent_id: string } | null> {
    const body: { feedback?: string; agent_id?: string } = {};
    if (feedback) body.feedback = feedback;
    if (agentId) body.agent_id = agentId;

    return this.http
      .post<{ status: string; job_id: string; agent_id: string }>(
        `${this.baseUrl}/jobs/${jobId}/resume`,
        body,
      )
      .pipe(
        tap(() => this.toast.success('Job resumed')),
        catchError((error) => {
          console.error(`Failed to resume job ${jobId}:`, error);
          this.toast.error('Failed to resume job');
          return of(null);
        }),
      );
  }

  /**
   * Get IDE session status for a job.
   */
  getIdeSession(jobId: string): Observable<IdeSessionStatus | null> {
    return this.http
      .get<IdeSessionStatus>(`${this.baseUrl}/jobs/${jobId}/ide`)
      .pipe(
        catchError((error) => {
          console.error(`Failed to get IDE session for ${jobId}:`, error);
          return of(null);
        }),
      );
  }

  /**
   * Ensure the current user has Gitea access to a job's workspace repo.
   * Called before navigating to the workspace URL.
   */
  ensureWorkspaceAccess(jobId: string): Observable<{ granted: boolean } | null> {
    return this.http
      .post<{ granted: boolean }>(`${this.baseUrl}/jobs/${jobId}/ensure-workspace-access`, {})
      .pipe(
        catchError((error) => {
          console.error(`Failed to ensure workspace access for ${jobId}:`, error);
          return of(null);
        }),
      );
  }

  /**
   * Start an IDE session for a job (restores snapshot into a fresh VM).
   */
  startIdeSession(jobId: string): Observable<IdeSessionStatus | null> {
    return this.http
      .post<IdeSessionStatus>(`${this.baseUrl}/jobs/${jobId}/ide`, {})
      .pipe(
        catchError((error) => {
          console.error(`Failed to start IDE session for ${jobId}:`, error);
          this.toast.error('Failed to start IDE session');
          return of(null);
        }),
      );
  }

  /**
   * Stop an active IDE session.
   */
  stopIdeSession(jobId: string): Observable<{ status: string } | null> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/jobs/${jobId}/ide`)
      .pipe(
        tap(() => this.toast.success('IDE session stopped')),
        catchError((error) => {
          console.error(`Failed to stop IDE session for ${jobId}:`, error);
          this.toast.error('Failed to stop IDE session');
          return of(null);
        }),
      );
  }

    /**
     * Get IDE status for a persistent thread's workspace.
     */
    getThreadIdeStatus(threadId: string): Observable<IdeSessionStatus | null> {
        return this.http
            .get<IdeSessionStatus>(`${this.baseUrl}/persistent/threads/${threadId}/ide`)
            .pipe(
                catchError((error) => {
                    console.error(`Failed to get IDE status for thread ${threadId}:`, error);
                    return of(null);
                }),
            );
    }

  /**
   * Get snapshot storage statistics (total count, size, GC pending).
   */
  getSnapshotStats(): Observable<SnapshotStorageStats | null> {
    return this.http
      .get<SnapshotStorageStats>(`${this.baseUrl}/snapshots/stats`)
      .pipe(
        catchError((error) => {
          console.error('Failed to get snapshot stats:', error);
          return of(null);
        }),
      );
  }

  /**
   * Toggle pin on a job's snapshot (GC exemption).
   */
  toggleSnapshotPin(jobId: string): Observable<{ pinned: boolean } | null> {
    return this.http
      .put<{ pinned: boolean }>(`${this.baseUrl}/jobs/${jobId}/snapshot/pin`, {})
      .pipe(
        catchError((error) => {
          console.error(`Failed to toggle snapshot pin for ${jobId}:`, error);
          return of(null);
        }),
      );
  }

  /**
   * Approve a frozen job (pending_review → completed).
   * @param jobId The job ID to approve
   * @param notes Optional reviewer notes
   */
  approveJob(
    jobId: string,
    notes?: string,
  ): Observable<{ status: string; job_id: string; summary: string; deliverables: string[] } | null> {
    const body: { notes?: string } = {};
    if (notes) body.notes = notes;

    return this.http
      .post<{ status: string; job_id: string; summary: string; deliverables: string[] }>(
        `${this.baseUrl}/jobs/${jobId}/approve`,
        body,
      )
      .pipe(
        tap(() => this.toast.success('Job approved')),
        catchError((error) => {
          console.error(`Failed to approve job ${jobId}:`, error);
          this.toast.error('Failed to approve job');
          return of(null);
        }),
      );
  }

  /**
   * Upgrade a frozen job from container workspace to a VM.
   * Called when a job freezes with freeze_type: vm_upgrade_required.
   */
  upgradeJobToVm(
    jobId: string,
  ): Observable<{ status: string; job_id: string; vm_provisioner_mode: string } | null> {
    return this.http
      .post<{ status: string; job_id: string; vm_provisioner_mode: string }>(
        `${this.baseUrl}/jobs/${jobId}/upgrade-to-vm`,
        {},
      )
      .pipe(
        tap(() => this.toast.success('VM upgrade initiated')),
        catchError((error) => {
          console.error(`Failed to upgrade job ${jobId} to VM:`, error);
          this.toast.error('Failed to upgrade to VM');
          return of(null);
        }),
      );
  }

  /**
   * Get frozen job data (job_frozen.json) for a pending_review job.
   */
  getFrozenJobData(jobId: string): Observable<Record<string, unknown> | null> {
    return this.http.get<Record<string, unknown>>(`${this.baseUrl}/jobs/${jobId}/frozen`).pipe(
      catchError((error) => {
        console.error(`Failed to fetch frozen data for job ${jobId}:`, error);
        return of(null);
      }),
    );
  }

  /**
   * Get full thread messages for a job's message thread.
   */
  getThreadMessages(jobId: string, threadId: string): Observable<ThreadDetail | null> {
    return this.http
      .get<ThreadDetail>(`${this.baseUrl}/jobs/${jobId}/messages/${threadId}`)
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch thread ${threadId} for job ${jobId}:`, error);
          return of(null);
        }),
      );
  }

  /**
   * Reply to an agent message thread.
   */
  replyToThread(
    jobId: string,
    threadId: string,
    message: string,
    urgent = false,
  ): Observable<{ status: string; sequence: number } | null> {
    return this.http
      .post<{ status: string; sequence: number }>(
        `${this.baseUrl}/jobs/${jobId}/messages/${threadId}/reply`,
        { message, urgent },
      )
      .pipe(
        tap(() => this.toast.success('Reply sent')),
        catchError((error) => {
          console.error(`Failed to reply to thread ${threadId}:`, error);
          this.toast.error('Failed to send reply');
          return of(null);
        }),
      );
  }

  /**
   * Get pending action counts across all types.
   */
  getPendingActions(): Observable<PendingActionCounts | null> {
    return this.http.get<PendingActionCounts>(`${this.baseUrl}/actions/pending`).pipe(
      catchError((error) => {
        console.error('Failed to fetch pending actions:', error);
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

  /**
   * Write content to a workspace file (requires user approval flow).
   */
  writeWorkspaceFile(
    jobId: string,
    path: string,
    content: string,
    commitMessage?: string,
  ): Observable<{ path: string; size: number; committed: boolean } | null> {
    const body: Record<string, string> = { content };
    if (commitMessage) body['commit_message'] = commitMessage;

    return this.http
      .put<{ path: string; size: number; committed: boolean }>(
        `${this.baseUrl}/jobs/${jobId}/workspace/${path}`,
        body,
      )
      .pipe(
        catchError((error) => {
          console.error(`Failed to write workspace file ${path} for job ${jobId}:`, error);
          return of(null);
        }),
      );
  }

  // ===== Repo Browser Endpoints (Gitea proxy) =====

  /**
   * List directory contents from a job's Gitea repository.
   * @param jobId Job ID
   * @param path Directory path within the repo (empty for root)
   */
  listRepoContents(
    jobId: string,
    path: string = '',
  ): Observable<{ name: string; path: string; type: string; size: number }[]> {
    const params = path ? new HttpParams().set('path', path) : new HttpParams();
    return this.http
      .get<{ name: string; path: string; type: string; size: number }[]>(
        `${this.baseUrl}/jobs/${jobId}/repo/contents`,
        { params },
      )
      .pipe(
        catchError((error) => {
          console.error(`Failed to list repo contents for job ${jobId} path=${path}:`, error);
          return of([]);
        }),
      );
  }

  /**
   * Get file content from a job's Gitea repository.
   * @param jobId Job ID
   * @param path File path within the repo
   */
  getRepoFile(jobId: string, path: string): Observable<{ path: string; content: string; size: number } | null> {
    const params = new HttpParams().set('path', path);
    return this.http
      .get<{ path: string; content: string; size: number }>(`${this.baseUrl}/jobs/${jobId}/repo/file`, { params })
      .pipe(
        catchError((error) => {
          console.error(`Failed to fetch repo file ${path} for job ${jobId}:`, error);
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

  // ===== Project Endpoints =====

  getProjects(userId?: string): Observable<Project[]> {
    let params = new HttpParams();
    if (userId) params = params.set('user_id', userId);
    return this.http.get<Project[]>(`${this.baseUrl}/projects`, { params }).pipe(
      catchError(() => of([])),
    );
  }

  getProject(id: string): Observable<Project | null> {
    return this.http.get<Project>(`${this.baseUrl}/projects/${id}`).pipe(
      catchError(() => of(null)),
    );
  }

  createProject(body: ProjectCreateRequest): Observable<Project | null> {
    return this.http.post<Project>(`${this.baseUrl}/projects`, body).pipe(
      catchError(() => of(null)),
    );
  }

  updateProject(id: string, body: ProjectUpdateRequest): Observable<{ status: string } | null> {
    return this.http.patch<{ status: string }>(`${this.baseUrl}/projects/${id}`, body).pipe(
      catchError(() => of(null)),
    );
  }

  deleteProject(id: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/projects/${id}`).pipe(
      catchError(() => of(null)),
    );
  }

  getProjectMembers(id: string): Observable<ProjectMember[]> {
    return this.http.get<ProjectMember[]>(`${this.baseUrl}/projects/${id}/members`).pipe(
      catchError(() => of([])),
    );
  }

  addProjectMember(id: string, body: ProjectMemberAddRequest): Observable<ProjectMember | null> {
    return this.http.post<ProjectMember>(`${this.baseUrl}/projects/${id}/members`, body).pipe(
      catchError(() => of(null)),
    );
  }

  updateProjectMember(id: string, userId: string, body: ProjectMemberUpdateRequest): Observable<{ status: string } | null> {
    return this.http.patch<{ status: string }>(`${this.baseUrl}/projects/${id}/members/${userId}`, body).pipe(
      catchError(() => of(null)),
    );
  }

  removeProjectMember(id: string, userId: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/projects/${id}/members/${userId}`).pipe(
      catchError(() => of(null)),
    );
  }

  getProjectRepositories(id: string): Observable<ProjectRepository[]> {
    return this.http.get<ProjectRepository[]>(`${this.baseUrl}/projects/${id}/repositories`).pipe(
      catchError(() => of([])),
    );
  }

  addProjectRepository(id: string, body: ProjectRepositoryCreateRequest): Observable<ProjectRepository | null> {
    return this.http.post<ProjectRepository>(`${this.baseUrl}/projects/${id}/repositories`, body).pipe(
      catchError(() => of(null)),
    );
  }

  updateProjectRepository(id: string, repoId: string, body: ProjectRepositoryUpdateRequest): Observable<{ status: string } | null> {
    return this.http.patch<{ status: string }>(`${this.baseUrl}/projects/${id}/repositories/${repoId}`, body).pipe(
      catchError(() => of(null)),
    );
  }

  removeProjectRepository(id: string, repoId: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(`${this.baseUrl}/projects/${id}/repositories/${repoId}`).pipe(
      catchError(() => of(null)),
    );
  }

  getProjectExperts(projectId: string): Observable<Expert[]> {
    return this.http.get<Expert[]>(`${this.baseUrl}/projects/${projectId}/experts`).pipe(
      catchError(() => of([])),
    );
  }

  getProjectExpertDetail(projectId: string, expertName: string): Observable<ExpertDetail | null> {
    return this.http.get<ExpertDetail>(`${this.baseUrl}/projects/${projectId}/experts/${expertName}`).pipe(
      catchError(() => of(null)),
    );
  }

  getProjectJobs(id: string, status?: string, limit: number = 100): Observable<Job[]> {
    let params = new HttpParams().set('limit', limit.toString());
    if (status) params = params.set('status', status);
    return this.http.get<Job[]>(`${this.baseUrl}/projects/${id}/jobs`, { params }).pipe(
      catchError(() => of([])),
    );
  }

  createProjectJob(id: string, body: JobCreateRequest): Observable<Job | null> {
    return this.http.post<Job>(`${this.baseUrl}/projects/${id}/jobs`, body).pipe(
      catchError(() => of(null)),
    );
  }

  promoteJob(jobId: string, body: PromoteRequest): Observable<{ status: string; project_id?: string } | null> {
    return this.http.post<{ status: string; project_id?: string }>(
      `${this.baseUrl}/jobs/${jobId}/promote`, body,
    ).pipe(catchError(() => of(null)));
  }

  // ===== Knowledge Base Endpoints =====

  getKnowledgeSummary(projectId: string): Observable<KnowledgeSummary | null> {
    return this.http.get<KnowledgeSummary>(
      `${this.baseUrl}/projects/${projectId}/knowledge/summary`,
    ).pipe(catchError(() => of(null)));
  }

  getKnowledgeNotes(
    projectId: string,
    filters?: { type?: string; status?: string; tag?: string; job_id?: string; limit?: number; offset?: number },
  ): Observable<KnowledgeListResponse | null> {
    let params = new HttpParams();
    if (filters?.type) params = params.set('type', filters.type);
    if (filters?.status) params = params.set('status', filters.status);
    if (filters?.tag) params = params.set('tag', filters.tag);
    if (filters?.job_id) params = params.set('job_id', filters.job_id);
    if (filters?.limit) params = params.set('limit', filters.limit.toString());
    if (filters?.offset) params = params.set('offset', filters.offset.toString());
    return this.http.get<KnowledgeListResponse>(
      `${this.baseUrl}/projects/${projectId}/knowledge`, { params },
    ).pipe(catchError(() => of(null)));
  }

  getKnowledgeNote(projectId: string, noteId: string): Observable<KnowledgeNoteDetail | null> {
    return this.http.get<KnowledgeNoteDetail>(
      `${this.baseUrl}/projects/${projectId}/knowledge/${noteId}`,
    ).pipe(catchError(() => of(null)));
  }

  searchKnowledge(projectId: string, query: string, limit: number = 10): Observable<KnowledgeSearchResponse | null> {
    return this.http.post<KnowledgeSearchResponse>(
      `${this.baseUrl}/projects/${projectId}/knowledge/search`,
      { query, limit },
    ).pipe(catchError(() => of(null)));
  }

  updateKnowledgeNote(
    projectId: string, noteId: string,
    body: { status?: string; add_tags?: string[]; remove_tags?: string[] },
  ): Observable<{ status: string } | null> {
    return this.http.patch<{ status: string }>(
      `${this.baseUrl}/projects/${projectId}/knowledge/${noteId}`, body,
    ).pipe(catchError(() => of(null)));
  }

  deleteKnowledgeNote(projectId: string, noteId: string): Observable<{ status: string } | null> {
    return this.http.delete<{ status: string }>(
      `${this.baseUrl}/projects/${projectId}/knowledge/${noteId}`,
    ).pipe(catchError(() => of(null)));
  }

  exportKnowledge(projectId: string): Observable<{ status: string; path: string; note_count: number } | null> {
    return this.http.post<{ status: string; path: string; note_count: number }>(
      `${this.baseUrl}/projects/${projectId}/knowledge/export`, {},
    ).pipe(catchError(() => of(null)));
  }

  // ===== Memory Endpoints =====

  getMemoryStats(jobId: string): Observable<MemoryStats | null> {
    return this.http.get<MemoryStats>(`${this.baseUrl}/jobs/${jobId}/memory/stats`).pipe(
      catchError(() => of(null)),
    );
  }

  getMemories(
    jobId: string,
    filters?: {
      memory_type?: string;
      source?: string;
      search?: string;
      sort_by?: string;
      sort_order?: string;
      limit?: number;
      offset?: number;
    },
  ): Observable<MemoryListResponse | null> {
    let params = new HttpParams();
    if (filters?.memory_type) params = params.set('memory_type', filters.memory_type);
    if (filters?.source) params = params.set('source', filters.source);
    if (filters?.search) params = params.set('search', filters.search);
    if (filters?.sort_by) params = params.set('sort_by', filters.sort_by);
    if (filters?.sort_order) params = params.set('sort_order', filters.sort_order);
    if (filters?.limit) params = params.set('limit', filters.limit.toString());
    if (filters?.offset) params = params.set('offset', filters.offset.toString());

    return this.http.get<MemoryListResponse>(
      `${this.baseUrl}/jobs/${jobId}/memories`, { params },
    ).pipe(catchError(() => of(null)));
  }
}
