import { beforeEach, describe, expect, it } from 'vitest';
import { FileHandlingService } from './file-handling.service';
import { UploadStatus } from '../models/file.model';

describe('FileHandlingService', () => {
  let service: FileHandlingService;

  beforeEach(() => {
    service = new FileHandlingService();
  });

  describe('createFilePreviews caps', () => {
    it('rejects a file over the backend 100MB limit instead of dropping it silently', async () => {
      const big = new File([new Uint8Array(1)], 'huge.pdf');
      Object.defineProperty(big, 'size', { value: 101 * 1024 * 1024 });

      const { previews, rejected } = await service.createFilePreviews([big]);

      expect(previews).toEqual([]);
      expect(rejected).toEqual([{ name: 'huge.pdf', reason: 'size' }]);
    });

    it('rejects files past the 20-file backend cap', async () => {
      const files = Array.from({ length: 21 }, (_, i) => new File(['x'], `f${i}.txt`));
      const { previews, rejected } = await service.createFilePreviews(files);

      expect(previews.length).toBe(20);
      expect(rejected).toEqual([{ name: 'f20.txt', reason: 'count' }]);
    });

    it('accepts a file exactly at the size cap (inclusive boundary)', async () => {
      const exact = new File([new Uint8Array(1)], 'exact.pdf');
      Object.defineProperty(exact, 'size', { value: 100 * 1024 * 1024 });

      const { previews, rejected } = await service.createFilePreviews([exact]);

      expect(rejected).toEqual([]);
      expect(previews.length).toBe(1);
    });

    it('accepts files within both caps with nothing rejected', async () => {
      const files = [new File(['x'], 'a.txt'), new File(['y'], 'b.txt')];
      const { previews, rejected } = await service.createFilePreviews(files);

      expect(rejected).toEqual([]);
      expect(previews.map((p) => p.name)).toEqual(['a.txt', 'b.txt']);
      expect(previews[0].uploadStatus).toBe(UploadStatus.PENDING);
    });
  });

  // job-create's endpoint (POST /api/uploads) lands files in local
  // orchestrator storage rather than a live thread workspace, so the server
  // genuinely allows a much larger batch there (orchestrator/uploads.py:53-54)
  // — this isn't the same 100MB/20 the composer caps default to.
  describe('createFilePreviews caps override', () => {
    it('honours a larger override, for a caller with bigger backend limits', async () => {
      const bigButAllowed = new File([new Uint8Array(1)], 'big.pdf');
      Object.defineProperty(bigButAllowed, 'size', { value: 500 * 1024 * 1024 }); // 500MB

      const { previews, rejected } = await service.createFilePreviews(
        [bigButAllowed],
        { maxFileSizeMB: 5120, maxFiles: 100 },
      );

      expect(rejected).toEqual([]);
      expect(previews.map((p) => p.name)).toEqual(['big.pdf']);
    });

    it('honours a stricter override too', async () => {
      const files = [new File(['x'], 'a.txt'), new File(['y'], 'b.txt'), new File(['z'], 'c.txt')];
      const { previews, rejected } = await service.createFilePreviews(files, {
        maxFileSizeMB: 100,
        maxFiles: 2,
      });

      expect(previews.map((p) => p.name)).toEqual(['a.txt', 'b.txt']);
      expect(rejected).toEqual([{ name: 'c.txt', reason: 'count' }]);
    });
  });
});
