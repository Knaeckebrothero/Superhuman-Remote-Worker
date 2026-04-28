import {Injectable, signal} from '@angular/core';

export type ToastTone = 'info' | 'success' | 'warning' | 'danger';

export interface ToastOptions {
  tone?: ToastTone;
  duration?: number;
  dismissible?: boolean;
}

export interface ToastEntry {
  readonly id: number;
  readonly message: string;
  readonly tone: ToastTone;
  readonly duration: number;
  readonly dismissible: boolean;
}

let nextToastId = 0;

@Injectable({providedIn: 'root'})
export class AppToastService {
  readonly toasts = signal<readonly ToastEntry[]>([]);

  private timers = new Map<number, ReturnType<typeof setTimeout>>();

  show(message: string, options: ToastOptions = {}): number {
    const id = ++nextToastId;
    const tone = options.tone ?? 'info';
    const duration = options.duration ?? (tone === 'danger' ? 6000 : 4000);
    const dismissible = options.dismissible ?? true;
    const entry: ToastEntry = {id, message, tone, duration, dismissible};
    this.toasts.update((list) => [...list, entry]);
    if (duration > 0) {
      this.timers.set(
        id,
        setTimeout(() => this.dismiss(id), duration),
      );
    }
    return id;
  }

  info(message: string, options: Omit<ToastOptions, 'tone'> = {}): number {
    return this.show(message, {...options, tone: 'info'});
  }

  success(message: string, options: Omit<ToastOptions, 'tone'> = {}): number {
    return this.show(message, {...options, tone: 'success'});
  }

  warning(message: string, options: Omit<ToastOptions, 'tone'> = {}): number {
    return this.show(message, {...options, tone: 'warning'});
  }

  danger(message: string, options: Omit<ToastOptions, 'tone'> = {}): number {
    return this.show(message, {...options, tone: 'danger'});
  }

  dismiss(id: number): void {
    const timer = this.timers.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.timers.delete(id);
    }
    this.toasts.update((list) => list.filter((t) => t.id !== id));
  }

  dismissAll(): void {
    for (const t of this.timers.values()) clearTimeout(t);
    this.timers.clear();
    this.toasts.set([]);
  }
}
