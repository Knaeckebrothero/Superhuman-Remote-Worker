import { Injectable } from '@angular/core';
import { FilePreview, FilePreviewResult, FileType, RejectedFile, UploadStatus } from '../models/file.model';
import { RecordingResult } from '../models/recording.model';

/** Optional override for `createFilePreviews()`'s size/count caps — see the
 *  job-creation caps below for why a caller would need this. */
export interface FileCaps {
  maxFileSizeMB: number;
  maxFiles: number;
}

/**
 * Service for handling file-related operations such as validation,
 * preview generation, and utility functions for file type detection.
 *
 * Two call sites use this service against two different backend endpoints
 * with genuinely different caps, not just one the frontend used to
 * over-report:
 *  - persistent-chat's composer uploads into a *live* thread workspace
 *    (`POST /api/persistent/threads/{id}/uploads`, orchestrator/services/
 *    thread_uploads.py) — 100MB per file is a real server limit that binds
 *    on every request (thread_uploads.py:69). 20 files is NOT a server
 *    limit here: it's our own composer policy, because the composer now
 *    sends one request per file, so the server's per-request file-count cap
 *    can never be hit by this client (see MAX_FILES below).
 *  - job-create uploads into local orchestrator storage for a not-yet-
 *    running job (`POST /api/uploads`, orchestrator/uploads.py:53-54) —
 *    genuinely capped at 5GB / 100 files server-side, a different order of
 *    magnitude because it never touches a live container/VM.
 * `MAX_FILE_SIZE_MB`/`MAX_FILES` below are the first (smaller) pair, applied
 * by default; job-create passes its own larger pair explicitly (see
 * `getJobUploadMaxFileSizeMB`/`getJobUploadMaxFiles`) so neither caller lies
 * about what the server will actually accept.
 */
@Injectable({
  providedIn: 'root',
})
export class FileHandlingService {
  /** Maximum file size in MB. Mirrors MAX_FILE_SIZE in
   *  orchestrator/services/thread_uploads.py:69 — the server rejects anything
   *  larger with 413, and the client should say so before the bytes move. */
  private readonly MAX_FILE_SIZE_MB = 100;

  /** Maximum files per upload. The server's own per-request cap
   *  (MAX_FILES_PER_REQUEST = 20, thread_uploads.py:70) can no longer bind
   *  here now that the composer sends one request per file (Task 2) — this
   *  is a deliberate composer-level policy cap, not a mirror of a server
   *  limit we'd otherwise hit. */
  private readonly MAX_FILES = 20;

  /** Job-creation uploads (`POST /api/uploads`) land in local orchestrator
   *  storage rather than a live workspace, so the server allows much larger
   *  batches — see orchestrator/uploads.py:53. */
  private readonly JOB_UPLOAD_MAX_FILE_SIZE_MB = 5120; // 5GB

  /** Mirrors MAX_FILES_PER_UPLOAD in orchestrator/uploads.py:54. */
  private readonly JOB_UPLOAD_MAX_FILES = 100;

  /**
   * Validates file size against maximum limit.
   * @param file File to validate
   * @returns true if file is within size limit
   */
  validateFileSize(file: File): boolean {
    return file.size <= this.MAX_FILE_SIZE_MB * 1024 * 1024;
  }

  /**
   * Validates total file count.
   * @param count Number of files
   * @returns true if count is within limit
   */
  validateFileCount(count: number): boolean {
    return count <= this.MAX_FILES;
  }

  /**
   * Get maximum file size in MB for thread/session composer uploads.
   */
  getMaxFileSizeMB(): number {
    return this.MAX_FILE_SIZE_MB;
  }

  /**
   * Get maximum file count for thread/session composer uploads.
   */
  getMaxFiles(): number {
    return this.MAX_FILES;
  }

  /** Get maximum file size in MB for job-creation uploads (`POST /api/uploads`). */
  getJobUploadMaxFileSizeMB(): number {
    return this.JOB_UPLOAD_MAX_FILE_SIZE_MB;
  }

  /** Get maximum file count for job-creation uploads (`POST /api/uploads`). */
  getJobUploadMaxFiles(): number {
    return this.JOB_UPLOAD_MAX_FILES;
  }

  /**
   * Creates file previews from selected files, honestly enforcing the same
   * caps the target endpoint does: files over the size cap or past the count
   * cap are reported back via `rejected` instead of silently vanishing (the
   * previous behaviour — a `console.warn` no user ever sees).
   *
   * Defaults to the thread/session composer's caps. Pass `caps` to validate
   * against a different endpoint's limits (job-create's `POST /api/uploads`
   * allows much larger batches — see `getJobUploadMaxFileSizeMB`/
   * `getJobUploadMaxFiles`).
   *
   * @param files Array of files to create previews for
   * @param caps Optional override of the default size/count caps
   * @returns Promise of accepted previews plus anything rejected, and why
   */
  async createFilePreviews(files: File[], caps?: FileCaps): Promise<FilePreviewResult> {
    const maxFileSizeBytes = (caps?.maxFileSizeMB ?? this.MAX_FILE_SIZE_MB) * 1024 * 1024;
    const maxFiles = caps?.maxFiles ?? this.MAX_FILES;
    const previews: FilePreview[] = [];
    const rejected: RejectedFile[] = [];

    for (const file of files) {
      if (file.size > maxFileSizeBytes) {
        rejected.push({ name: file.name, reason: 'size' });
        continue;
      }
      if (previews.length >= maxFiles) {
        rejected.push({ name: file.name, reason: 'count' });
        continue;
      }

      const preview: FilePreview = {
        id: this.generateId(),
        file,
        name: file.name,
        size: file.size,
        sizeFormatted: this.formatFileSize(file.size),
        type: this.getFileType(file.type),
        mimeType: file.type || 'application/octet-stream',
        uploadStatus: UploadStatus.PENDING,
      };

      // Generate image preview for image files
      if (preview.type === FileType.IMAGE) {
        try {
          preview.preview = await this.generateImagePreview(file);
        } catch (e) {
          console.error('Failed to generate image preview:', e);
        }
      }

      previews.push(preview);
    }

    return { previews, rejected };
  }

  /**
   * Generates a unique identifier for file tracking.
   */
  private generateId(): string {
    return `file-${Date.now()}-${Math.random().toString(36).substring(2, 11)}`;
  }

  /**
   * Formats file size to human-readable string.
   * @param bytes Size in bytes
   * @returns Formatted size string (e.g., "1.5 MB")
   */
  formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 Bytes';

    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));

    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  }

  /**
   * Determines file type from MIME type.
   * @param mimeType MIME type string
   * @returns FileType enum value
   */
  getFileType(mimeType: string): FileType {
    if (!mimeType) return FileType.OTHER;

    if (mimeType.startsWith('image/')) return FileType.IMAGE;
    if (mimeType.startsWith('video/')) return FileType.VIDEO;
    if (mimeType.startsWith('audio/')) return FileType.AUDIO;

    const documentTypes = [
      'application/pdf',
      'application/msword',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      'application/vnd.ms-excel',
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'text/plain',
      'text/markdown',
      'text/csv',
    ];

    if (documentTypes.includes(mimeType)) return FileType.DOCUMENT;

    // Treat zip files as documents (they contain documents)
    const archiveTypes = ['application/zip', 'application/x-zip-compressed'];
    if (archiveTypes.includes(mimeType)) return FileType.DOCUMENT;

    return FileType.OTHER;
  }

  /**
   * Gets appropriate icon name for a file type.
   * @param type FileType enum value
   * @returns Icon name for display
   */
  getFileIcon(type: FileType): string {
    switch (type) {
      case FileType.IMAGE:
        return 'image';
      case FileType.VIDEO:
        return 'videocam';
      case FileType.AUDIO:
        return 'audiotrack';
      case FileType.DOCUMENT:
        return 'description';
      default:
        return 'insert_drive_file';
    }
  }

  /**
   * Generates a base64 data URL preview for an image file.
   * @param file Image file to preview
   * @returns Promise with data URL string
   */
  private generateImagePreview(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = (e) => resolve(e.target?.result as string);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  /**
   * Build a FilePreview for a voice recording. The file name encodes the
   * timestamp and duration so it's distinguishable in the workspace.
   */
  async createAudioFilePreview(result: RecordingResult): Promise<FilePreview> {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const extension = this.getAudioExtension(result.mimeType);
    const filename = `voice-message-${timestamp}.${extension}`;
    const audioFile = new File([result.blob], filename, {
      type: result.mimeType,
      lastModified: Date.now(),
    });
    return {
      id: this.generateId(),
      file: audioFile,
      name: `Voice message (${this.formatDuration(result.duration)})`,
      size: audioFile.size,
      sizeFormatted: this.formatFileSize(audioFile.size),
      type: FileType.AUDIO,
      mimeType: audioFile.type,
      uploadStatus: UploadStatus.PENDING,
    };
  }

  private getAudioExtension(mimeType: string): string {
    const map: Record<string, string> = {
      'audio/webm': 'webm',
      'audio/ogg': 'ogg',
      'audio/mp4': 'm4a',
      'audio/mpeg': 'mp3',
      'audio/wav': 'wav',
    };
    return map[mimeType.split(';')[0]] || 'webm';
  }

  private formatDuration(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  }
}
