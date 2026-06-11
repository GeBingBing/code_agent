import * as vscode from 'vscode';
import { ServerManager } from './server';
import { SSEClient, ServerEvent } from './sse';

/**
 * Inline Completion Provider for VS Code.
 * Implements the VS Code InlineCompletionItemProvider API.
 */
export class InlineCompletionProvider implements vscode.InlineCompletionProvider {
    private serverManager: ServerManager;
    private currentCompletion: string = '';

    constructor(serverManager: ServerManager) {
        this.serverManager = serverManager;
    }

    async provideInlineCompletionItems(
        document: vscode.TextDocument,
        position: vscode.Position,
        context: vscode.InlineCompletionContext,
        token: vscode.CancellationToken
    ): Promise<vscode.InlineCompletionItem[] | undefined> {
        // Check if server is running
        const healthy = await this.serverManager.checkHealth();
        if (!healthy) {
            return undefined;
        }

        // Get text before cursor (context for completion)
        const textBefore = document.getText(
            new vscode.Range(new vscode.Position(0, 0), position)
        );

        // Get the last few lines for task context
        const lines = textBefore.split('\n');
        const lastLines = lines.slice(-10).join('\n');

        // Build task description
        const language = document.languageId;
        const task = `[${language} code completion]\n${lastLines}`;

        try {
            const completion = await this.getCompletion(task, document.languageId);
            if (!completion) {
                return undefined;
            }

            this.currentCompletion = completion;
            const range = new vscode.Range(position, position);
            const item = new vscode.InlineCompletionItem(
                new vscode.SnippetString(completion),
                range,
                {
                    title: 'Coding Agent Completion',
                    command: undefined,
                }
            );
            item.insertText = completion;

            return [item];
        } catch (error) {
            console.error('Coding Agent completion error:', error);
            return undefined;
        }
    }

    async getCompletion(task: string, _languageId: string): Promise<string | undefined> {
        const client = new SSEClient(this.serverManager.port, '');

        let fullContent = '';
        try {
            for await (const event of client.connect(task)) {
                if (event.type === 'content') {
                    fullContent += event.content || '';
                } else if (event.type === 'final' || event.type === 'complete') {
                    break;
                } else if (event.type === 'error') {
                    console.error('Server error:', event.error);
                    break;
                }
            }
        } finally {
            client.abort();
        }

        // Clean up completion - remove any tool call artifacts
        // and extract just the code
        return this.cleanCompletion(fullContent);
    }

    private cleanCompletion(completion: string): string {
        // Remove any markdown code blocks
        let cleaned = completion.replace(/^```[\w]*\n?/gm, '');
        cleaned = cleaned.replace(/```$/gm, '');

        // Remove any explanatory text before/after code
        // Keep only code-like content
        const lines = cleaned.split('\n');
        const codeLines: string[] = [];
        let inCode = false;

        for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed && !trimmed.startsWith('#') && !trimmed.startsWith('//')) {
                // Likely code
                inCode = true;
            }
            if (inCode || trimmed.length > 0) {
                codeLines.push(line);
            }
        }

        return codeLines.join('\n').trim();
    }
}