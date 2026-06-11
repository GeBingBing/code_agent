import * as vscode from 'vscode';
import { InlineCompletionProvider } from './completion';
import { ServerManager } from './server';

let provider: InlineCompletionProvider | undefined;
let serverManager: ServerManager | undefined;

export function activate(context: vscode.ExtensionContext) {
    const config = vscode.workspace.getConfiguration('codingAgent');
    const port = config.get<number>('serverPort', 18792);
    const serverKey = config.get<string>('serverKey', '');

    // Initialize server manager
    serverManager = new ServerManager(port, serverKey);

    // Register inline completion provider
    provider = new InlineCompletionProvider(serverManager);
    context.subscriptions.push(
        vscode.languages.registerInlineCompletionItemProvider(
            [{ pattern: '**' }],  // All languages
            provider
        )
    );

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('coding-agent.startServer', async () => {
            await serverManager?.start();
            vscode.window.showInformationMessage('Coding Agent server started');
        }),
        vscode.commands.registerCommand('coding-agent.stopServer', async () => {
            await serverManager?.stop();
            vscode.window.showInformationMessage('Coding Agent server stopped');
        }),
        vscode.commands.registerCommand('coding-agent.restartServer', async () => {
            await serverManager?.restart();
            vscode.window.showInformationMessage('Coding Agent server restarted');
        }),
        vscode.commands.registerCommand('coding-agent.inlineCompletion', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) { return; }
            const position = editor.selection.active;
            const doc = editor.document;
            const textBefore = doc.getText(new vscode.Range(new vscode.Position(0, 0), position));

            const completion = await provider?.getCompletion(textBefore, doc.languageId);
            if (completion) {
                const snippet = new vscode.SnippetString(completion);
                editor.insertSnippet(snippet, position);
            }
        })
    );

    // Auto-start server if configured
    const triggerMode = config.get<string>('inlineTriggerMode', 'manual');
    if (triggerMode === 'auto') {
        serverManager.start().catch(err => {
            vscode.window.showWarningMessage(`Coding Agent: Failed to start server: ${err.message}`);
        });
    }
}

export function deactivate() {
    serverManager?.stop();
}