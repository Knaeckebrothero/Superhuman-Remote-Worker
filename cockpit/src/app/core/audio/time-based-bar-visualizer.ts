import { VisualizationBar } from '../models/recording.model';
import { AUDIO_VISUALIZATION_CONFIG } from './audio-constants';
import { CanvasRenderer } from './canvas-renderer';

export class TimeBasedBarVisualizer {
  private bars: VisualizationBar[] = [];
  private lastBarTime = 0;
  private readonly BAR_INTERVAL = AUDIO_VISUALIZATION_CONFIG.BAR_INTERVAL;
  private readonly BAR_WIDTH = AUDIO_VISUALIZATION_CONFIG.BAR_WIDTH;
  private readonly BAR_GAP = AUDIO_VISUALIZATION_CONFIG.BAR_GAP;
  private readonly TOTAL_BAR_SPACE = this.BAR_WIDTH + this.BAR_GAP;
  private readonly SCROLL_SPEED = AUDIO_VISUALIZATION_CONFIG.SCROLL_SPEED;

  private animationId: number | null = null;
  private lastAnimationTime = 0;
  private renderer: CanvasRenderer;

  constructor(
    private canvas: HTMLCanvasElement,
    private audioLevelCallback: () => number,
  ) {
    this.renderer = new CanvasRenderer(canvas, this.BAR_WIDTH, this.BAR_GAP);
  }

  start(): void {
    this.lastAnimationTime = performance.now();
    this.lastBarTime = this.lastAnimationTime;
    this.animationId = requestAnimationFrame(this.animate);
  }

  stop(): void {
    if (this.animationId !== null) {
      cancelAnimationFrame(this.animationId);
      this.animationId = null;
    }
    this.bars = [];
  }

  private animate = (currentTime: number): void => {
    const deltaTime = currentTime - this.lastAnimationTime;
    this.lastAnimationTime = currentTime;

    if (currentTime - this.lastBarTime >= this.BAR_INTERVAL) {
      this.addNewBar(this.audioLevelCallback());
      this.lastBarTime = currentTime;
    }

    this.updateBarPositions(deltaTime);
    this.renderer.renderFrame(this.bars);

    this.animationId = requestAnimationFrame(this.animate);
  };

  private addNewBar(height: number): void {
    const lastBar = this.bars[this.bars.length - 1];
    const startX = lastBar ? lastBar.x + this.TOTAL_BAR_SPACE : this.canvas.width;
    this.bars.push({ height, timestamp: Date.now(), x: startX });
  }

  private updateBarPositions(deltaTime: number): void {
    const scrollDistance = (this.SCROLL_SPEED * deltaTime) / 1000;
    this.bars = this.bars.filter((bar) => {
      bar.x -= scrollDistance;
      return bar.x > -(this.BAR_WIDTH + this.BAR_GAP);
    });
  }
}
