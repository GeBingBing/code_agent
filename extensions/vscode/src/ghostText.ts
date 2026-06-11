/**
 * Ghost Text renderer utilities.
 * Used to render inline completions with styled ghost text.
 */

import * as vscode from 'vscode';

/**
 * Apply ghost text styling to inline completion items.
 * Ghost text is typically shown in a lighter/italic style.
 */
export function applyGhostTextStyle(
    completion: vscode.InlineCompletionItem,
    theme: vscode.ColorTheme
): void {
    // VS Code handles ghost text styling via InlineCompletionItem properties
    // The editor automatically shows completions differently based on:
    // - Whether the item was auto-triggered
    // - The complete kind (e.g., 'assistant' for AI completions)
    // This is handled internally by VS Code's completion system
}

/**
 * Determine if a completion should be shown as ghost text.
 * Auto-triggered completions from AI agents typically use ghost text.
 */
export function isGhostTextCandidate(
    triggerKind: vscode.InlineCompletionTriggerKind
): boolean {
    return triggerKind === vscode.InlineCompletionTriggerKind.Automatic;
}