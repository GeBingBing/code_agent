import { ServerManager } from './server';

/**
 * SSE client for connecting to the local Coding Agent server.
 * Handles Server-Sent Events for streaming completions.
 */
export class SSEClient {
    private port: number;
    private serverKey: string;
    private abortController: AbortController | null = null;

    constructor(port: number = 18792, serverKey: string = '') {
        this.port = port;
        this.serverKey = serverKey;
    }

    /**
     * Connect to the server and yield SSE events.
     */
    async *connect(task: string): AsyncGenerator<ServerEvent, void, unknown> {
        this.abortController = new AbortController();
        const url = `http://127.0.0.1:${this.port}/completion/stream?task=${encodeURIComponent(task)}`;

        const headers: Record<string, string> = {};
        if (this.serverKey) {
            headers['Authorization'] = `Bearer ${this.serverKey}`;
        }

        try {
            const response = await fetch(url, {
                method: 'GET',
                headers,
                signal: this.abortController.signal,
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const reader = response.body?.getReader();
            if (!reader) {
                throw new Error('No response body');
            }

            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) { break; }

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const data = line.slice(6);
                        try {
                            const event = JSON.parse(data) as ServerEvent;
                            yield event;
                            if (event.type === 'done' || event.type === 'error') {
                                return;
                            }
                        } catch {
                            // Skip malformed JSON
                        }
                    }
                }
            }
        } finally {
            this.abortController = null;
        }
    }

    abort(): void {
        this.abortController?.abort();
    }
}

export interface ServerEvent {
    type: 'step_start' | 'thinking' | 'content' | 'content_end' | 'tool_call' | 'tool_result' | 'final' | 'complete' | 'done' | 'error';
    step?: number;
    max_steps?: number;
    content?: string;
    tool_name?: string;
    tool_args?: Record<string, unknown>;
    tool_call_id?: string;
    success?: boolean;
    error?: string;
}