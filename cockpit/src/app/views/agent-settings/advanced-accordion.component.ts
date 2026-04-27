import {Component, computed, effect, inject, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {readConfigPath, resolveMatrixForModel, SettingsMode} from './agent-settings.types';
import {getReasoningOptions} from './reasoning-options';
import {UserService} from '../../core/services/user.service';

/**
 * Advanced settings tab: collapsible accordion sections for power-user settings.
 * One level of accordion only (no nesting). Includes the read-only resolved config viewer.
 */
@Component({
  selector: 'app-advanced-accordion',
  standalone: true,
    imports: [FormsModule, TranslocoPipe, AppIconComponent],
  template: `
    <div class="advanced-container">
      <!-- Inference Parameters -->
      <div class="accordion-section" [class.expanded]="expanded().has('inference')">
        <button type="button" class="accordion-header" (click)="toggleSection('inference')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('inference') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.inference' | transloco }}
          @if (inferenceModifiedCount() > 0) {
            <span class="modified-badge">{{ inferenceModifiedCount() }}</span>
          }
        </button>
        @if (expanded().has('inference')) {
          <div class="accordion-body">
            @if (mode() === 'job') {
              <!-- Strategic phase -->
              <div class="phase-section">
                <span class="phase-label">{{ 'advanced.phases.strategic' | transloco }}</span>
                <div class="field-row" [class.modified]="strategicReasoning() !== null">
                  <label class="field-label">{{ 'advanced.labels.reasoning' | transloco }}</label>
                  <div class="field-control">
                    <select class="form-input"
                      [ngModel]="strategicReasoning() ?? resolvedStrategicReasoning()"
                      (ngModelChange)="onStrategicReasoningChange($event)"
                      [disabled]="disabled()">
                      @for (opt of strategicReasoningOptions(); track opt.value) {
                        @if (opt.value === null) {
                          <option [ngValue]="null">{{ opt.label }}</option>
                        } @else {
                          <option [value]="opt.value">{{ opt.label }}</option>
                        }
                      }
                    </select>
                    @if (strategicReasoning() !== null) {
                      <button type="button" class="reset-btn" (click)="strategicReasoning.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                    }
                  </div>
                </div>
                <div class="field-row" [class.modified]="strategicTemperature() !== null">
                  <label class="field-label">
                    {{ 'advanced.labels.temperature' | transloco: {value: effectiveStrategicTemp()} }}
                  </label>
                  <div class="slider-row">
                    <span class="slider-label">0</span>
                    <input type="range" class="form-range" min="0" max="2" step="0.1"
                      [ngModel]="strategicTemperature() ?? resolvedStrategicTemp()"
                      (ngModelChange)="onStrategicTempChange($event)"
                      [disabled]="disabled()">
                    <span class="slider-label">2</span>
                    @if (strategicTemperature() !== null) {
                      <button type="button" class="reset-btn" (click)="strategicTemperature.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                    }
                  </div>
                </div>
                <div class="field-row toggle-row" [class.modified]="strategicMultimodal() !== null">
                  <label class="toggle-label">
                    <input type="checkbox"
                      [checked]="strategicMultimodal() ?? resolvedStrategicMultimodal()"
                      (change)="onStrategicMultimodalChange($event)"
                      [disabled]="disabled()">
                    <span>{{ 'advanced.labels.multimodal' | transloco }}</span>
                  </label>
                  @if (strategicMultimodal() !== null) {
                    <button type="button" class="reset-btn" (click)="strategicMultimodal.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <!-- Tactical phase -->
              <div class="phase-section">
                <span class="phase-label">{{ 'advanced.phases.tactical' | transloco }}</span>
                <div class="field-row" [class.modified]="tacticalReasoning() !== null">
                  <label class="field-label">{{ 'advanced.labels.reasoning' | transloco }}</label>
                  <div class="field-control">
                    <select class="form-input"
                      [ngModel]="tacticalReasoning() ?? resolvedTacticalReasoning()"
                      (ngModelChange)="onTacticalReasoningChange($event)"
                      [disabled]="disabled()">
                      @for (opt of tacticalReasoningOptions(); track opt.value) {
                        @if (opt.value === null) {
                          <option [ngValue]="null">{{ opt.label }}</option>
                        } @else {
                          <option [value]="opt.value">{{ opt.label }}</option>
                        }
                      }
                    </select>
                    @if (tacticalReasoning() !== null) {
                      <button type="button" class="reset-btn" (click)="tacticalReasoning.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                    }
                  </div>
                </div>
                <div class="field-row" [class.modified]="tacticalTemperature() !== null">
                  <label class="field-label">
                    {{ 'advanced.labels.temperature' | transloco: {value: effectiveTacticalTemp()} }}
                  </label>
                  <div class="slider-row">
                    <span class="slider-label">0</span>
                    <input type="range" class="form-range" min="0" max="2" step="0.1"
                      [ngModel]="tacticalTemperature() ?? resolvedTacticalTemp()"
                      (ngModelChange)="onTacticalTempChange($event)"
                      [disabled]="disabled()">
                    <span class="slider-label">2</span>
                    @if (tacticalTemperature() !== null) {
                      <button type="button" class="reset-btn" (click)="tacticalTemperature.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                    }
                  </div>
                </div>
                <div class="field-row toggle-row" [class.modified]="tacticalMultimodal() !== null">
                  <label class="toggle-label">
                    <input type="checkbox"
                      [checked]="tacticalMultimodal() ?? resolvedTacticalMultimodal()"
                      (change)="onTacticalMultimodalChange($event)"
                      [disabled]="disabled()">
                    <span>{{ 'advanced.labels.multimodal' | transloco }}</span>
                  </label>
                  @if (tacticalMultimodal() !== null) {
                    <button type="button" class="reset-btn" (click)="tacticalMultimodal.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
            } @else {
              <!-- Session: single model inference settings -->
              <div class="field-row" [class.modified]="sessionReasoning() !== null">
                <label class="field-label">{{ 'advanced.labels.reasoning' | transloco }}</label>
                <div class="field-control">
                  <select class="form-input"
                    [ngModel]="sessionReasoning() ?? resolvedSessionReasoning()"
                    (ngModelChange)="onSessionReasoningChange($event)"
                    [disabled]="disabled()">
                    @for (opt of sessionReasoningOptions(); track opt.value) {
                      @if (opt.value === null) {
                        <option [ngValue]="null">{{ opt.label }}</option>
                      } @else {
                        <option [value]="opt.value">{{ opt.label }}</option>
                      }
                    }
                  </select>
                  @if (sessionReasoning() !== null) {
                    <button type="button" class="reset-btn" (click)="sessionReasoning.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <div class="field-row" [class.modified]="sessionTemperature() !== null">
                <label class="field-label">
                  {{ 'advanced.labels.temperature' | transloco: {value: effectiveSessionTemp()} }}
                </label>
                <div class="slider-row">
                  <span class="slider-label">0</span>
                  <input type="range" class="form-range" min="0" max="2" step="0.1"
                    [ngModel]="sessionTemperature() ?? resolvedSessionTemp()"
                    (ngModelChange)="onSessionTempChange($event)"
                    [disabled]="disabled()">
                  <span class="slider-label">2</span>
                  @if (sessionTemperature() !== null) {
                    <button type="button" class="reset-btn" (click)="sessionTemperature.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <div class="field-row toggle-row" [class.modified]="sessionMultimodal() !== null">
                <label class="toggle-label">
                  <input type="checkbox"
                    [checked]="sessionMultimodal() ?? resolvedSessionMultimodal()"
                    (change)="onSessionMultimodalChange($event)"
                    [disabled]="disabled()">
                  <span>{{ 'advanced.labels.multimodal' | transloco }}</span>
                </label>
                @if (sessionMultimodal() !== null) {
                  <button type="button" class="reset-btn" (click)="sessionMultimodal.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            }

            <!-- Shared inference params -->
            <div class="shared-params">
              <div class="field-row" [class.modified]="topP() !== null">
                <label class="field-label">{{ 'advanced.labels.topP' | transloco }}</label>
                <div class="field-control">
                  <input type="number" class="form-input compact-input" min="0" max="1" step="0.05"
                    [ngModel]="topP() ?? resolvedTopP()"
                    (ngModelChange)="onTopPChange($event)"
                    [disabled]="disabled()"
                    [placeholder]="'advanced.hints.autoPlaceholder' | transloco">
                  @if (topP() !== null) {
                    <button type="button" class="reset-btn" (click)="topP.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <div class="field-row" [class.modified]="topK() !== null">
                <label class="field-label">{{ 'advanced.labels.topK' | transloco }}</label>
                <div class="field-control">
                  <input type="number" class="form-input compact-input" min="0" step="1"
                    [ngModel]="topK() ?? resolvedTopK()"
                    (ngModelChange)="onTopKChange($event)"
                    [disabled]="disabled()"
                    [placeholder]="'advanced.hints.autoPlaceholder' | transloco">
                  @if (topK() !== null) {
                    <button type="button" class="reset-btn" (click)="topK.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <div class="field-row" [class.modified]="maxOutputTokens() !== null">
                <label class="field-label">{{ 'advanced.labels.maxOutputTokens' | transloco }}</label>
                <div class="field-control">
                  <input type="number" class="form-input compact-input" min="1" step="100"
                    [ngModel]="maxOutputTokens() ?? resolvedMaxOutputTokens()"
                    (ngModelChange)="onMaxOutputTokensChange($event)"
                    [disabled]="disabled()"
                    [placeholder]="'advanced.hints.autoPlaceholder' | transloco">
                  @if (maxOutputTokens() !== null) {
                    <button type="button" class="reset-btn" (click)="maxOutputTokens.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <div class="field-row toggle-row" [class.modified]="parallelToolCalls() !== null">
                <label class="toggle-label">
                  <input type="checkbox"
                    [checked]="parallelToolCalls() ?? resolvedParallelToolCalls()"
                    (change)="onParallelToolCallsChange($event)"
                    [disabled]="disabled()">
                  <span>{{ 'advanced.labels.parallelToolCalls' | transloco }}</span>
                </label>
                @if (parallelToolCalls() !== null) {
                  <button type="button" class="reset-btn" (click)="parallelToolCalls.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
          </div>
        }
      </div>

      <!-- Limits & Safety -->
      <div class="accordion-section" [class.expanded]="expanded().has('limits')">
        <button type="button" class="accordion-header" (click)="toggleSection('limits')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('limits') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.limits' | transloco }}
        </button>
        @if (expanded().has('limits')) {
          <div class="accordion-body">
            <div class="field-row" [class.modified]="messageCountThreshold() !== null">
              <label class="field-label">{{ 'advanced.labels.messageCountThreshold' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="1"
                  [ngModel]="messageCountThreshold() ?? resolvedMessageCountThreshold()"
                  (ngModelChange)="messageCountThreshold.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (messageCountThreshold() !== null) {
                  <button type="button" class="reset-btn" (click)="messageCountThreshold.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row" [class.modified]="toolRetryCount() !== null">
              <label class="field-label">{{ 'advanced.labels.toolRetryCount' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="0" max="10"
                  [ngModel]="toolRetryCount() ?? resolvedToolRetryCount()"
                  (ngModelChange)="toolRetryCount.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (toolRetryCount() !== null) {
                  <button type="button" class="reset-btn" (click)="toolRetryCount.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row" [class.modified]="progressStallThreshold() !== null">
              <label class="field-label">{{ 'advanced.labels.progressStallThreshold' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="1"
                  [ngModel]="progressStallThreshold() ?? resolvedProgressStallThreshold()"
                  (ngModelChange)="progressStallThreshold.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (progressStallThreshold() !== null) {
                  <button type="button" class="reset-btn" (click)="progressStallThreshold.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row" [class.modified]="maxToolCallsPerPhase() !== null">
              <label class="field-label">{{ 'advanced.labels.maxToolCallsPerPhase' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="1"
                  [ngModel]="maxToolCallsPerPhase() ?? resolvedMaxToolCallsPerPhase()"
                  (ngModelChange)="maxToolCallsPerPhase.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (maxToolCallsPerPhase() !== null) {
                  <button type="button" class="reset-btn" (click)="maxToolCallsPerPhase.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
          </div>
        }
      </div>

      <!-- Memory Tuning -->
      <div class="accordion-section" [class.expanded]="expanded().has('memory')">
        <button type="button" class="accordion-header" (click)="toggleSection('memory')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('memory') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.memory' | transloco }}
        </button>
        @if (expanded().has('memory')) {
          <div class="accordion-body">
            <div class="field-row toggle-row" [class.modified]="memoryEnabled() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="memoryEnabled() ?? resolvedMemoryEnabled()"
                  (change)="onMemoryEnabledChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.memoryEnabled' | transloco }}</span>
              </label>
              @if (memoryEnabled() !== null) {
                <button type="button" class="reset-btn" (click)="memoryEnabled.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
            <div class="field-row" [class.modified]="memoryBudget() !== null">
              <label class="field-label">{{ 'advanced.labels.budgetTokens' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="0" step="1000"
                  [ngModel]="memoryBudget() ?? resolvedMemoryBudget()"
                  (ngModelChange)="memoryBudget.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (memoryBudget() !== null) {
                  <button type="button" class="reset-btn" (click)="memoryBudget.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
          </div>
        }
      </div>

      <!-- Context Management -->
      <div class="accordion-section" [class.expanded]="expanded().has('context')">
        <button type="button" class="accordion-header" (click)="toggleSection('context')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('context') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.context' | transloco }}
        </button>
        @if (expanded().has('context')) {
          <div class="accordion-body">
            <div class="field-row toggle-row" [class.modified]="compactOnArchive() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="compactOnArchive() ?? resolvedCompactOnArchive()"
                  (change)="onCompactOnArchiveChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.compactOnArchive' | transloco }}</span>
              </label>
              @if (compactOnArchive() !== null) {
                <button type="button" class="reset-btn" (click)="compactOnArchive.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
            <div class="field-row" [class.modified]="keepRecentToolResults() !== null">
              <label class="field-label">{{ 'advanced.labels.keepRecentToolResults' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="0"
                  [ngModel]="keepRecentToolResults() ?? resolvedKeepRecentToolResults()"
                  (ngModelChange)="keepRecentToolResults.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (keepRecentToolResults() !== null) {
                  <button type="button" class="reset-btn" (click)="keepRecentToolResults.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row" [class.modified]="keepRecentMessages() !== null">
              <label class="field-label">{{ 'advanced.labels.keepRecentMessages' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="0"
                  [ngModel]="keepRecentMessages() ?? resolvedKeepRecentMessages()"
                  (ngModelChange)="keepRecentMessages.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (keepRecentMessages() !== null) {
                  <button type="button" class="reset-btn" (click)="keepRecentMessages.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
          </div>
        }
      </div>

      <!-- Workspace -->
      <div class="accordion-section" [class.expanded]="expanded().has('workspace')">
        <button type="button" class="accordion-header" (click)="toggleSection('workspace')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('workspace') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.workspace' | transloco }}
        </button>
        @if (expanded().has('workspace')) {
          <div class="accordion-body">
            <div class="field-row" [class.modified]="workspaceBackend() !== null">
              <label class="field-label">{{ 'advanced.labels.backend' | transloco }}</label>
              <div class="field-control">
                <select class="form-input"
                  [ngModel]="workspaceBackend() ?? resolvedWorkspaceBackend()"
                  (ngModelChange)="workspaceBackend.set($event); emitChange()"
                  [disabled]="disabled()">
                  <option value="sandbox">{{ 'advanced.options.container' | transloco }}</option>
                  @if (canUseVm()) {
                    <option value="vm">{{ 'advanced.options.vmQemu' | transloco }}</option>
                  }
                </select>
                @if (workspaceBackend() !== null) {
                  <button type="button" class="reset-btn" (click)="workspaceBackend.set(null); vmCpuCores.set(null); vmMemory.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            @if ((workspaceBackend() ?? resolvedWorkspaceBackend()) === 'vm') {
              <div class="field-row" [class.modified]="vmCpuCores() !== null">
                <label class="field-label">{{ 'advanced.labels.vmCpuCores' | transloco }}</label>
                <div class="field-control">
                  <input type="number" class="form-input compact-input" min="1" max="16" step="1"
                    [ngModel]="vmCpuCores() ?? resolvedVmCpuCores()"
                    (ngModelChange)="vmCpuCores.set($event); emitChange()"
                    [disabled]="disabled()">
                  @if (vmCpuCores() !== null) {
                    <button type="button" class="reset-btn" (click)="vmCpuCores.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <div class="field-row" [class.modified]="vmMemory() !== null">
                <label class="field-label">{{ 'advanced.labels.vmMemory' | transloco }}</label>
                <div class="field-control">
                  <input type="text" class="form-input compact-input" placeholder="4Gi"
                    [ngModel]="vmMemory() ?? resolvedVmMemory()"
                    (ngModelChange)="vmMemory.set($event); emitChange()"
                    [disabled]="disabled()">
                  @if (vmMemory() !== null) {
                    <button type="button" class="reset-btn" (click)="vmMemory.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
            }
            <div class="field-row" [class.modified]="maxReadWords() !== null">
              <label class="field-label">{{ 'advanced.labels.maxReadWords' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="0" step="1000"
                  [ngModel]="maxReadWords() ?? resolvedMaxReadWords()"
                  (ngModelChange)="maxReadWords.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (maxReadWords() !== null) {
                  <button type="button" class="reset-btn" (click)="maxReadWords.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row" [class.modified]="maxWriteWords() !== null">
              <label class="field-label">{{ 'advanced.labels.maxWriteWords' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="0" step="1000"
                  [ngModel]="maxWriteWords() ?? resolvedMaxWriteWords()"
                  (ngModelChange)="maxWriteWords.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (maxWriteWords() !== null) {
                  <button type="button" class="reset-btn" (click)="maxWriteWords.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row toggle-row" [class.modified]="gitVersioning() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="gitVersioning() ?? resolvedGitVersioning()"
                  (change)="onGitVersioningChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.gitVersioning' | transloco }}</span>
              </label>
              @if (gitVersioning() !== null) {
                <button type="button" class="reset-btn" (click)="gitVersioning.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
          </div>
        }
      </div>

      <!-- Shell -->
      <div class="accordion-section" [class.expanded]="expanded().has('shell')">
        <button type="button" class="accordion-header" (click)="toggleSection('shell')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('shell') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.shell' | transloco }}
        </button>
        @if (expanded().has('shell')) {
          <div class="accordion-body">
            <div class="field-row" [class.modified]="shellMode() !== null">
              <label class="field-label">{{ 'advanced.labels.mode' | transloco }}</label>
              <div class="field-control">
                <select class="form-input"
                  [ngModel]="shellMode() ?? resolvedShellMode()"
                  (ngModelChange)="shellMode.set($event); emitChange()"
                  [disabled]="disabled()">
                  <option value="stateless">{{ 'advanced.options.stateless' | transloco }}</option>
                  <option value="persistent">{{ 'advanced.options.persistent' | transloco }}</option>
                </select>
                @if (shellMode() !== null) {
                  <button type="button" class="reset-btn" (click)="shellMode.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row toggle-row" [class.modified]="shellSandbox() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="shellSandbox() ?? resolvedShellSandbox()"
                  (change)="onShellSandboxChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.sandbox' | transloco }}</span>
              </label>
              @if (shellSandbox() !== null) {
                <button type="button" class="reset-btn" (click)="shellSandbox.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
            <div class="field-row" [class.modified]="shellTimeout() !== null">
              <label class="field-label">{{ 'advanced.labels.defaultTimeout' | transloco }}</label>
              <div class="field-control">
                <input type="number" class="form-input compact-input" min="1"
                  [ngModel]="shellTimeout() ?? resolvedShellTimeout()"
                  (ngModelChange)="shellTimeout.set($event); emitChange()"
                  [disabled]="disabled()">
                @if (shellTimeout() !== null) {
                  <button type="button" class="reset-btn" (click)="shellTimeout.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
            <div class="field-row" [class.modified]="sudoAction() !== null">
              <label class="field-label">{{ 'advanced.labels.sudoAction' | transloco }}</label>
              <div class="field-control">
                <select class="form-input"
                  [ngModel]="sudoAction() ?? resolvedSudoAction()"
                  (ngModelChange)="sudoAction.set($event); emitChange()"
                  [disabled]="disabled()">
                  <option value="freeze">{{ 'advanced.options.sudoFreeze' | transloco }}</option>
                  <option value="block">{{ 'advanced.options.sudoBlock' | transloco }}</option>
                  <option value="allow">{{ 'advanced.options.sudoAllow' | transloco }}</option>
                </select>
                @if (sudoAction() !== null) {
                  <button type="button" class="reset-btn" (click)="sudoAction.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                }
              </div>
            </div>
          </div>
        }
      </div>

      <!-- Research & Browser -->
      <div class="accordion-section" [class.expanded]="expanded().has('research')">
        <button type="button" class="accordion-header" (click)="toggleSection('research')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('research') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.research' | transloco }}
        </button>
        @if (expanded().has('research')) {
          <div class="accordion-body">
            <div class="field-row toggle-row" [class.modified]="proxyEnabled() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="proxyEnabled() ?? resolvedProxyEnabled()"
                  (change)="onProxyEnabledChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.proxyEnabled' | transloco }}</span>
              </label>
              @if (proxyEnabled() !== null) {
                <button type="button" class="reset-btn" (click)="proxyEnabled.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
            <div class="field-row toggle-row" [class.modified]="browserHeadless() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="browserHeadless() ?? resolvedBrowserHeadless()"
                  (change)="onBrowserHeadlessChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.browserHeadless' | transloco }}</span>
              </label>
              @if (browserHeadless() !== null) {
                <button type="button" class="reset-btn" (click)="browserHeadless.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
            <div class="field-row toggle-row" [class.modified]="browserVision() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="browserVision() ?? resolvedBrowserVision()"
                  (change)="onBrowserVisionChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.browserVision' | transloco }}</span>
              </label>
              @if (browserVision() !== null) {
                <button type="button" class="reset-btn" (click)="browserVision.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
          </div>
        }
      </div>

      <!-- Auxiliary LLM -->
      <div class="accordion-section" [class.expanded]="expanded().has('auxiliary')">
        <button type="button" class="accordion-header" (click)="toggleSection('auxiliary')">
          <app-icon size="md" class="accordion-icon">{{ expanded().has('auxiliary') ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.auxiliary' | transloco }}
        </button>
        @if (expanded().has('auxiliary')) {
          <div class="accordion-body">
            <div class="field-row toggle-row" [class.modified]="auxEnabled() !== null">
              <label class="toggle-label">
                <input type="checkbox"
                  [checked]="auxEnabled() ?? resolvedAuxEnabled()"
                  (change)="onAuxEnabledChange($event)"
                  [disabled]="disabled()">
                <span>{{ 'advanced.labels.auxEnabled' | transloco }}</span>
              </label>
              @if (auxEnabled() !== null) {
                <button type="button" class="reset-btn" (click)="auxEnabled.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
              }
            </div>
            @if (effectiveAuxEnabled()) {
              <div class="field-row" [class.modified]="auxModel() !== null">
                <label class="field-label">{{ 'advanced.labels.model' | transloco }}</label>
                <div class="field-control">
                  <input type="text" class="form-input"
                    [ngModel]="auxModel() ?? resolvedAuxModel()"
                    (ngModelChange)="auxModel.set($event); emitChange()"
                    [disabled]="disabled()"
                    placeholder="RedHatAI/gemma-4-31B-it-FP8-Dynamic">
                  @if (auxModel() !== null) {
                    <button type="button" class="reset-btn" (click)="auxModel.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
              <div class="field-row" [class.modified]="auxTemperature() !== null">
                <label class="field-label">
                  {{ 'advanced.labels.temperature' | transloco: {value: effectiveAuxTemp()} }}
                </label>
                <div class="slider-row">
                  <span class="slider-label">0</span>
                  <input type="range" class="form-range" min="0" max="2" step="0.1"
                    [ngModel]="auxTemperature() ?? resolvedAuxTemperature()"
                    (ngModelChange)="auxTemperature.set(clampTemp($event)); emitChange()"
                    [disabled]="disabled()">
                  <span class="slider-label">2</span>
                  @if (auxTemperature() !== null) {
                    <button type="button" class="reset-btn" (click)="auxTemperature.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
            }
          </div>
        }
      </div>

      <!-- Session-specific: idle timeout, greeting, claude code -->
      @if (mode() === 'session') {
        <div class="accordion-section" [class.expanded]="expanded().has('session')">
          <button type="button" class="accordion-header" (click)="toggleSection('session')">
            <app-icon size="md" class="accordion-icon">{{ expanded().has('session') ? 'expand_less' : 'expand_more' }}</app-icon>
            {{ 'advanced.sections.session' | transloco }}
          </button>
          @if (expanded().has('session')) {
            <div class="accordion-body">
              <div class="field-row" [class.modified]="idleTimeout() !== null">
                <label class="field-label">{{ 'advanced.labels.idleTimeout' | transloco }}</label>
                <div class="field-control">
                  <input type="number" class="form-input compact-input" min="0"
                    [ngModel]="idleTimeout() ?? resolvedIdleTimeout()"
                    (ngModelChange)="idleTimeout.set($event); emitChange()"
                    [disabled]="disabled()">
                  @if (idleTimeout() !== null) {
                    <button type="button" class="reset-btn" (click)="idleTimeout.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
                <span class="field-hint">{{ 'advanced.hints.idleDisabled' | transloco }}</span>
              </div>
              <div class="field-row" [class.modified]="greeting() !== null">
                <label class="field-label">{{ 'advanced.labels.greeting' | transloco }}</label>
                <div class="field-control">
                  <input type="text" class="form-input"
                    [ngModel]="greeting() ?? resolvedGreeting()"
                    (ngModelChange)="greeting.set($event); emitChange()"
                    [disabled]="disabled()">
                  @if (greeting() !== null) {
                    <button type="button" class="reset-btn" (click)="greeting.set(null); emitChange()"><app-icon size="xs">close</app-icon></button>
                  }
                </div>
              </div>
            </div>
          }
        </div>
      }

      <!-- Resolved Config Viewer -->
      <div class="config-viewer-section">
        <button type="button" class="accordion-header" (click)="showResolvedConfig.set(!showResolvedConfig())">
          <app-icon size="md" class="accordion-icon">{{ showResolvedConfig() ? 'expand_less' : 'expand_more' }}</app-icon>
          {{ 'advanced.sections.viewResolvedConfig' | transloco }}
        </button>
        @if (showResolvedConfig()) {
          <div class="config-viewer">
            <pre class="config-json">{{ resolvedConfigJson() }}</pre>
          </div>
        }
      </div>
    </div>
  `,
  styles: [`
    .advanced-container {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .accordion-section {
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      overflow: hidden;
    }
    .accordion-section.expanded {
      border-color: var(--border-color, #45475a);
    }
    .accordion-header {
      display: flex;
      align-items: center;
      gap: 8px;
      width: 100%;
      padding: 10px 12px;
      border: none;
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      text-align: left;
      transition: background 0.15s;
    }
    .accordion-header:hover {
      background: rgba(255, 255, 255, 0.05);
    }
    .accordion-icon {
      color: var(--text-muted, #6c7086);
    }
    .modified-badge {
      margin-left: auto;
      padding: 1px 6px;
      border-radius: 8px;
      background: rgba(203, 166, 247, 0.15);
      color: var(--accent-color, #cba6f7);
      font-size: 11px;
      font-weight: 600;
    }
    .accordion-body {
      padding: 12px 14px;
      background: var(--panel-bg, #181825);
    }
    .phase-section {
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border-color, #313244);
    }
    .phase-section:last-of-type {
      border-bottom: none;
      margin-bottom: 8px;
    }
    .phase-label {
      display: block;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted, #6c7086);
      margin-bottom: 8px;
    }
    .shared-params {
      padding-top: 8px;
      border-top: 1px solid var(--border-color, #313244);
    }
    .field-row {
      margin-bottom: 10px;
      padding-left: 8px;
      border-left: 2px solid transparent;
      transition: border-color 0.15s;
    }
    .field-row.modified {
      border-left-color: var(--accent-color, #cba6f7);
    }
    .field-label {
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-primary, #cdd6f4);
      margin-bottom: 4px;
    }
    .field-control {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .field-hint {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 2px;
    }
    .form-input {
      flex: 1;
      padding: 7px 10px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 6px;
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      font-family: inherit;
      font-size: 13px;
    }
    .form-input:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
    }
    .form-input:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .compact-input {
      max-width: 120px;
    }
    .slider-row {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .slider-label {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      min-width: 12px;
      text-align: center;
    }
    .form-range {
      flex: 1;
      accent-color: var(--accent-color, #cba6f7);
    }
    .toggle-row {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .toggle-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      color: var(--text-primary, #cdd6f4);
      cursor: pointer;
    }
    .toggle-label input[type="checkbox"] {
      accent-color: var(--accent-color, #cba6f7);
    }
    .reset-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border: none;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.08);
      color: var(--text-muted, #6c7086);
      cursor: pointer;
      flex-shrink: 0;
    }
    .reset-btn:hover {
      background: var(--danger-tint);
      color: var(--danger);
    }
    .config-viewer-section {
      margin-top: 12px;
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      overflow: hidden;
    }
    .config-viewer {
      max-height: 300px;
      overflow: auto;
      background: var(--surface-0, #313244);
    }
    .config-json {
      padding: 12px 14px;
      margin: 0;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      line-height: 1.5;
      color: var(--text-primary, #cdd6f4);
      white-space: pre-wrap;
      word-break: break-all;
    }
  `],
})
export class AdvancedAccordionComponent {
  config = input<Record<string, unknown>>({});
  mode = input<SettingsMode>('job');
  disabled = input(false);
  /** Raw settings_matrix for model-family-aware defaults. */
  settingsMatrix = input<Record<string, Record<string, unknown>>>({});
  /** The currently selected model(s), used for reasoning options. */
  strategicModelOverride = input<string | null>(null);
  tacticalModelOverride = input<string | null>(null);
  sessionModelOverride = input<string | null>(null);

  change = output<void>();

  // Accordion state
  readonly expanded = signal<Set<string>>(new Set());
  readonly showResolvedConfig = signal(false);

  // --- Inference params ---
  readonly strategicReasoning = signal<string | null>(null);
  readonly strategicTemperature = signal<number | null>(null);
  readonly strategicMultimodal = signal<boolean | null>(null);
  readonly tacticalReasoning = signal<string | null>(null);
  readonly tacticalTemperature = signal<number | null>(null);
  readonly tacticalMultimodal = signal<boolean | null>(null);
  readonly sessionReasoning = signal<string | null>(null);
  readonly sessionTemperature = signal<number | null>(null);
  readonly sessionMultimodal = signal<boolean | null>(null);
  readonly topP = signal<number | null>(null);
  readonly topK = signal<number | null>(null);
  readonly maxOutputTokens = signal<number | null>(null);
  readonly parallelToolCalls = signal<boolean | null>(null);

  // --- Limits ---
  readonly messageCountThreshold = signal<number | null>(null);
  readonly toolRetryCount = signal<number | null>(null);
  readonly progressStallThreshold = signal<number | null>(null);
  readonly maxToolCallsPerPhase = signal<number | null>(null);

  // --- Memory ---
  readonly memoryEnabled = signal<boolean | null>(null);
  readonly memoryBudget = signal<number | null>(null);

  // --- Context ---
  readonly compactOnArchive = signal<boolean | null>(null);
  readonly keepRecentToolResults = signal<number | null>(null);
  readonly keepRecentMessages = signal<number | null>(null);

  // --- Workspace ---
  private readonly userService = inject(UserService);
  /** Whether the current user is allowed to pick the VM backend. Admins always qualify. */
  readonly canUseVm = computed(() => {
    const u = this.userService.currentUser();
    return !!(u?.is_admin || u?.can_use_vm);
  });
  readonly workspaceBackend = signal<string | null>(null);
  readonly vmCpuCores = signal<number | null>(null);
  readonly vmMemory = signal<string | null>(null);
  readonly maxReadWords = signal<number | null>(null);
  readonly maxWriteWords = signal<number | null>(null);
  readonly gitVersioning = signal<boolean | null>(null);

  // --- Shell ---
  readonly shellMode = signal<string | null>(null);
  readonly shellSandbox = signal<boolean | null>(null);
  readonly shellTimeout = signal<number | null>(null);
  readonly sudoAction = signal<string | null>(null);

  // --- Research ---
  readonly proxyEnabled = signal<boolean | null>(null);
  readonly browserHeadless = signal<boolean | null>(null);
  readonly browserVision = signal<boolean | null>(null);

  // --- Auxiliary ---
  readonly auxEnabled = signal<boolean | null>(null);
  readonly auxModel = signal<string | null>(null);
  readonly auxTemperature = signal<number | null>(null);

  // --- Session-specific ---
  readonly idleTimeout = signal<number | null>(null);
  readonly greeting = signal<string | null>(null);
  // ===== Resolved defaults =====
  // Resolution order: phase-specific config → matrix for effective model → base config → hardcoded.
  // The server sends raw config (no matrix baked in) + the raw settings_matrix.
  // The client resolves matrix values for the effective model (user override or config default).

  private r(path: string): unknown { return readConfigPath(this.config(), path); }

  /** Look up a settings_matrix value for a model. Returns null if key not found. */
  private mv(model: string | null, key: string): unknown {
    if (!model) return null;
    const resolved = resolveMatrixForModel(this.settingsMatrix(), model);
    return resolved[key] ?? null;
  }

  // Effective models: user override falls back to config default.
  private readonly effectiveStrategicModel = computed(() =>
    this.strategicModelOverride() ?? (this.r('llm.strategic.model') as string) ?? (this.r('llm.model') as string) ?? null);
  private readonly effectiveTacticalModel = computed(() =>
    this.tacticalModelOverride() ?? (this.r('llm.tactical.model') as string) ?? (this.r('llm.model') as string) ?? null);
  private readonly effectiveSessionModel = computed(() =>
    this.sessionModelOverride() ?? (this.r('llm.model') as string) ?? null);

  readonly resolvedStrategicReasoning = computed(() => (this.r('llm.strategic.reasoning_level') ?? this.r('llm.reasoning_level')) as string | null);
  readonly resolvedStrategicTemp = computed(() =>
    (this.r('llm.strategic.temperature') ?? this.mv(this.effectiveStrategicModel(), 'temperature') ?? this.r('llm.temperature') ?? 0) as number);
  readonly resolvedStrategicMultimodal = computed(() =>
    (this.r('llm.strategic.multimodal') ?? this.mv(this.effectiveStrategicModel(), 'multimodal') ?? this.r('llm.multimodal') ?? false) as boolean);
  readonly resolvedTacticalReasoning = computed(() => (this.r('llm.tactical.reasoning_level') ?? this.r('llm.reasoning_level')) as string | null);
  readonly resolvedTacticalTemp = computed(() =>
    (this.r('llm.tactical.temperature') ?? this.mv(this.effectiveTacticalModel(), 'temperature') ?? this.r('llm.temperature') ?? 0) as number);
  readonly resolvedTacticalMultimodal = computed(() =>
    (this.r('llm.tactical.multimodal') ?? this.mv(this.effectiveTacticalModel(), 'multimodal') ?? this.r('llm.multimodal') ?? false) as boolean);
  readonly resolvedSessionReasoning = computed(() => this.r('llm.reasoning_level') as string | null);
  readonly resolvedSessionTemp = computed(() =>
    (this.mv(this.effectiveSessionModel(), 'temperature') ?? this.r('llm.temperature') ?? 0) as number);
  readonly resolvedSessionMultimodal = computed(() =>
    (this.mv(this.effectiveSessionModel(), 'multimodal') ?? this.r('llm.multimodal') ?? false) as boolean);
  readonly resolvedTopP = computed(() => {
    const model = this.mode() === 'session' ? this.effectiveSessionModel() : this.effectiveStrategicModel();
    return (this.mv(model, 'top_p') ?? this.r('llm.top_p')) as number | null;
  });
  readonly resolvedTopK = computed(() => {
    const model = this.mode() === 'session' ? this.effectiveSessionModel() : this.effectiveStrategicModel();
    return (this.mv(model, 'top_k') ?? this.r('llm.top_k')) as number | null;
  });
  readonly resolvedMaxOutputTokens = computed(() => this.r('llm.max_output_tokens') as number | null);
  readonly resolvedParallelToolCalls = computed(() => {
    const model = this.mode() === 'session' ? this.effectiveSessionModel() : this.effectiveStrategicModel();
    return (this.mv(model, 'parallel_tool_calls') ?? this.r('llm.parallel_tool_calls') ?? false) as boolean;
  });

  readonly resolvedMessageCountThreshold = computed(() => (this.r('limits.message_count_threshold') ?? 300) as number);
  readonly resolvedToolRetryCount = computed(() => (this.r('limits.tool_retry_count') ?? 3) as number);
  readonly resolvedProgressStallThreshold = computed(() => (this.r('limits.progress_stall_threshold') ?? 30) as number);
  readonly resolvedMaxToolCallsPerPhase = computed(() => (this.r('limits.max_tool_calls_per_phase') ?? 200) as number);

  readonly resolvedMemoryEnabled = computed(() => (this.r('memory.enabled') ?? true) as boolean);
  readonly resolvedMemoryBudget = computed(() => (this.r('memory.budget_tokens') ?? 10000) as number);

  readonly resolvedCompactOnArchive = computed(() => (this.r('context_management.compact_on_archive') ?? true) as boolean);
  readonly resolvedKeepRecentToolResults = computed(() => (this.r('context_management.keep_recent_tool_results') ?? 150) as number);
  readonly resolvedKeepRecentMessages = computed(() => (this.r('context_management.keep_recent_messages') ?? 10) as number);

  readonly resolvedWorkspaceBackend = computed(() => (this.r('workspace.backend') ?? 'sandbox') as string);
  readonly resolvedVmCpuCores = computed(() => (this.r('workspace.vm.cpu_cores') ?? 2) as number);
  readonly resolvedVmMemory = computed(() => (this.r('workspace.vm.memory') ?? '4Gi') as string);
  readonly resolvedMaxReadWords = computed(() => (this.r('workspace.max_read_words') ?? 25000) as number);
  readonly resolvedMaxWriteWords = computed(() => (this.r('workspace.max_write_words') ?? 10000) as number);
  readonly resolvedGitVersioning = computed(() => {
    const v = this.r('workspace.git_versioning');
    return (v ?? (this.mode() === 'job')) as boolean;
  });

  readonly resolvedShellMode = computed(() => (this.r('shell.mode') ?? 'stateless') as string);
  readonly resolvedShellSandbox = computed(() => (this.r('shell.sandbox') ?? true) as boolean);
  readonly resolvedShellTimeout = computed(() => (this.r('shell.default_timeout') ?? 120) as number);
  readonly resolvedSudoAction = computed(() => (this.r('shell.sudo_action') ?? 'freeze') as string);

  readonly resolvedProxyEnabled = computed(() => (this.r('research.proxy.enabled') ?? false) as boolean);
  readonly resolvedBrowserHeadless = computed(() => (this.r('browser.headless') ?? true) as boolean);
  readonly resolvedBrowserVision = computed(() => (this.r('browser.use_vision') ?? false) as boolean);

  readonly resolvedAuxEnabled = computed(() => (this.r('auxiliary.enabled') ?? true) as boolean);
  readonly resolvedAuxModel = computed(() => (this.r('auxiliary.model') ?? 'RedHatAI/gemma-4-31B-it-FP8-Dynamic') as string);
  readonly resolvedAuxTemperature = computed(() => (this.r('auxiliary.temperature') ?? 0) as number);

  readonly resolvedIdleTimeout = computed(() => (this.r('interactive.idle_timeout_minutes') ?? 30) as number);
  readonly resolvedGreeting = computed(() => (this.r('interactive.greeting') ?? '') as string);
  // ===== Computed helpers =====
  readonly effectiveAuxEnabled = computed(() => this.auxEnabled() ?? this.resolvedAuxEnabled());
  readonly effectiveStrategicTemp = computed(() => this.strategicTemperature() ?? this.resolvedStrategicTemp());
  readonly effectiveTacticalTemp = computed(() => this.tacticalTemperature() ?? this.resolvedTacticalTemp());
  readonly effectiveSessionTemp = computed(() => this.sessionTemperature() ?? this.resolvedSessionTemp());
  readonly effectiveAuxTemp = computed(() => this.auxTemperature() ?? this.resolvedAuxTemperature());

  readonly strategicReasoningOptions = computed(() =>
    getReasoningOptions(this.strategicModelOverride() ?? (this.r('llm.strategic.model') ?? this.r('llm.model')) as string | null)
  );
  readonly tacticalReasoningOptions = computed(() =>
    getReasoningOptions(this.tacticalModelOverride() ?? (this.r('llm.tactical.model') ?? this.r('llm.model')) as string | null)
  );
  readonly sessionReasoningOptions = computed(() =>
    getReasoningOptions(this.sessionModelOverride() ?? this.r('llm.model') as string | null)
  );

  readonly inferenceModifiedCount = computed(() => {
    let c = 0;
    if (this.strategicReasoning() !== null) c++;
    if (this.strategicTemperature() !== null) c++;
    if (this.strategicMultimodal() !== null) c++;
    if (this.tacticalReasoning() !== null) c++;
    if (this.tacticalTemperature() !== null) c++;
    if (this.tacticalMultimodal() !== null) c++;
    if (this.sessionReasoning() !== null) c++;
    if (this.sessionTemperature() !== null) c++;
    if (this.sessionMultimodal() !== null) c++;
    if (this.topP() !== null) c++;
    if (this.topK() !== null) c++;
    if (this.maxOutputTokens() !== null) c++;
    if (this.parallelToolCalls() !== null) c++;
    return c;
  });

  readonly resolvedConfigJson = computed(() => {
    try {
      return JSON.stringify(this.config(), null, 2);
    } catch {
      return '{}';
    }
  });

  // ===== Event handlers =====
  toggleSection(section: string): void {
    const next = new Set(this.expanded());
    if (next.has(section)) {
      next.delete(section);
    } else {
      next.add(section);
    }
    this.expanded.set(next);
  }

  constructor() {
    // Snap ineligible users off 'vm' so the form can't submit a denied backend.
    // The server is authoritative — this is a UX safeguard.
    effect(() => {
      if (this.canUseVm()) return;
      const effective = this.workspaceBackend() ?? this.resolvedWorkspaceBackend();
      if (effective === 'vm') {
        this.workspaceBackend.set('sandbox');
        this.vmCpuCores.set(null);
        this.vmMemory.set(null);
        this.emitChange();
      }
    });
  }

  emitChange(): void { this.change.emit(); }

  clampTemp(value: number): number {
    return Math.round(Math.min(2, Math.max(0, value)) * 10) / 10;
  }

  onStrategicReasoningChange(v: string | null): void { this.strategicReasoning.set(v); this.emitChange(); }
  onStrategicTempChange(v: number): void { this.strategicTemperature.set(this.clampTemp(v)); this.emitChange(); }
  onStrategicMultimodalChange(e: Event): void { this.strategicMultimodal.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onTacticalReasoningChange(v: string | null): void { this.tacticalReasoning.set(v); this.emitChange(); }
  onTacticalTempChange(v: number): void { this.tacticalTemperature.set(this.clampTemp(v)); this.emitChange(); }
  onTacticalMultimodalChange(e: Event): void { this.tacticalMultimodal.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onSessionReasoningChange(v: string | null): void { this.sessionReasoning.set(v); this.emitChange(); }
  onSessionTempChange(v: number): void { this.sessionTemperature.set(this.clampTemp(v)); this.emitChange(); }
  onSessionMultimodalChange(e: Event): void { this.sessionMultimodal.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onTopPChange(v: number | null): void { this.topP.set(v); this.emitChange(); }
  onTopKChange(v: number | null): void { this.topK.set(v); this.emitChange(); }
  onMaxOutputTokensChange(v: number | null): void { this.maxOutputTokens.set(v); this.emitChange(); }
  onParallelToolCallsChange(e: Event): void { this.parallelToolCalls.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onMemoryEnabledChange(e: Event): void { this.memoryEnabled.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onCompactOnArchiveChange(e: Event): void { this.compactOnArchive.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onGitVersioningChange(e: Event): void { this.gitVersioning.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onShellSandboxChange(e: Event): void { this.shellSandbox.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onProxyEnabledChange(e: Event): void { this.proxyEnabled.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onBrowserHeadlessChange(e: Event): void { this.browserHeadless.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onBrowserVisionChange(e: Event): void { this.browserVision.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  onAuxEnabledChange(e: Event): void { this.auxEnabled.set((e.target as HTMLInputElement).checked); this.emitChange(); }
  /** Build the advanced settings config_override fragment. */
  getOverrides(): Record<string, unknown> {
    const o: Record<string, unknown> = {};
    const llm: Record<string, unknown> = {};

    if (this.mode() === 'job') {
      const s: Record<string, unknown> = {};
      if (this.strategicReasoning() !== null) s['reasoning_level'] = this.strategicReasoning();
      if (this.strategicTemperature() !== null) s['temperature'] = this.strategicTemperature();
      if (this.strategicMultimodal() !== null) s['multimodal'] = this.strategicMultimodal();
      if (Object.keys(s).length) llm['strategic'] = s;

      const t: Record<string, unknown> = {};
      if (this.tacticalReasoning() !== null) t['reasoning_level'] = this.tacticalReasoning();
      if (this.tacticalTemperature() !== null) t['temperature'] = this.tacticalTemperature();
      if (this.tacticalMultimodal() !== null) t['multimodal'] = this.tacticalMultimodal();
      if (Object.keys(t).length) llm['tactical'] = t;
    } else {
      if (this.sessionReasoning() !== null) llm['reasoning_level'] = this.sessionReasoning();
      if (this.sessionTemperature() !== null) llm['temperature'] = this.sessionTemperature();
      if (this.sessionMultimodal() !== null) llm['multimodal'] = this.sessionMultimodal();
    }

    if (this.topP() !== null) llm['top_p'] = this.topP();
    if (this.topK() !== null) llm['top_k'] = this.topK();
    if (this.maxOutputTokens() !== null) llm['max_output_tokens'] = this.maxOutputTokens();
    if (this.parallelToolCalls() !== null) llm['parallel_tool_calls'] = this.parallelToolCalls();
    if (Object.keys(llm).length) o['llm'] = llm;

    // Limits
    const lim: Record<string, unknown> = {};
    if (this.messageCountThreshold() !== null) lim['message_count_threshold'] = this.messageCountThreshold();
    if (this.toolRetryCount() !== null) lim['tool_retry_count'] = this.toolRetryCount();
    if (this.progressStallThreshold() !== null) lim['progress_stall_threshold'] = this.progressStallThreshold();
    if (this.maxToolCallsPerPhase() !== null) lim['max_tool_calls_per_phase'] = this.maxToolCallsPerPhase();
    if (Object.keys(lim).length) o['limits'] = lim;

    // Memory
    const mem: Record<string, unknown> = {};
    if (this.memoryEnabled() !== null) mem['enabled'] = this.memoryEnabled();
    if (this.memoryBudget() !== null) mem['budget_tokens'] = this.memoryBudget();
    if (Object.keys(mem).length) o['memory'] = { ...(o['memory'] as any ?? {}), ...mem };

    // Context
    const ctx: Record<string, unknown> = {};
    if (this.compactOnArchive() !== null) ctx['compact_on_archive'] = this.compactOnArchive();
    if (this.keepRecentToolResults() !== null) ctx['keep_recent_tool_results'] = this.keepRecentToolResults();
    if (this.keepRecentMessages() !== null) ctx['keep_recent_messages'] = this.keepRecentMessages();
    if (Object.keys(ctx).length) o['context_management'] = ctx;

    // Workspace
    const ws: Record<string, unknown> = {};
    if (this.workspaceBackend() !== null) ws['backend'] = this.workspaceBackend();
    if (this.workspaceBackend() === 'vm') {
      const vm: Record<string, unknown> = {};
      if (this.vmCpuCores() !== null) vm['cpu_cores'] = this.vmCpuCores();
      if (this.vmMemory() !== null) vm['memory'] = this.vmMemory();
      if (Object.keys(vm).length) ws['vm'] = vm;
    }
    if (this.maxReadWords() !== null) ws['max_read_words'] = this.maxReadWords();
    if (this.maxWriteWords() !== null) ws['max_write_words'] = this.maxWriteWords();
    if (this.gitVersioning() !== null) ws['git_versioning'] = this.gitVersioning();
    if (Object.keys(ws).length) o['workspace'] = ws;

    // Shell
    const sh: Record<string, unknown> = {};
    if (this.shellMode() !== null) sh['mode'] = this.shellMode();
    if (this.shellSandbox() !== null) sh['sandbox'] = this.shellSandbox();
    if (this.shellTimeout() !== null) sh['default_timeout'] = this.shellTimeout();
    if (this.sudoAction() !== null) sh['sudo_action'] = this.sudoAction();
    if (Object.keys(sh).length) o['shell'] = sh;

    // Research & Browser
    if (this.proxyEnabled() !== null) o['research'] = { proxy: { enabled: this.proxyEnabled() } };
    const br: Record<string, unknown> = {};
    if (this.browserHeadless() !== null) br['headless'] = this.browserHeadless();
    if (this.browserVision() !== null) br['use_vision'] = this.browserVision();
    if (Object.keys(br).length) o['browser'] = br;

    // Auxiliary
    const aux: Record<string, unknown> = {};
    if (this.auxEnabled() !== null) aux['enabled'] = this.auxEnabled();
    if (this.auxModel() !== null) aux['model'] = this.auxModel();
    if (this.auxTemperature() !== null) aux['temperature'] = this.auxTemperature();
    if (Object.keys(aux).length) o['auxiliary'] = aux;

    // Session-specific
    if (this.mode() === 'session') {
      const inter: Record<string, unknown> = {};
      if (this.idleTimeout() !== null) inter['idle_timeout_minutes'] = this.idleTimeout();
      if (this.greeting() !== null) inter['greeting'] = this.greeting();
      if (Object.keys(inter).length) o['interactive'] = { ...(o['interactive'] as any ?? {}), ...inter };
    }

    return o;
  }

  /** Prefill from expert config. */
  prefillFromConfig(config: Record<string, unknown>): void {
    const llm = config['llm'] as Record<string, unknown> | undefined;
    const strat = llm?.['strategic'] as Record<string, unknown> | undefined;
    const tact = llm?.['tactical'] as Record<string, unknown> | undefined;

    this.strategicReasoning.set((strat?.['reasoning_level'] as string) ?? (llm?.['reasoning_level'] as string) ?? null);
    this.strategicTemperature.set((strat?.['temperature'] as number) ?? (llm?.['temperature'] as number) ?? null);
    this.strategicMultimodal.set((strat?.['multimodal'] as boolean) ?? (llm?.['multimodal'] as boolean) ?? null);
    this.tacticalReasoning.set((tact?.['reasoning_level'] as string) ?? (llm?.['reasoning_level'] as string) ?? null);
    this.tacticalTemperature.set((tact?.['temperature'] as number) ?? (llm?.['temperature'] as number) ?? null);
    this.tacticalMultimodal.set((tact?.['multimodal'] as boolean) ?? (llm?.['multimodal'] as boolean) ?? null);
  }

  resetAll(): void {
    this.strategicReasoning.set(null);
    this.strategicTemperature.set(null);
    this.strategicMultimodal.set(null);
    this.tacticalReasoning.set(null);
    this.tacticalTemperature.set(null);
    this.tacticalMultimodal.set(null);
    this.sessionReasoning.set(null);
    this.sessionTemperature.set(null);
    this.sessionMultimodal.set(null);
    this.topP.set(null);
    this.topK.set(null);
    this.maxOutputTokens.set(null);
    this.parallelToolCalls.set(null);
    this.messageCountThreshold.set(null);
    this.toolRetryCount.set(null);
    this.progressStallThreshold.set(null);
    this.maxToolCallsPerPhase.set(null);
    this.memoryEnabled.set(null);
    this.memoryBudget.set(null);
    this.compactOnArchive.set(null);
    this.keepRecentToolResults.set(null);
    this.keepRecentMessages.set(null);
    this.workspaceBackend.set(null);
    this.vmCpuCores.set(null);
    this.vmMemory.set(null);
    this.maxReadWords.set(null);
    this.maxWriteWords.set(null);
    this.gitVersioning.set(null);
    this.shellMode.set(null);
    this.shellSandbox.set(null);
    this.shellTimeout.set(null);
    this.sudoAction.set(null);
    this.proxyEnabled.set(null);
    this.browserHeadless.set(null);
    this.browserVision.set(null);
    this.auxEnabled.set(null);
    this.auxModel.set(null);
    this.auxTemperature.set(null);
    this.idleTimeout.set(null);
    this.greeting.set(null);
    this.expanded.set(new Set());
  }
}
