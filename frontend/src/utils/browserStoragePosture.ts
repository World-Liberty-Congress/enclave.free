import { STORAGE_KEYS } from '../types/onboarding';

export type BrowserStorageSensitivity =
  | 'preference'
  | 'auth_marker'
  | 'profile_marker'
  | 'admin_marker';

export interface BrowserStorageAllowance {
  key: string;
  sensitivity: BrowserStorageSensitivity;
  reason: string;
  clearedOnLogout: boolean;
}

export const BROWSER_STORAGE_ALLOWLIST: BrowserStorageAllowance[] = [
  {
    key: 'enclave-theme',
    sensitivity: 'preference',
    reason:
      'UI preference only; does not contain Instance data or Conversation Content.',
    clearedOnLogout: false,
  },
  {
    key: 'i18nextLng',
    sensitivity: 'preference',
    reason: 'Language preference only.',
    clearedOnLogout: false,
  },
  {
    key: STORAGE_KEYS.ADMIN_PUBKEY,
    sensitivity: 'admin_marker',
    reason:
      'Local marker for Admin session routing; server cookie remains authoritative.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.ADMIN_SESSION_TOKEN,
    sensitivity: 'admin_marker',
    reason:
      'Legacy Admin session marker cleared when local auth state is cleared.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.USER_EMAIL,
    sensitivity: 'auth_marker',
    reason:
      'Local marker for User session routing; server cookie remains authoritative.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.USER_NAME,
    sensitivity: 'profile_marker',
    reason: 'Display marker for the authenticated User.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.USER_APPROVED,
    sensitivity: 'auth_marker',
    reason: 'Approval marker used for routing.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.USER_PROFILE,
    sensitivity: 'profile_marker',
    reason: 'Onboarding profile cache; must not survive logout.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.USER_TYPE_ID,
    sensitivity: 'profile_marker',
    reason: 'Selected User Type marker.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.PENDING_EMAIL,
    sensitivity: 'auth_marker',
    reason: 'Pending magic-link email marker.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.PENDING_NAME,
    sensitivity: 'profile_marker',
    reason: 'Pending magic-link display name marker.',
    clearedOnLogout: true,
  },
  {
    key: STORAGE_KEYS.SESSION_TOKEN,
    sensitivity: 'auth_marker',
    reason:
      'Legacy User session marker cleared when local auth state is cleared.',
    clearedOnLogout: true,
  },
];

export function browserStoragePosture() {
  return {
    allowlist: BROWSER_STORAGE_ALLOWLIST,
    unsupportedDeploymentSurface: {
      key: 'browser_storage',
      category: 'client_storage',
      summary:
        'Browser localStorage, sessionStorage, and cache remain Deployment Surfaces outside Active Storage Lifecycle.',
    },
    disallowedSensitivePatterns: [
      'conversation',
      'message',
      'prompt',
      'tool_output',
      'source_snippet',
    ],
  };
}

export function clearLogoutBrowserStorage(
  scope: 'all' | 'admin' | 'user' = 'all'
): void {
  if (scope !== 'user' && typeof window !== 'undefined') {
    window.dispatchEvent(new Event('enclave:admin-auth-cleared'));
  }
  for (const item of BROWSER_STORAGE_ALLOWLIST) {
    const matchesScope =
      scope === 'all' ||
      (scope === 'admin' && item.sensitivity === 'admin_marker') ||
      (scope === 'user' && item.sensitivity !== 'admin_marker');
    if (item.clearedOnLogout && matchesScope) {
      localStorage.removeItem(item.key);
      sessionStorage.removeItem(item.key);
    }
  }
}
