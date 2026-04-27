import { Component, Input, Output, EventEmitter } from '@angular/core';
import { AppIconComponent } from '../../../ui/icon';

export type AgentStepType =
  | 'thought'
  | 'tool_call'
  | 'tool_result'
  | 'observation'
  | 'approval_request'
  | 'file_change'
  | 'inspection_result'
  | 'workspace_proposal';

export type AgentStatus = 'thinking' | 'responding' | 'complete' | 'error' | 'waiting';

export interface IAgentStep {
  id: string;
  type: AgentStepType;
  title: string;
  content: string;
  timestamp: number;
  duration?: number;
  callId?: string;
  metadata?: Record<string, unknown>;
}

@Component({
  selector: 'app-agent-steps',
  standalone: true,
  imports: [AppIconComponent],
  templateUrl: './agent-steps.component.html',
  styleUrls: ['./agent-steps.component.scss'],
})
export class AgentStepsComponent {
  @Input() steps: IAgentStep[] = [];
  @Input() status: AgentStatus = 'thinking';
  @Output() expandedChange = new EventEmitter<boolean>();

  isExpanded = false;

  get currentStep(): IAgentStep | null {
    return this.steps.length > 0 ? this.steps[this.steps.length - 1] : null;
  }

  get isProcessing(): boolean {
    return this.status === 'thinking' || this.status === 'responding';
  }

  get headerText(): string {
    if (this.isProcessing && this.currentStep) return this.currentStep.title;
    if (this.status === 'error') return 'Error occurred';
    return 'Thought process';
  }

  getStepIcon(type: AgentStepType): string {
    const icons: Record<string, string> = {
      thought: 'psychology',
      tool_call: 'build',
      tool_result: 'check_circle',
      observation: 'visibility',
      approval_request: 'gpp_maybe',
      file_change: 'difference',
      inspection_result: 'visibility',
      workspace_proposal: 'edit_note',
    };
    return icons[type] || 'circle';
  }

  toggleExpanded(): void {
    this.isExpanded = !this.isExpanded;
    this.expandedChange.emit(this.isExpanded);
  }
}
