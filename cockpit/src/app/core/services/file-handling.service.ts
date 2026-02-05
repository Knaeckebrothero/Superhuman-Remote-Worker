import { Injectable } from '@angular/core';
import { FilePreview, FileType, UploadStatus } from '../models/file.model';

/**
 * Service for handling file-related operations such as validation,
 * preview generation, and utility functions for file type detection.
 */
@Injectable({
  providedIn: 'root',
})
export class FileHandlingService {
  /** Maximum file size in MB */
  private readonly MAX_FILE_SIZE_MB = 5120; // 5GB

  /** Maximum number of files per upload */
  private readonly MAX_FILES = 100;

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
   * Get maximum file size in MB.
   */
  getMaxFileSizeMB(): number {
    return this.MAX_FILE_SIZE_MB;
  }

  /**
   * Get maximum file count.
   */
  getMaxFiles(): number {
    return this.MAX_FILES;
  }

  /**
   * Creates file previews from selected files.
   * @param files Array of files to create previews for
   * @returns Promise with array of file previews
   */
  async createFilePreviews(files: File[]): Promise<FilePreview[]> {
    const previews: FilePreview[] = [];

    for (const file of files) {
      // Skip oversized files
      if (!this.validateFileSize(file)) {
        console.warn(`File "${file.name}" exceeds ${this.MAX_FILE_SIZE_MB}MB limit`);
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

    return previews;
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
}
