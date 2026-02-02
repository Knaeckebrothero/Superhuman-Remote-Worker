/**
 * File upload models for job creation.
 */

/**
 * File type categories.
 */
export enum FileType {
  IMAGE = 'image',
  VIDEO = 'video',
  AUDIO = 'audio',
  DOCUMENT = 'document',
  OTHER = 'other',
}

/**
 * Upload status for tracking file upload progress.
 */
export enum UploadStatus {
  PENDING = 'pending',
  UPLOADING = 'uploading',
  COMPLETED = 'completed',
  FAILED = 'failed',
}

/**
 * File preview for local UI state.
 * Tracks file selection, validation, and upload progress.
 */
export interface FilePreview {
  /** Unique local identifier */
  id: string;
  /** Original File object (for upload) */
  file: File;
  /** Display name */
  name: string;
  /** File size in bytes */
  size: number;
  /** Formatted file size (e.g., "1.5 MB") */
  sizeFormatted: string;
  /** Detected file type category */
  type: FileType;
  /** MIME type */
  mimeType: string;
  /** Base64 data URL for image preview */
  preview?: string;
  /** Upload progress percentage (0-100) */
  uploadProgress?: number;
  /** Current upload status */
  uploadStatus: UploadStatus;
  /** Error message if upload failed */
  error?: string;
}

/**
 * Uploaded file metadata from server.
 */
export interface UploadedFile {
  /** Filename as stored on server */
  name: string;
  /** File size in bytes */
  size: number;
  /** MIME type */
  mime_type: string;
}

/**
 * Response from POST /api/uploads endpoint.
 */
export interface UploadResponse {
  /** Unique upload identifier */
  upload_id: string;
  /** List of uploaded files */
  files: UploadedFile[];
}

/**
 * Information about an existing upload.
 */
export interface UploadInfo {
  /** Unique upload identifier */
  upload_id: string;
  /** List of files in the upload */
  files: UploadedFile[];
  /** ISO timestamp when upload was created */
  created_at: string;
}
