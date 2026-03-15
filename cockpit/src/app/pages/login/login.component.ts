import { Component, inject, signal, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { UserService } from '../../core/services/user.service';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [FormsModule, RouterLink],
  template: `
    <div class="login-page">
      <div class="login-card">
        <div class="login-header">
          <h1 class="login-title">SRW</h1>
          <p class="login-subtitle">Cockpit</p>
        </div>

        <div class="login-form">
          <input
            type="email"
            class="form-input"
            placeholder="Email address"
            [(ngModel)]="email"
            (keyup.enter)="login()"
            [disabled]="loading()"
            autofocus
          />

          <input
            type="password"
            class="form-input"
            placeholder="Password"
            [(ngModel)]="password"
            (keyup.enter)="login()"
            [disabled]="loading()"
          />

          <button
            class="login-button"
            (click)="login()"
            [disabled]="!canSubmit() || loading()"
          >
            @if (loading()) {
              <span class="spinner"></span>
            }
            Sign In
          </button>

          @if (error()) {
            <p class="error-message">{{ error() }}</p>
          }

          <div class="auth-links">
            <a routerLink="/register">Create Account</a>
            <a routerLink="/forgot-password">Forgot Password?</a>
          </div>
        </div>
      </div>
    </div>
  `,
  styles: [
    `
      .login-page {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100vh;
        height: 100dvh;
        background: var(--app-bg, #1e1e2e);
      }

      .login-card {
        width: 360px;
        padding: 40px;
        background: var(--panel-bg, #181825);
        border: 1px solid var(--border-color, #313244);
        border-radius: 12px;
      }

      .login-header {
        text-align: center;
        margin-bottom: 32px;
      }

      .login-title {
        font-size: 32px;
        font-weight: 700;
        color: var(--accent-color, #cba6f7);
        letter-spacing: 2px;
        margin-bottom: 4px;
      }

      .login-subtitle {
        font-size: 14px;
        color: var(--text-muted, #6c7086);
        letter-spacing: 1px;
      }

      .login-form {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .form-input {
        width: 100%;
        padding: 10px 14px;
        background: var(--surface-0, #313244);
        border: 1px solid var(--border-color, #313244);
        border-radius: 8px;
        color: var(--text-primary, #cdd6f4);
        font-size: 14px;
        font-family: inherit;
        outline: none;
        transition: border-color 0.15s ease;
      }

      .form-input:focus {
        border-color: var(--accent-color, #cba6f7);
      }

      .form-input::placeholder {
        color: var(--text-muted, #6c7086);
      }

      .form-input:disabled {
        opacity: 0.6;
      }

      .login-button {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        width: 100%;
        padding: 10px 14px;
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        border: none;
        border-radius: 8px;
        font-size: 14px;
        font-weight: 600;
        font-family: inherit;
        cursor: pointer;
        transition: opacity 0.15s ease;
      }

      .login-button:hover:not(:disabled) {
        opacity: 0.9;
      }

      .login-button:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .spinner {
        width: 14px;
        height: 14px;
        border: 2px solid transparent;
        border-top-color: var(--timeline-bg, #11111b);
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
      }

      @keyframes spin {
        to {
          transform: rotate(360deg);
        }
      }

      .error-message {
        color: #f38ba8;
        font-size: 13px;
        text-align: center;
      }

      .auth-links {
        display: flex;
        justify-content: space-between;
        margin-top: 4px;
      }

      .auth-links a {
        color: var(--text-muted, #6c7086);
        font-size: 13px;
        text-decoration: none;
        transition: color 0.15s ease;
      }

      .auth-links a:hover {
        color: var(--accent-color, #cba6f7);
      }
    `,
  ],
})
export class LoginComponent implements OnInit {
  readonly userService = inject(UserService);
  private readonly router = inject(Router);

  email = '';
  password = '';
  readonly loading = signal(false);
  readonly error = signal('');

  ngOnInit(): void {
    if (this.userService.isAuthenticated()) {
      this.router.navigate(['/']);
    }
  }

  canSubmit(): boolean {
    if (!this.email.trim()) return false;
    if (!this.password) return false;
    return true;
  }

  login(): void {
    if (!this.canSubmit() || this.loading()) return;

    this.loading.set(true);
    this.error.set('');

    this.userService.login(this.email.trim(), this.password).subscribe({
      next: (user) => {
        this.loading.set(false);
        if (user) {
          this.router.navigate(['/']);
        } else {
          this.error.set('Login failed. Please try again.');
        }
      },
      error: (err: HttpErrorResponse) => {
        this.loading.set(false);
        const detail = err.error?.detail;
        if (err.status === 403 && detail?.includes('not verified')) {
          this.error.set('Email not verified. Check your inbox.');
        } else {
          this.error.set(detail || 'Login failed. Please try again.');
        }
      },
    });
  }
}
