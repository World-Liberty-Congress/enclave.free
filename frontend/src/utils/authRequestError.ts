export class AuthRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly retryAfterSeconds = 60
  ) {
    super(message);
  }
}

export async function authRequestError(
  response: Response,
  fallback: string
): Promise<AuthRequestError> {
  let message = fallback;
  if (response.headers.get('content-type')?.includes('application/json')) {
    try {
      const body = await response.json();
      const detail = body.detail ?? body.message;
      if (typeof detail === 'string') message = detail;
    } catch {
      // A gateway or failed service may not return a readable JSON body.
    }
  }
  const header = response.headers.get('Retry-After');
  const seconds =
    header && /^\d+$/.test(header)
      ? Number(header)
      : header
        ? Math.ceil((Date.parse(header) - Date.now()) / 1000)
        : NaN;
  const retryAfter =
    Number.isFinite(seconds) && seconds >= 0 ? Math.max(1, seconds) : 60;
  return new AuthRequestError(message, response.status, retryAfter);
}
