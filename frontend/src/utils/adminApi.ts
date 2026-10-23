/**
 * Admin API utilities
 * Provides authenticated fetch helper for admin endpoints.
 * Auth uses secure session cookies.
 */

import { API_BASE, STORAGE_KEYS } from '../types/onboarding';
import { clearLogoutBrowserStorage } from './browserStoragePosture';

export type AdminSessionValidationState =
  | 'authenticated'
  | 'unauthenticated'
  | 'unavailable';

/**
 * Fired when the curated resource directory (or its help-type vocabulary) is
 * mutated by an Admin action.
 * Open admin views (the Resource Directory table) listen for this to refresh
 * without a manual reload.
 */
export const ADMIN_RESOURCES_CHANGED_EVENT = 'enclave:admin-resources-changed';

/** Notify any open admin views that curated resources changed. */
export function notifyAdminResourcesChanged(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(ADMIN_RESOURCES_CHANGED_EVENT));
  }
}

/**
 * Make an authenticated admin API request.
 * Uses secure session cookies.
 * Redirects to /admin on 401 (session expired).
 */
export async function adminFetch(
  endpoint: string,
  options: RequestInit = {}
): Promise<Response> {
  const headers = new Headers(options.headers);

  if (
    options.body &&
    !headers.has('Content-Type') &&
    !(typeof FormData !== 'undefined' && options.body instanceof FormData)
  ) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers,
    credentials: 'include',
  });

  // Handle 401 - redirect to admin login
  if (response.status === 401) {
    clearAdminAuth();
    window.location.href = '/admin';
    throw new Error('errors.adminSessionExpired');
  }

  return response;
}

/**
 * Check if current session has valid admin authentication.
 */
export function isAdminAuthenticated(): boolean {
  try {
    return !!localStorage.getItem(STORAGE_KEYS.ADMIN_PUBKEY);
  } catch {
    return false;
  }
}

/**
 * Clear admin authentication state.
 */
export function clearAdminAuth(): void {
  clearLogoutBrowserStorage('admin');
}

/**
 * Clear server-side admin/user session cookies and local admin markers.
 */
export async function clearAdminAuthWithServerLogout(): Promise<void> {
  window.dispatchEvent(new Event('enclave:admin-auth-cleared'));
  try {
    await fetch(`${API_BASE}/admin/logout`, {
      method: 'POST',
      credentials: 'include',
    });
  } catch {
    // Best-effort cleanup
  }
  clearAdminAuth();
}

/**
 * Validate current admin cookie session against the backend.
 * Returns:
 * - authenticated: session accepted by backend
 * - unauthenticated: session missing/expired/invalid (401)
 * - unavailable: backend error/unreachable (5xx/network)
 */
export async function validateAdminSession(): Promise<AdminSessionValidationState> {
  if (!isAdminAuthenticated()) {
    return 'unauthenticated';
  }

  try {
    const response = await fetch(`${API_BASE}/admin/session`, {
      credentials: 'include',
    });

    if (response.status === 401) {
      clearAdminAuth();
      return 'unauthenticated';
    }

    if (response.ok) {
      return 'authenticated';
    }

    return 'unavailable';
  } catch {
    return 'unavailable';
  }
}
