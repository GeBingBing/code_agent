import { ServerManager } from './server';

/**
 * Manages the connection to the local Coding Agent server.
 */
export class ServerManager {
    private port: number;
    private serverKey: string;
    private running: boolean = false;

    constructor(port: number = 18792, serverKey: string = '') {
        this.port = port;
        this.serverKey = serverKey;
    }

    get isRunning(): boolean {
        return this.running;
    }

    get endpoint(): string {
        return `http://127.0.0.1:${this.port}`;
    }

    async start(): Promise<void> {
        // Check if server is already running by hitting health endpoint
        try {
            const response = await fetch(`${this.endpoint}/health`, {
                method: 'GET',
            });
            if (response.ok) {
                this.running = true;
                return;
            }
        } catch {
            // Server not running, will be started by the user
        }
        this.running = true;
    }

    async stop(): Promise<void> {
        this.running = false;
    }

    async restart(): Promise<void> {
        await this.stop();
        await this.start();
    }

    async checkHealth(): Promise<boolean> {
        try {
            const response = await fetch(`${this.endpoint}/health`);
            return response.ok;
        } catch {
            return false;
        }
    }
}