/**
 * @sphere/sdk — thin OpenAI-compatible client for the SPHERE metered gateway
 * (§4.7 of BUTTERBASE_BACKEND_DESIGN.md). Zero runtime dependencies; uses the
 * platform fetch available in Node ≥ 18, Deno, Bun, and browsers.
 *
 * v1 surface: chat.completions.create (non-streaming), models.list, balance().
 * Streaming and embeddings are deferred (the gateway rejects stream:true).
 */

export const DEFAULT_BASE_URL = "https://api.butterbase.ai/v1/app_21ze8d0ep28o/fn";

export class SphereError extends Error {
  status: number;
  code: string;
  type: string;
  constructor(message: string, status = 0, code = "", type = "") {
    super(message);
    this.name = new.target.name;
    this.status = status;
    this.code = code;
    this.type = type;
  }
}
export class InvalidKeyError extends SphereError {}
export class InsufficientCreditsError extends SphereError {}
export class ModelNotFoundError extends SphereError {}
export class InvalidRequestError extends SphereError {}
export class APIError extends SphereError {}

const ERROR_BY_STATUS: Record<number, typeof SphereError> = {
  400: InvalidRequestError,
  401: InvalidKeyError,
  402: InsufficientCreditsError,
  404: ModelNotFoundError,
};

export interface ChatMessage {
  role: "system" | "user" | "assistant" | string;
  content: unknown;
}
export interface ChatCompletionParams {
  model: string;
  messages: ChatMessage[];
  max_tokens?: number;
  temperature?: number;
  stream?: boolean;
  [k: string]: unknown;
}
export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}
export interface ChatCompletion {
  id: string;
  model: string;
  choices: Array<{ index: number; message: { role: string; content: string }; finish_reason: string }>;
  usage: Usage;
  [k: string]: unknown;
}
export interface Balance {
  balance_microcents: number;
  balance_usd: number;
}
export interface CatalogModel {
  id: string;
  name?: string;
  inputPricePerMTokens?: number;
  outputPricePerMTokens?: number;
  contextWindow?: number | null;
}

export interface SphereOptions {
  apiKey?: string;
  baseURL?: string;
  fetch?: typeof fetch;
}

export class Sphere {
  readonly baseURL: string;
  private readonly apiKey: string;
  private readonly fetchImpl: typeof fetch;

  readonly chat: { completions: { create: (params: ChatCompletionParams) => Promise<ChatCompletion> } };
  readonly models: { list: () => Promise<CatalogModel[]> };

  constructor(opts: SphereOptions = {}) {
    const env = (globalThis as any).process?.env ?? {};
    this.apiKey = opts.apiKey ?? env.SPHERE_API_KEY ?? "";
    if (!this.apiKey.startsWith("sphere_sk_")) {
      throw new InvalidKeyError("apiKey must be a sphere_sk_... key (or set SPHERE_API_KEY)");
    }
    this.baseURL = (opts.baseURL ?? env.SPHERE_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.fetchImpl = opts.fetch ?? fetch;

    this.chat = {
      completions: {
        create: (params) => {
          if (params.stream) {
            throw new InvalidRequestError("streaming is not supported in v1", 0, "stream_unsupported");
          }
          return this.request<ChatCompletion>("POST", `${this.baseURL}/gateway`, params);
        },
      },
    };
    this.models = {
      list: async () => {
        const root = this.baseURL.split("/v1/")[0];
        const r = await this.request<{ models: CatalogModel[] }>("GET", `${root}/v1/public/models`);
        return r.models;
      },
    };
  }

  async balance(): Promise<Balance> {
    return this.request<Balance>("GET", `${this.baseURL}/balance`);
  }

  private async request<T>(method: string, url: string, payload?: unknown): Promise<T> {
    let res: Response;
    try {
      res = await this.fetchImpl(url, {
        method,
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${this.apiKey}`,
          "user-agent": "sphere-js/0.1.0",
        },
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
    } catch (e) {
      throw new APIError(`connection failed: ${(e as Error).message}`);
    }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = (body as any)?.error ?? {};
      const Cls = ERROR_BY_STATUS[res.status] ?? APIError;
      throw new Cls(err.message ?? err.code ?? `HTTP ${res.status}`, res.status, err.code ?? "", err.type ?? "");
    }
    return body as T;
  }
}

export default Sphere;
