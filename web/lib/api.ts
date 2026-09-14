const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // ignore parse failure, fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export interface Session {
  email: string;
  is_admin: boolean;
}

export function login(email: string): Promise<Session> {
  return request<Session>("/api/login", { method: "POST", body: JSON.stringify({ email }) });
}

export function logout(): Promise<{ ok: boolean }> {
  return request("/api/logout", { method: "POST" });
}

export function getSession(): Promise<Session> {
  return request<Session>("/api/session");
}

export interface StartRunPayload {
  market: string;
  geography: string;
  category_prompt: string;
  brief: string;
}

export function startRun(payload: StartRunPayload): Promise<{ run_id: string }> {
  return request("/api/runs", { method: "POST", body: JSON.stringify(payload) });
}

export interface ProgressEntry {
  stage: string;
  detail: string;
  ts: number;
}

export interface CompanyPreview {
  company_name: string;
  website: string;
  hq_country: string;
  category: string;
  confidence: number;
}

export interface RunSummary {
  run_id: string;
  market: string;
  geography: string;
  category_prompt: string;
  status: "running" | "done" | "error";
  started_at: number;
  progress_log: ProgressEntry[];
  duration_seconds?: number;
  total_candidates_found?: number;
  total_verified?: number;
  companies_count?: number;
  companies_preview?: CompanyPreview[];
  download_formats?: string[];
  error?: string;
}

export function getRun(runId: string): Promise<RunSummary> {
  return request<RunSummary>(`/api/runs/${runId}`);
}

export function listRuns(): Promise<RunSummary[]> {
  return request<RunSummary[]>("/api/runs");
}

export function downloadUrl(runId: string, fmt: string): string {
  return `${API_BASE}/api/runs/${runId}/download/${fmt}`;
}
