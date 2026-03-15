import { Component, inject, signal, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { UserService } from '../../core/services/user.service';

@Component({
  selector: 'app-register',
  standalone: true,
  imports: [FormsModule, RouterLink],
  template: `
    <div class="auth-page">
      <div class="auth-card">
        <div class="auth-header">
          <h1 class="auth-title">SRW</h1>
          <p class="auth-subtitle">Create Account</p>
        </div>

        @if (step() === 'register') {
          <div class="auth-form">
            <input
              type="text"
              class="form-input"
              placeholder="Display name"
              [(ngModel)]="displayName"
              (keyup.enter)="register()"
              [disabled]="loading()"
              autofocus
            />
            <input
              type="email"
              class="form-input"
              placeholder="Email address"
              [(ngModel)]="email"
              (keyup.enter)="register()"
              [disabled]="loading()"
            />
            <input
              type="password"
              class="form-input"
              placeholder="Password (min 8 characters)"
              [(ngModel)]="password"
              (keyup.enter)="register()"
              [disabled]="loading()"
            />
            <input
              type="password"
              class="form-input"
              placeholder="Confirm password"
              [(ngModel)]="confirmPassword"
              (keyup.enter)="register()"
              [disabled]="loading()"
            />

            <button
              class="auth-button"
              (click)="register()"
              [disabled]="!canRegister() || loading()"
            >
              @if (loading()) {
                <span class="spinner"></span>
              }
              Create Account
            </button>

            @if (error()) {
              <p class="error-message">{{ error() }}</p>
            }

            <div class="auth-links">
              <a routerLink="/login">Already have an account? Sign in</a>
            </div>
          </div>
        }

        @if (step() === 'verify') {
          <div class="auth-form">
            <p class="info-text">
              A verification code was sent to <strong>{{ email }}</strong>.
              Enter it below to complete registration.
            </p>

            <input
              type="text"
              class="form-input code-input"
              placeholder="6-digit code"
              [(ngModel)]="code"
              (keyup.enter)="verify()"
              [disabled]="loading()"
              maxlength="6"
              autofocus
            />

            <button
              class="auth-button"
              (click)="verify()"
              [disabled]="code.length !== 6 || loading()"
            >
              @if (loading()) {
                <span class="spinner"></span>
              }
              Verify Email
            </button>

            @if (error()) {
              <p class="error-message">{{ error() }}</p>
            }
            @if (success()) {
              <p class="success-message">{{ success() }}</p>
            }

            <div class="auth-links">
              <a (click)="resend()" class="action-link">Resend Code</a>
            </div>
          </div>
        }
      </div>
    </div>
  `,
  styles: [
    `
      .auth-page {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100vh;
        height: 100dvh;
        background: var(--app-bg, #1e1e2e);
      }

      .auth-card {
        width: 360px;
        padding: 40px;
        background: var(--panel-bg, #181825);
        border: 1px solid var(--border-color, #313244);
        border-radius: 12px;
      }

      .auth-header {
        text-align: center;
        margin-bottom: 32px;
      }

      .auth-title {
        font-size: 32px;
        font-weight: 700;
        color: var(--accent-color, #cba6f7);
        letter-spacing: 2px;
        margin-bottom: 4px;
      }

      .auth-subtitle {
        font-size: 14px;
        color: var(--text-muted, #6c7086);
        letter-spacing: 1px;
      }

      .auth-form {
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

      .code-input {
        text-align: center;
        font-size: 24px;
        letter-spacing: 8px;
        font-weight: 700;
      }

      .auth-button {
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

      .auth-button:hover:not(:disabled) {
        opacity: 0.9;
      }

      .auth-button:disabled {
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
        to { transform: rotate(360deg); }
      }

      .error-message {
        color: #f38ba8;
        font-size: 13px;
        text-align: center;
      }

      .success-message {
        color: #a6e3a1;
        font-size: 13px;
        text-align: center;
      }

      .info-text {
        color: var(--text-muted, #6c7086);
        font-size: 13px;
        text-align: center;
        line-height: 1.5;
      }

      .info-text strong {
        color: var(--text-primary, #cdd6f4);
      }

      .auth-links {
        display: flex;
        justify-content: center;
        margin-top: 4px;
      }

      .auth-links a, .action-link {
        color: var(--text-muted, #6c7086);
        font-size: 13px;
        text-decoration: none;
        cursor: pointer;
        transition: color 0.15s ease;
      }

      .auth-links a:hover, .action-link:hover {
        color: var(--accent-color, #cba6f7);
      }
    `,
  ],
})
export class RegisterComponent implements OnInit {
  private readonly userService = inject(UserService);
  private readonly router = inject(Router);

  displayName = '';
  email = '';
  password = '';
  confirmPassword = '';
  code = '';

  readonly step = signal<'register' | 'verify'>('register');
  readonly loading = signal(false);
  readonly error = signal('');
  readonly success = signal('');

  ngOnInit(): void {}

  canRegister(): boolean {
    return (
      this.displayName.trim().length > 0 &&
      this.email.trim().length > 0 &&
      this.password.length >= 8 &&
      this.password === this.confirmPassword
    );
  }

  register(): void {
    if (!this.canRegister() || this.loading()) return;

    if (this.password !== this.confirmPassword) {
      this.error.set('Passwords do not match');
      return;
    }

    this.loading.set(true);
    this.error.set('');

    this.userService.register(this.email.trim(), this.password, this.displayName.trim()).subscribe({
      next: () => {
        this.loading.set(false);
        this.step.set('verify');
      },
      error: (err: HttpErrorResponse) => {
        this.loading.set(false);
        this.error.set(err.error?.detail || 'Registration failed. Please try again.');
      },
    });
  }

  verify(): void {
    if (this.code.length !== 6 || this.loading()) return;

    this.loading.set(true);
    this.error.set('');
    this.success.set('');

    this.userService.verifyEmail(this.email.trim(), this.code.trim()).subscribe({
      next: () => {
        this.loading.set(false);
        this.router.navigate(['/login']);
      },
      error: (err: HttpErrorResponse) => {
        this.loading.set(false);
        this.error.set(err.error?.detail || 'Verification failed. Please try again.');
      },
    });
  }

  resend(): void {
    this.error.set('');
    this.success.set('');

    this.userService.resendVerification(this.email.trim()).subscribe({
      next: (res) => {
        this.success.set(res.message || 'Code sent.');
      },
      error: (err: HttpErrorResponse) => {
        this.error.set(err.error?.detail || 'Could not resend code.');
      },
    });
  }
}
