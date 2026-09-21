export type Args = Record<string, unknown>;

export interface ApiError {
  code: string;
  message: string;
  detail?: Record<string, unknown>;
}

export class CallError extends Error {
  code: string;
  detail: Record<string, unknown>;
  constructor(e: ApiError) {
    super(`${e.code}: ${e.message}`);
    this.code = e.code;
    this.detail = e.detail ?? {};
  }
}

const TOKEN_KEY = "mariposa_token";
const WHO_KEY = "mariposa_who";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}
export function getWho(): string {
  return localStorage.getItem(WHO_KEY) ?? "qiaosheng";
}
export function saveAuth(who: string, token: string) {
  localStorage.setItem(WHO_KEY, who);
  localStorage.setItem(TOKEN_KEY, token);
}

export async function call<T = unknown>(capability: string, args?: Args,
                                        idem?: string): Promise<T> {
  const headers: Record<string, string> = {
    "Authorization": `Bearer ${getToken()}`,
    "Content-Type": "application/json",
  };
  if (idem) headers["Idempotency-Key"] = idem;
  const res = await fetch(`/api/capability/${capability}`, {
    method: "POST",
    headers,
    body: JSON.stringify({ arguments: args ?? {} }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.ok === false) {
    throw new CallError(body.error ?? { code: "HTTP_" + res.status, message: "" });
  }
  return body.data as T;
}
