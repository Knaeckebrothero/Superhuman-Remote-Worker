import { VisualizationBar } from '../models/recording.model';

export class CanvasRenderer {
  private ctx: CanvasRenderingContext2D;
  private readonly BAR_WIDTH: number;
  private readonly TOTAL_BAR_SPACE: number;
  private barGradient: CanvasGradient | null = null;

  constructor(
    private canvas: HTMLCanvasElement,
    barWidth: number,
    totalBarSpace: number,
  ) {
    // alpha:true so the canvas is transparent and inherits whatever the
    // theme puts behind it (dark in cockpit, light elsewhere). The
    // original Advanced-LLM-Chat used alpha:false + a hardcoded white
    // fill, which clashed with the dark composer.
    this.ctx = canvas.getContext('2d', {
      alpha: true,
      desynchronized: true,
    })!;

    this.BAR_WIDTH = barWidth;
    this.TOTAL_BAR_SPACE = totalBarSpace;

    this.createBarGradient();
  }

  private createBarGradient(): void {
    this.barGradient = this.ctx.createLinearGradient(0, 0, 0, this.canvas.height);
    this.barGradient.addColorStop(0, '#66B3FF');
    this.barGradient.addColorStop(0.5, '#4CA5DC');
    this.barGradient.addColorStop(1, '#3399D6');
  }

  renderFrame(bars: VisualizationBar[]): void {
    // Transparent wipe — the surrounding theme colors the background.
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

    const visibleBars = bars.filter(
      (bar) => bar.x > -this.TOTAL_BAR_SPACE && bar.x < this.canvas.width,
    );

    this.ctx.fillStyle = this.barGradient ?? '#4CA5DC';

    visibleBars.forEach((bar) => {
      const normalizedHeight = Math.max(bar.height * 1.8, 5);
      const barHeight = (normalizedHeight / 100) * this.canvas.height;
      const cappedHeight = Math.min(barHeight, this.canvas.height - 4);
      const y = this.canvas.height - cappedHeight;
      const x = Math.floor(bar.x);

      this.drawRoundedRect(x, y, this.BAR_WIDTH, cappedHeight, 4);

      this.ctx.shadowColor = 'rgba(0, 0, 0, 0.1)';
      this.ctx.shadowBlur = 4;
      this.ctx.shadowOffsetX = 2;
      this.ctx.shadowOffsetY = 2;
      this.ctx.fill();

      this.ctx.shadowBlur = 0;
      this.ctx.shadowOffsetX = 0;
      this.ctx.shadowOffsetY = 0;
    });
  }

  private drawRoundedRect(x: number, y: number, width: number, height: number, radius: number): void {
    radius = Math.min(radius, width / 2, height / 2);

    this.ctx.beginPath();
    this.ctx.moveTo(x + radius, y);
    this.ctx.lineTo(x + width - radius, y);
    this.ctx.arc(x + width - radius, y + radius, radius, -Math.PI / 2, 0);
    this.ctx.lineTo(x + width, y + height - radius);
    this.ctx.arc(x + width - radius, y + height - radius, radius, 0, Math.PI / 2);
    this.ctx.lineTo(x + radius, y + height);
    this.ctx.arc(x + radius, y + height - radius, radius, Math.PI / 2, Math.PI);
    this.ctx.lineTo(x, y + radius);
    this.ctx.arc(x + radius, y + radius, radius, Math.PI, -Math.PI / 2);
    this.ctx.closePath();
  }
}
