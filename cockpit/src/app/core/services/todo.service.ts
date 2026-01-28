import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import {
  JobTodos,
  TodoArchiveInfo,
  ArchivedTodos,
  CurrentTodos,
} from '../models/todo.model';
import { DataService } from './data.service';

/**
 * Service for fetching and managing todo data from workspace files.
 */
@Injectable({ providedIn: 'root' })
export class TodoService {
  private readonly http = inject(HttpClient);
  private readonly data = inject(DataService);
  private readonly baseUrl = 'http://localhost:8085/api';

  // State signals
  readonly isLoading = signal(false);
  readonly error = signal<string | null>(null);

  // Current todos (live from todos.yaml)
  readonly currentTodos = signal<CurrentTodos | null>(null);

  // Archive list
  readonly archives = signal<TodoArchiveInfo[]>([]);

  // Selected archive (for viewing historical todos)
  readonly selectedArchive = signal<string | null>(null);
  readonly selectedArchiveData = signal<ArchivedTodos | null>(null);

  // Has workspace
  readonly hasWorkspace = signal(false);

  /**
   * Computed: todos to display (either current or selected archive).
   */
  readonly displayTodos = computed(() => {
    const archive = this.selectedArchiveData();
    if (archive) {
      return archive.todos;
    }
    const current = this.currentTodos();
    return current?.todos || [];
  });

  /**
   * Computed: display source label.
   */
  readonly displaySource = computed(() => {
    const archive = this.selectedArchiveData();
    if (archive) {
      return archive.phase_name || archive.source;
    }
    const current = this.currentTodos();
    return current ? 'Current Phase' : null;
  });

  /**
   * Computed: is showing current todos (not archive).
   */
  readonly isShowingCurrent = computed(() => {
    return this.selectedArchive() === null && this.currentTodos() !== null;
  });

  /**
   * Computed: progress stats for current display.
   */
  readonly progress = computed(() => {
    const todos = this.displayTodos();
    const total = todos.length;
    const completed = todos.filter(t => t.status === 'completed').length;
    const inProgress = todos.filter(t => t.status === 'in_progress').length;
    const pending = todos.filter(t => t.status === 'pending').length;
    const percentage = total > 0 ? Math.round((completed / total) * 100) : 0;

    return { total, completed, inProgress, pending, percentage };
  });

  /**
   * Computed: failure note if viewing a failed archive.
   */
  readonly failureNote = computed(() => {
    return this.selectedArchiveData()?.failure_note || null;
  });

  /**
   * Load all todos for the currently selected job.
   */
  async loadTodos(): Promise<void> {
    const jobId = this.data.currentJobId();
    if (!jobId) {
      this.clear();
      return;
    }

    this.isLoading.set(true);
    this.error.set(null);

    try {
      const data = await firstValueFrom(
        this.http.get<JobTodos>(`${this.baseUrl}/jobs/${jobId}/todos`)
      );

      this.hasWorkspace.set(data.has_workspace);
      this.currentTodos.set(data.current);
      this.archives.set(data.archives);

      // Clear selected archive when loading new job
      this.selectedArchive.set(null);
      this.selectedArchiveData.set(null);
    } catch (err) {
      console.error('[TodoService] Failed to load todos:', err);
      this.error.set('Failed to load todos');
      this.clear();
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Select an archive to view.
   */
  async selectArchive(filename: string | null): Promise<void> {
    const jobId = this.data.currentJobId();
    if (!jobId) return;

    this.selectedArchive.set(filename);

    if (!filename) {
      this.selectedArchiveData.set(null);
      return;
    }

    this.isLoading.set(true);
    this.error.set(null);

    try {
      const data = await firstValueFrom(
        this.http.get<ArchivedTodos>(
          `${this.baseUrl}/jobs/${jobId}/todos/archives/${filename}`
        )
      );
      this.selectedArchiveData.set(data);
    } catch (err) {
      console.error('[TodoService] Failed to load archive:', err);
      this.error.set('Failed to load archive');
      this.selectedArchiveData.set(null);
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Show current todos (deselect archive).
   */
  showCurrent(): void {
    this.selectedArchive.set(null);
    this.selectedArchiveData.set(null);
  }

  /**
   * Refresh todos for current job.
   */
  async refresh(): Promise<void> {
    await this.loadTodos();

    // If we had an archive selected, reload it
    const archiveFilename = this.selectedArchive();
    if (archiveFilename) {
      await this.selectArchive(archiveFilename);
    }
  }

  /**
   * Clear all state.
   */
  clear(): void {
    this.currentTodos.set(null);
    this.archives.set([]);
    this.selectedArchive.set(null);
    this.selectedArchiveData.set(null);
    this.hasWorkspace.set(false);
  }
}
