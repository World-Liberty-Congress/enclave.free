import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  Loader2,
  Server,
  Database,
  Mail,
  Shield,
  ShieldCheck,
  Search,
  Download,
  RefreshCw,
  CheckCircle,
  XCircle,
  AlertCircle,
  Clock,
  Eye,
  EyeOff,
  Save,
  History,
  Send,
  X,
  HelpCircle,
  ChevronLeft,
  ChevronRight,
  Key,
  Globe,
  Lock,
  RotateCcw,
} from 'lucide-react';
import { OnboardingCard } from '../components/onboarding/OnboardingCard';
import { Button, Callout, Card, TextField } from '../components/ui';
import { isAdminAuthenticated, adminFetch } from '../utils/adminApi';
import {
  useDeploymentConfig,
  useServiceHealth,
  useDeploymentReadiness,
  useConfigAuditLog,
  useKeyMigration,
  useLifecycleStatus,
  useDeletionTombstones,
} from '../hooks/useAdminConfig';
import { useFocusTrap } from '../hooks/useFocusTrap';
import type {
  DeploymentConfigItem,
  ServiceHealthItem,
  ConfigCategory,
  DeploymentConfigItemKey,
  MigrationPrepareResponse,
  DecryptedUserData,
  DecryptedFieldValue,
  DeploymentValidationResponse,
  DeploymentReadinessItem,
  DeletionTombstoneStatusFilter,
} from '../types/config';
import {
  DEFAULT_TINFOIL_MODEL,
  TINFOIL_SIGNUP_URL,
  getConfigCategories,
  getDeploymentConfigItemMeta,
} from '../types/config';
import { hasNip04Support, decryptField } from '../utils/encryption';
import { hasNostrExtension } from '../utils/nostrAuth';
import { normalizePubkey } from '../utils/nostrKeys';
import { clearAdminAuth } from '../utils/adminApi';
import { STORAGE_KEYS } from '../types/onboarding';

type ValidationState = {
  result: DeploymentValidationResponse;
  validatedAt: string;
  configFingerprint: string;
};

type InferenceVerificationRecord = {
  id: number;
  status?: string;
  trigger?: string;
  provider_identity?: string | null;
  provider_endpoint?: string | null;
  model_identifier?: string | null;
  checked_at?: string | null;
  expires_at?: string | null;
  verifier_version?: string | null;
  claims_fingerprint?: string | null;
  attestation_material?: unknown;
  error?: string | null;
};

type InferenceVerificationStatus = {
  status: string;
  checked_at?: string | null;
  expires_at?: string | null;
  expected_claims_fingerprint?: string | null;
  configured_provider?: {
    provider_identity?: string | null;
    provider_endpoint?: string | null;
    model_identifier?: string | null;
  } | null;
  record?: InferenceVerificationRecord | null;
};

export function AdminDeploymentConfig() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const fixedT = i18n.getFixedT(i18n.language);

  // Hooks for config data
  const {
    config: deploymentConfig,
    loading: configLoading,
    error: configError,
    updateConfig,
    exportEnv,
    exportSageRuntimeEnv,
    exportCoreBackendRuntimeEnv,
    validate,
    revealSecret,
  } = useDeploymentConfig();
  const translatedConfigError =
    configError && i18n.exists(configError) ? fixedT(configError) : configError;

  const {
    health,
    loading: healthLoading,
    refresh: refreshHealth,
  } = useServiceHealth();
  const {
    readiness: deploymentReadiness,
    loading: readinessLoading,
    error: readinessError,
    refresh: refreshReadiness,
  } = useDeploymentReadiness();

  const {
    status: lifecycleStatus,
    loading: lifecycleLoading,
    acknowledgeUnsupportedSurface,
    acknowledgeUnsupportedSurfaceCategory,
    updateRetentionPolicy,
    updateArtifactEncryptionPosture,
    previewRetention,
    runScheduledRetention,
  } = useLifecycleStatus();
  const [tombstoneStatusFilter, setTombstoneStatusFilter] =
    useState<DeletionTombstoneStatusFilter>('all');
  const {
    tombstones,
    loading: tombstonesLoading,
    error: tombstonesError,
    retryTombstone,
  } = useDeletionTombstones(tombstoneStatusFilter);

  const {
    log: auditLog,
    loading: auditLoading,
    refresh: refreshAudit,
  } = useConfigAuditLog('deployment_config', 20);

  // Local state
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [showSecret, setShowSecret] = useState<string | null>(null);
  const [revealedSecrets, setRevealedSecrets] = useState<
    Record<string, string>
  >({});
  const [revealingSecret, setRevealingSecret] = useState<string | null>(null);
  const [revealError, setRevealError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [validationState, setValidationState] =
    useState<ValidationState | null>(null);
  const [validationDismissed, setValidationDismissed] = useState(false);
  const [validationLoading, setValidationLoading] = useState(false);
  const [showAuditLog, setShowAuditLog] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [sageRuntimeEnvExported, setSageRuntimeEnvExported] = useState(false);
  const [coreBackendRuntimeEnvExported, setCoreBackendRuntimeEnvExported] =
    useState(false);
  const [retryingTombstoneId, setRetryingTombstoneId] = useState<number | null>(
    null
  );
  const [tombstoneRetryError, setTombstoneRetryError] = useState<string | null>(
    null
  );
  const [acknowledgingSurfaceKey, setAcknowledgingSurfaceKey] = useState<
    string | null
  >(null);
  const [acknowledgingSurfaceCategory, setAcknowledgingSurfaceCategory] =
    useState<string | null>(null);
  const [unsupportedSurfaceError, setUnsupportedSurfaceError] = useState<
    string | null
  >(null);
  const [retentionPolicyDrafts, setRetentionPolicyDrafts] = useState<
    Record<
      string,
      {
        enabled: boolean;
        retention_window_days: number;
        scheduled_enforcement_enabled: boolean;
      }
    >
  >({});
  const [savingRetentionPolicyKey, setSavingRetentionPolicyKey] = useState<
    string | null
  >(null);
  const [retentionPolicyError, setRetentionPolicyError] = useState<
    string | null
  >(null);
  const [retentionPreviewError, setRetentionPreviewError] = useState<
    string | null
  >(null);
  const [retentionPreview, setRetentionPreview] = useState<{
    counts?: {
      stale_conversations?: number;
      document_artifacts?: number;
      skipped_classes?: number;
    };
  } | null>(null);
  const [retentionPreviewLoading, setRetentionPreviewLoading] = useState(false);
  const [scheduledRetentionResult, setScheduledRetentionResult] = useState<{
    status?: string;
    retry_results?: unknown[];
  } | null>(null);
  const [scheduledRetentionLoading, setScheduledRetentionLoading] =
    useState(false);
  const [scheduledRetentionError, setScheduledRetentionError] = useState<
    string | null
  >(null);
  const [artifactPostureUpdating, setArtifactPostureUpdating] = useState(false);
  const [artifactPostureError, setArtifactPostureError] = useState<
    string | null
  >(null);
  const [inferenceVerificationStatus, setInferenceVerificationStatus] =
    useState<InferenceVerificationStatus | null>(null);
  const [inferenceVerificationRecords, setInferenceVerificationRecords] =
    useState<InferenceVerificationRecord[]>([]);
  const [inferenceVerificationLoading, setInferenceVerificationLoading] =
    useState(false);
  const [inferenceVerificationError, setInferenceVerificationError] = useState<
    string | null
  >(null);
  const [manualVerificationLoading, setManualVerificationLoading] =
    useState(false);
  const [manualVerificationMessage, setManualVerificationMessage] = useState<
    string | null
  >(null);
  const [manualVerificationError, setManualVerificationError] = useState<
    string | null
  >(null);
  const [selectedVerificationRecordId, setSelectedVerificationRecordId] =
    useState<number | null>(null);
  const [selectedVerificationRecord, setSelectedVerificationRecord] =
    useState<InferenceVerificationRecord | null>(null);
  const [selectedVerificationLoading, setSelectedVerificationLoading] =
    useState(false);
  const [selectedVerificationError, setSelectedVerificationError] = useState<
    string | null
  >(null);
  const [showReadinessWizard, setShowReadinessWizard] = useState(false);
  const [readinessWizardIndex, setReadinessWizardIndex] = useState(0);

  // Test email modal state
  const [showTestEmailModal, setShowTestEmailModal] = useState(false);
  const [testEmailAddress, setTestEmailAddress] = useState('');
  const [testEmailSending, setTestEmailSending] = useState(false);
  const [testEmailResult, setTestEmailResult] = useState<{
    success: boolean;
    message: string;
    error?: string;
  } | null>(null);

  // Email help modal state
  const [showEmailHelpModal, setShowEmailHelpModal] = useState(false);
  const [emailHelpPage, setEmailHelpPage] = useState(0);
  const emailHelpModalRef = useRef<HTMLDivElement>(null);

  // LLM help modal state
  const [showLlmHelpModal, setShowLlmHelpModal] = useState(false);
  const [llmHelpPage, setLlmHelpPage] = useState(0);
  const llmHelpModalRef = useRef<HTMLDivElement>(null);

  // Embedding help modal state
  const [showEmbeddingHelpModal, setShowEmbeddingHelpModal] = useState(false);
  const [embeddingHelpPage, setEmbeddingHelpPage] = useState(0);
  const embeddingHelpModalRef = useRef<HTMLDivElement>(null);

  // Domains help modal state
  const [showDomainsHelpModal, setShowDomainsHelpModal] = useState(false);
  const [domainsHelpPage, setDomainsHelpPage] = useState(0);
  const domainsHelpModalRef = useRef<HTMLDivElement>(null);

  // Storage help modal state
  const [showStorageHelpModal, setShowStorageHelpModal] = useState(false);
  const [storageHelpPage, setStorageHelpPage] = useState(0);
  const storageHelpModalRef = useRef<HTMLDivElement>(null);

  // Search help modal state
  const [showSearchHelpModal, setShowSearchHelpModal] = useState(false);
  const [searchHelpPage, setSearchHelpPage] = useState(0);
  const searchHelpModalRef = useRef<HTMLDivElement>(null);

  // Security help modal state
  const [showSecurityHelpModal, setShowSecurityHelpModal] = useState(false);
  const [securityHelpPage, setSecurityHelpPage] = useState(0);
  const securityHelpModalRef = useRef<HTMLDivElement>(null);

  // SSL help modal state
  const [showSslHelpModal, setShowSslHelpModal] = useState(false);
  const [sslHelpPage, setSslHelpPage] = useState(0);
  const sslHelpModalRef = useRef<HTMLDivElement>(null);

  const [openHelpItemKey, setOpenHelpItemKey] = useState<string | null>(null);

  // Key migration hook and state
  const {
    loading: migrationLoading,
    prepare: prepareMigration,
    execute: executeMigration,
  } = useKeyMigration();

  const [showMigrationModal, setShowMigrationModal] = useState(false);
  const [migrationStep, setMigrationStep] = useState<
    'input' | 'confirm' | 'progress' | 'complete' | 'error'
  >('input');
  const [newAdminPubkey, setNewAdminPubkey] = useState('');
  const [migrationPrepareData, setMigrationPrepareData] =
    useState<MigrationPrepareResponse | null>(null);
  const [migrationProgress, setMigrationProgress] = useState('');
  const [migrationResult, setMigrationResult] = useState<{
    success: boolean;
    message: string;
    usersMigrated?: number;
    fieldValuesMigrated?: number;
  } | null>(null);
  const migrationModalRef = useRef<HTMLDivElement>(null);
  const isExecutingMigration = useRef(false);

  // Check if admin is logged in
  useEffect(() => {
    if (!isAdminAuthenticated()) {
      navigate('/admin');
    } else {
      setAuthChecked(true);
    }
  }, [navigate]);

  const configSignature = useMemo(() => {
    if (!deploymentConfig) {
      return { fingerprint: '', lastUpdatedAt: null as string | null };
    }

    const items = Object.values(deploymentConfig).flat();
    if (items.length === 0) {
      return { fingerprint: '', lastUpdatedAt: null as string | null };
    }

    const fingerprint = items
      .map((item) => `${item.key}:${item.value ?? ''}:${item.updated_at ?? ''}`)
      .sort()
      .join('|');

    let lastUpdatedAt: string | null = null;
    for (const item of items) {
      if (!item.updated_at) continue;
      if (!lastUpdatedAt) {
        lastUpdatedAt = item.updated_at;
        continue;
      }
      const current = new Date(item.updated_at).getTime();
      const existing = new Date(lastUpdatedAt).getTime();
      if (!isNaN(current) && (isNaN(existing) || current > existing)) {
        lastUpdatedAt = item.updated_at;
      }
    }

    return { fingerprint, lastUpdatedAt };
  }, [deploymentConfig]);

  const validationIsStale = Boolean(
    validationState &&
    configSignature.fingerprint &&
    validationState.configFingerprint !== configSignature.fingerprint
  );

  const formatTimestamp = (value?: string | null) => {
    if (!value) return t('common.unknown', 'Unknown');
    const date = new Date(value);
    return isNaN(date.getTime()) ? value : date.toLocaleString();
  };

  const parseAdminResponseError = async (
    response: Response,
    fallback: string
  ) => {
    try {
      const body = await response.json();
      return typeof body?.detail === 'string' ? body.detail : fallback;
    } catch {
      return fallback;
    }
  };

  const formatVerificationStatus = (status?: string | null) => {
    if (!status) return t('common.unknown', 'Unknown');
    if (status === 'current')
      return t('adminDeployment.inferenceVerification.current', 'Current');
    if (status === 'missing')
      return t('adminDeployment.inferenceVerification.missing', 'Missing');
    if (status === 'expired')
      return t('adminDeployment.inferenceVerification.expired', 'Expired');
    if (status === 'failed')
      return t('adminDeployment.inferenceVerification.failed', 'Failed');
    if (status === 'success')
      return t('adminDeployment.inferenceVerification.success', 'Success');
    return status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, ' ');
  };

  const loadInferenceVerification = useCallback(async () => {
    setInferenceVerificationLoading(true);
    setInferenceVerificationError(null);
    try {
      const [statusResponse, recordsResponse] = await Promise.all([
        adminFetch('/admin/deployment/inference-verification/status'),
        adminFetch('/admin/deployment/inference-verification/records'),
      ]);

      if (!statusResponse.ok) {
        throw new Error(
          await parseAdminResponseError(
            statusResponse,
            t(
              'adminDeployment.inferenceVerification.statusLoadFailed',
              'Failed to load inference verification status'
            )
          )
        );
      }
      if (!recordsResponse.ok) {
        throw new Error(
          await parseAdminResponseError(
            recordsResponse,
            t(
              'adminDeployment.inferenceVerification.historyLoadFailed',
              'Failed to load inference verification history'
            )
          )
        );
      }

      const statusBody = await statusResponse.json();
      const recordsBody = await recordsResponse.json();
      setInferenceVerificationStatus(statusBody);
      setInferenceVerificationRecords(
        Array.isArray(recordsBody?.records) ? recordsBody.records : []
      );
    } catch (err) {
      setInferenceVerificationError(
        err instanceof Error
          ? err.message
          : t(
              'adminDeployment.inferenceVerification.loadFailed',
              'Failed to load inference verification'
            )
      );
    } finally {
      setInferenceVerificationLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (authChecked) {
      void loadInferenceVerification();
    }
  }, [authChecked, loadInferenceVerification]);

  const handleManualInferenceVerification = async () => {
    setManualVerificationLoading(true);
    setManualVerificationMessage(null);
    setManualVerificationError(null);
    try {
      const response = await adminFetch(
        '/admin/deployment/inference-verification/verify',
        { method: 'POST' }
      );
      if (!response.ok) {
        throw new Error(
          await parseAdminResponseError(
            response,
            t(
              'adminDeployment.inferenceVerification.manualFailed',
              'Manual verification failed'
            )
          )
        );
      }
      await response.json();
      setManualVerificationMessage(
        t(
          'adminDeployment.inferenceVerification.manualSucceeded',
          'Manual verification succeeded'
        )
      );
      await loadInferenceVerification();
    } catch (err) {
      setManualVerificationError(
        err instanceof Error
          ? err.message
          : t(
              'adminDeployment.inferenceVerification.manualFailed',
              'Manual verification failed'
            )
      );
    } finally {
      setManualVerificationLoading(false);
    }
  };

  const handleInspectInferenceVerificationRecord = async (recordId: number) => {
    setSelectedVerificationRecordId(recordId);
    setSelectedVerificationRecord(null);
    setSelectedVerificationError(null);
    setSelectedVerificationLoading(true);
    try {
      const response = await adminFetch(
        `/admin/deployment/inference-verification/records/${recordId}`
      );
      if (!response.ok) {
        throw new Error(
          await parseAdminResponseError(
            response,
            t(
              'adminDeployment.inferenceVerification.detailLoadFailed',
              'Failed to load attestation detail'
            )
          )
        );
      }
      setSelectedVerificationRecord(await response.json());
    } catch (err) {
      setSelectedVerificationError(
        err instanceof Error
          ? err.message
          : t(
              'adminDeployment.inferenceVerification.detailLoadFailed',
              'Failed to load attestation detail'
            )
      );
    } finally {
      setSelectedVerificationLoading(false);
    }
  };

  const formatLifecycleStatus = (status: string) => {
    if (status === 'not_started')
      return t('adminDeployment.lifecycle.notStarted', 'Not Started');
    if (status === 'not_configured')
      return t('adminDeployment.lifecycle.notConfigured', 'Not Configured');
    if (status === 'plaintext_by_operator_choice')
      return t(
        'adminDeployment.lifecycle.plaintextByOperatorChoice',
        'Plaintext by Operator Choice'
      );
    return status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, ' ');
  };

  const formatReadinessSeverity = (severity: string) => {
    if (severity === 'blocker')
      return t('adminDeployment.readiness.blocker', 'Blocker');
    if (severity === 'warning')
      return t('adminDeployment.readiness.warning', 'Warning');
    if (severity === 'ready')
      return t('adminDeployment.readiness.ready', 'Ready');
    return formatLifecycleStatus(severity);
  };

  const readinessToneClass = (severity: string) => {
    if (severity === 'blocker') return 'border-error/20 bg-error/10 text-error';
    if (severity === 'warning')
      return 'border-warning/20 bg-warning/10 text-warning';
    if (severity === 'ready' || severity === 'success')
      return 'border-success/20 bg-success/10 text-success';
    return 'border-border bg-surface-muted text-text-muted';
  };

  /**
   * Deployment readiness text is authored in the Enclave Control Plane
   * (`backend/app/deployment_config.py`). Earlier responses carried only
   * finished English prose, which rendered in every locale.
   *
   * Translate the stable message descriptors and preserve the English fields
   * as compatibility fallbacks. Runtime counts and technical setting names are
   * supplied separately as interpolation values. See #647.
   */
  const readinessLabel = (item: DeploymentReadinessItem) =>
    !item.label_key ||
    item.label_key === `adminDeployment.readiness.${item.key}.label`
      ? t(`adminDeployment.readiness.${item.key}.label`, item.label)
      : item.label;

  const readinessMessage = (
    item: DeploymentReadinessItem,
    field: 'summary' | 'nextAction'
  ) => {
    const isSummary = field === 'summary';
    const key = isSummary ? item.summary_key : item.next_action_key;
    const fallback = isSummary ? item.summary : item.next_action;
    const values = isSummary ? item.summary_values : item.next_action_values;
    const derivedKey = `adminDeployment.readiness.${item.key}.status.${item.status}.${field}`;
    const options = { defaultValue: fallback, ...values };

    if (isSummary) {
      if (
        key ===
        'adminDeployment.readiness.deployment_surface_acknowledgements.status.needs_acknowledgement.summary'
      ) {
        return t(
          'adminDeployment.readiness.deployment_surface_acknowledgements.status.needs_acknowledgement.summary',
          options
        );
      }
      if (key && key !== derivedKey) return fallback;
      return t(
        `adminDeployment.readiness.${item.key}.status.${item.status}.summary`,
        options
      );
    }

    if (
      key ===
      'adminDeployment.readiness.deployment_surface_acknowledgements.status.needs_acknowledgement.nextAction'
    ) {
      return t(
        'adminDeployment.readiness.deployment_surface_acknowledgements.status.needs_acknowledgement.nextAction',
        options
      );
    }
    if (key && key !== derivedKey) return fallback;
    return t(
      `adminDeployment.readiness.${item.key}.status.${item.status}.nextAction`,
      options
    );
  };

  const readinessSummary = (item: DeploymentReadinessItem) =>
    readinessMessage(item, 'summary');

  const readinessNextAction = (item: DeploymentReadinessItem) =>
    readinessMessage(item, 'nextAction');

  const readinessReviewTarget = (item: DeploymentReadinessItem) => {
    if (item.source === 'inference_verification') {
      return {
        href: '#inference-verification',
        label: t(
          'adminDeployment.readiness.reviewInferenceVerification',
          'Review Inference Verification'
        ),
      };
    }
    if (
      item.source === 'lifecycle_readiness' ||
      item.source === 'deployment_surfaces'
    ) {
      return {
        href: '#data-lifecycle-status',
        label: t(
          'adminDeployment.readiness.reviewDataLifecycleStatus',
          'Review Data Lifecycle Status'
        ),
      };
    }
    if (item.source === 'deployment_validation') {
      return {
        href: '#deployment-settings',
        label: t(
          'adminDeployment.readiness.reviewDeploymentValidation',
          'Review Deployment Settings'
        ),
      };
    }
    if (item.source === 'restart_required') {
      return {
        href: '#restart-required',
        label: t(
          'adminDeployment.readiness.reviewRestartRequired',
          'Review Restart Required'
        ),
      };
    }
    if (item.source === 'runtime_env') {
      if (!health?.runtime_env?.sage) {
        return null;
      }
      return {
        href: '#runtime-config-alignment',
        label: t(
          'adminDeployment.readiness.reviewRuntimeEnv',
          'Review Runtime Env Export'
        ),
      };
    }
    if (item.source === 'operational_readiness') {
      return {
        href: '#operational-readiness',
        label: t(
          'adminDeployment.readiness.reviewOperationalReadiness',
          'Review Operational Readiness'
        ),
      };
    }
    return {
      href: '#service-health',
      label: t(
        'adminDeployment.readiness.reviewServiceHealth',
        'Review Service Health'
      ),
    };
  };

  const tombstoneStatusFilters: Array<{
    value: DeletionTombstoneStatusFilter;
    label: string;
    ariaLabel: string;
  }> = [
    {
      value: 'all',
      label: t('adminDeployment.lifecycle.allTombstones', 'All'),
      ariaLabel: t(
        'adminDeployment.lifecycle.allTombstonesLabel',
        'All tombstones'
      ),
    },
    {
      value: 'incomplete',
      label: t(
        'adminDeployment.lifecycle.incompleteTombstonesFilter',
        'Incomplete'
      ),
      ariaLabel: t(
        'adminDeployment.lifecycle.incompleteTombstonesLabel',
        'Incomplete tombstones'
      ),
    },
    {
      value: 'completed',
      label: t(
        'adminDeployment.lifecycle.completedTombstonesFilter',
        'Completed'
      ),
      ariaLabel: t(
        'adminDeployment.lifecycle.completedTombstonesLabel',
        'Completed tombstones'
      ),
    },
  ];

  useEffect(() => {
    if (!Array.isArray(lifecycleStatus?.data_classes)) return;
    setRetentionPolicyDrafts((currentDrafts) => {
      const nextDrafts = { ...currentDrafts };
      lifecycleStatus.data_classes.forEach((dataClass) => {
        if (dataClass.retention_policy) {
          nextDrafts[dataClass.key] = {
            ...(nextDrafts[dataClass.key] ?? {}),
            enabled: dataClass.retention_policy.enabled,
            retention_window_days:
              dataClass.retention_policy.retention_window_days,
            scheduled_enforcement_enabled:
              dataClass.retention_policy.scheduled_enforcement_enabled,
          };
        }
      });
      return nextDrafts;
    });
  }, [lifecycleStatus]);

  const handleRetryTombstone = async (id: number) => {
    try {
      setRetryingTombstoneId(id);
      setTombstoneRetryError(null);
      await retryTombstone(id);
    } catch (err) {
      setTombstoneRetryError(
        err instanceof Error
          ? err.message
          : t('errors.failedToRetryDeletionTombstone')
      );
    } finally {
      setRetryingTombstoneId(null);
    }
  };

  const handleAcknowledgeUnsupportedSurface = async (
    key: string,
    acknowledged: boolean
  ) => {
    try {
      setAcknowledgingSurfaceKey(key);
      setUnsupportedSurfaceError(null);
      await acknowledgeUnsupportedSurface(key, acknowledged);
    } catch (err) {
      setUnsupportedSurfaceError(
        err instanceof Error
          ? err.message
          : t('errors.failedToAcknowledgeUnsupportedSurface')
      );
    } finally {
      setAcknowledgingSurfaceKey(null);
    }
  };

  const handleAcknowledgeUnsupportedSurfaceCategory = async (
    category: string,
    acknowledged: boolean
  ) => {
    try {
      setAcknowledgingSurfaceCategory(category);
      setUnsupportedSurfaceError(null);
      await acknowledgeUnsupportedSurfaceCategory(category, acknowledged);
    } catch (err) {
      setUnsupportedSurfaceError(
        err instanceof Error
          ? err.message
          : t('errors.failedToAcknowledgeUnsupportedSurfaceCategory')
      );
    } finally {
      setAcknowledgingSurfaceCategory(null);
    }
  };

  const updateRetentionPolicyDraft = (
    key: string,
    patch: Partial<{
      enabled: boolean;
      retention_window_days: number;
      scheduled_enforcement_enabled: boolean;
    }>
  ) => {
    const defaultDraft = {
      enabled: false,
      retention_window_days: 30,
      scheduled_enforcement_enabled: false,
    };
    setRetentionPolicyDrafts((drafts) => ({
      ...drafts,
      [key]: {
        ...defaultDraft,
        ...(drafts[key] ?? {}),
        ...patch,
      },
    }));
  };

  const handleSaveRetentionPolicy = async (key: string) => {
    const draft = retentionPolicyDrafts[key];
    if (!draft) return;
    try {
      setSavingRetentionPolicyKey(key);
      setRetentionPolicyError(null);
      await updateRetentionPolicy(key, {
        enabled: draft.enabled,
        retention_window_days: Math.max(
          1,
          Number(draft.retention_window_days) || 1
        ),
        scheduled_enforcement_enabled: draft.scheduled_enforcement_enabled,
      });
    } catch (err) {
      setRetentionPolicyError(
        err instanceof Error
          ? err.message
          : t('errors.failedToUpdateRetentionPolicy')
      );
    } finally {
      setSavingRetentionPolicyKey(null);
    }
  };

  const handlePreviewRetention = async () => {
    try {
      setRetentionPreviewLoading(true);
      setRetentionPreviewError(null);
      setRetentionPreview(await previewRetention());
    } catch (err) {
      setRetentionPreviewError(
        err instanceof Error
          ? err.message
          : t('errors.failedToPreviewRetention')
      );
    } finally {
      setRetentionPreviewLoading(false);
    }
  };

  const handleRunScheduledRetention = async () => {
    try {
      setScheduledRetentionLoading(true);
      setScheduledRetentionError(null);
      setScheduledRetentionResult(await runScheduledRetention());
    } catch (err) {
      setScheduledRetentionError(
        err instanceof Error
          ? err.message
          : t('errors.failedToRunScheduledRetention')
      );
    } finally {
      setScheduledRetentionLoading(false);
    }
  };

  const handleArtifactPostureChange = async (
    posture: 'required' | 'disabled'
  ) => {
    try {
      setArtifactPostureUpdating(true);
      setArtifactPostureError(null);
      await updateArtifactEncryptionPosture(posture);
    } catch (err) {
      setArtifactPostureError(
        err instanceof Error
          ? err.message
          : t('errors.failedToUpdateArtifactEncryptionPosture')
      );
    } finally {
      setArtifactPostureUpdating(false);
    }
  };

  // Handle editing a config value
  const handleEdit = (item: DeploymentConfigItem) => {
    setEditingKey(item.key);
    setEditValue(item.is_secret ? '' : item.value || '');
    setSaveError(null);
  };

  // Handle saving a config value
  const handleSave = async () => {
    if (!editingKey) return;

    // Find the config item being edited
    const item = Object.values(deploymentConfig || {})
      .flat()
      .find((c) => c.key === editingKey);

    // Don't save empty secret values - this prevents wiping existing credentials.
    if (item?.is_secret && editValue === '') {
      setEditingKey(null);
      setEditValue('');
      return;
    }

    setSaving(true);
    setSaveError(null);

    try {
      await updateConfig(editingKey, editValue);
      // Clear revealed secret cache for this key if it was updated
      setRevealedSecrets((prev) => {
        const { [editingKey]: _, ...rest } = prev;
        return rest;
      });
      setShowSecret(null);
      setEditingKey(null);
      setEditValue('');
      // Refresh health after config change
      refreshHealth();
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : t('adminDeployment.saveError')
      );
    } finally {
      setSaving(false);
    }
  };

  // Handle cancel editing
  const handleCancel = () => {
    setEditingKey(null);
    setEditValue('');
    setSaveError(null);
  };

  // Handle toggling secret visibility
  const handleToggleSecret = async (key: string) => {
    if (showSecret === key) {
      // Hide the secret
      setShowSecret(null);
      return;
    }

    // Check if we already have the revealed value cached
    if (revealedSecrets[key] !== undefined) {
      setShowSecret(key);
      return;
    }

    // Fetch the actual secret value
    setRevealingSecret(key);
    setRevealError(null);
    try {
      const value = await revealSecret(key);
      setRevealedSecrets((prev) => ({ ...prev, [key]: value }));
      setShowSecret(key);
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : t('adminDeployment.revealFailed', 'Failed to reveal secret');
      setRevealError(message);
      console.error('Failed to reveal secret:', err);
    } finally {
      setRevealingSecret(null);
    }
  };

  // Handle export .env
  const handleExport = async () => {
    setExportError(null);
    try {
      const content = await exportEnv();
      const blob = new Blob([content], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = '.env';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : t('adminDeployment.exportFailed', 'Export failed');
      setExportError(message);
      console.error('Export failed:', err);
    }
  };

  const handleExportSageRuntimeEnv = async () => {
    setExportError(null);
    setSageRuntimeEnvExported(false);
    try {
      const content = await exportSageRuntimeEnv();
      const blob = new Blob([content], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'sage.env';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setSageRuntimeEnvExported(true);
      await refreshReadiness();
      await refreshHealth();
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : t('adminDeployment.exportFailed', 'Export failed');
      setExportError(message);
      console.error('Sage runtime env export failed:', err);
    }
  };

  const handleExportCoreBackendRuntimeEnv = async () => {
    setExportError(null);
    setCoreBackendRuntimeEnvExported(false);
    try {
      const content = await exportCoreBackendRuntimeEnv();
      const blob = new Blob([content], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'core-backend.env';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setCoreBackendRuntimeEnvExported(true);
      await refreshReadiness();
      await refreshHealth();
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : t('adminDeployment.exportFailed', 'Export failed');
      setExportError(message);
      console.error('Core backend runtime env export failed:', err);
    }
  };

  // Handle validate
  const handleValidate = async () => {
    setValidationLoading(true);
    setValidationDismissed(false);
    try {
      const result = await validate();
      setValidationState({
        result,
        validatedAt: new Date().toISOString(),
        configFingerprint: configSignature.fingerprint,
      });
    } catch (err) {
      console.error('Validation failed:', err);
      setValidationState({
        result: {
          valid: false,
          errors: [
            err instanceof Error
              ? err.message
              : t(
                  'adminDeployment.validationFailed',
                  'Validation request failed'
                ),
          ],
          warnings: [],
        },
        validatedAt: new Date().toISOString(),
        configFingerprint: configSignature.fingerprint,
      });
    } finally {
      setValidationLoading(false);
    }
  };

  // Handle send test email
  const handleSendTestEmail = async () => {
    if (!testEmailAddress.trim()) return;

    setTestEmailSending(true);
    setTestEmailResult(null);

    try {
      const response = await adminFetch('/auth/test-email', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email: testEmailAddress.trim() }),
      });

      // Check response.ok before attempting to parse JSON
      if (!response.ok) {
        let errorDetail = `HTTP ${response.status}`;
        try {
          const errorData = await response.json();
          errorDetail = errorData.detail || errorDetail;
        } catch {
          // Response body isn't JSON (e.g., HTML error page), use status code
        }
        setTestEmailResult({
          success: false,
          message: t('errors.requestFailed'),
          error: errorDetail,
        });
        return;
      }

      let data;
      try {
        data = await response.json();
      } catch {
        setTestEmailResult({
          success: false,
          message: t('errors.requestFailed'),
          error: t('errors.invalidServerResponse'),
        });
        return;
      }

      setTestEmailResult(data);

      // Refresh health to show updated SMTP status (green after success)
      if (data.success) {
        refreshHealth();
      }
    } catch (err) {
      console.error('Test email failed:', err);
      setTestEmailResult({
        success: false,
        message: t('errors.requestFailed'),
        error: err instanceof Error ? err.message : t('errors.unknownError'),
      });
    } finally {
      setTestEmailSending(false);
    }
  };

  // Close test email modal
  const handleCloseTestEmailModal = () => {
    setShowTestEmailModal(false);
    setTestEmailAddress('');
    setTestEmailResult(null);
  };

  // Close email help modal
  const handleCloseEmailHelpModal = () => {
    setShowEmailHelpModal(false);
    setEmailHelpPage(0);
  };

  // Close LLM help modal
  const handleCloseLlmHelpModal = () => {
    setShowLlmHelpModal(false);
    setLlmHelpPage(0);
  };

  // Close embedding help modal
  const handleCloseEmbeddingHelpModal = () => {
    setShowEmbeddingHelpModal(false);
    setEmbeddingHelpPage(0);
  };

  const handleCloseDomainsHelpModal = () => {
    setShowDomainsHelpModal(false);
    setDomainsHelpPage(0);
  };

  const handleCloseStorageHelpModal = () => {
    setShowStorageHelpModal(false);
    setStorageHelpPage(0);
  };

  const handleCloseSearchHelpModal = () => {
    setShowSearchHelpModal(false);
    setSearchHelpPage(0);
  };

  const handleCloseSecurityHelpModal = () => {
    setShowSecurityHelpModal(false);
    setSecurityHelpPage(0);
  };

  const handleCloseSslHelpModal = () => {
    setShowSslHelpModal(false);
    setSslHelpPage(0);
  };

  // Key migration handlers
  const handleOpenMigrationModal = () => {
    // Check prerequisites
    if (!hasNostrExtension()) {
      setMigrationResult({
        success: false,
        message: t(
          'adminDeployment.keyMigration.noExtension',
          'No Nostr extension found. Please install a NIP-07 compatible extension like Alby or nos2x.'
        ),
      });
      setMigrationStep('error');
      setShowMigrationModal(true);
      return;
    }
    if (!hasNip04Support()) {
      setMigrationResult({
        success: false,
        message: t(
          'adminDeployment.keyMigration.noNip04',
          'Your Nostr extension does not support NIP-04 decryption.'
        ),
      });
      setMigrationStep('error');
      setShowMigrationModal(true);
      return;
    }

    setMigrationStep('input');
    setNewAdminPubkey('');
    setMigrationPrepareData(null);
    setMigrationResult(null);
    setShowMigrationModal(true);
  };

  const handleCloseMigrationModal = () => {
    if (migrationStep === 'progress') {
      // Don't allow closing during migration
      return;
    }
    setShowMigrationModal(false);
    setMigrationStep('input');
    setNewAdminPubkey('');
    setMigrationPrepareData(null);
    setMigrationResult(null);
  };

  const handleMigrationPrepare = async () => {
    // Validate new pubkey
    const trimmed = newAdminPubkey.trim();
    let normalizedPubkey: string;
    try {
      normalizedPubkey = normalizePubkey(trimmed);
    } catch {
      setMigrationResult({
        success: false,
        message: t(
          'adminDeployment.keyMigration.invalidPubkey',
          'Invalid pubkey format. Enter a valid npub or 64-character hex pubkey.'
        ),
      });
      setMigrationStep('error');
      return;
    }

    // Fetch encrypted data
    setMigrationProgress(
      t(
        'adminDeployment.keyMigration.fetchingData',
        'Fetching encrypted data...'
      )
    );
    setMigrationStep('progress');

    try {
      const prepareData = await prepareMigration();
      setMigrationPrepareData(prepareData);

      // Check if trying to migrate to same key
      if (normalizedPubkey === prepareData.admin_pubkey) {
        setMigrationResult({
          success: false,
          message: t(
            'adminDeployment.keyMigration.samePubkey',
            'The new pubkey must be different from the current admin pubkey.'
          ),
        });
        setMigrationStep('error');
        return;
      }

      setNewAdminPubkey(normalizedPubkey);
      setMigrationStep('confirm');
    } catch (err) {
      setMigrationResult({
        success: false,
        message:
          err instanceof Error
            ? err.message
            : t(
                'adminDeployment.keyMigration.prepareFailed',
                'Failed to prepare migration'
              ),
      });
      setMigrationStep('error');
    }
  };

  const handleMigrationExecute = async () => {
    if (!migrationPrepareData || !newAdminPubkey) return;
    if (isExecutingMigration.current) return;

    isExecutingMigration.current = true;
    setMigrationStep('progress');

    try {
      // Step 1: Decrypt all user data
      setMigrationProgress(
        t(
          'adminDeployment.keyMigration.decryptingUsers',
          'Decrypting user data...'
        )
      );
      const decryptedUsers: DecryptedUserData[] = [];

      for (const user of migrationPrepareData.users) {
        const decryptedUser: DecryptedUserData = { id: user.id };

        // Guard: encrypted data must have its ephemeral pubkey
        if (user.encrypted_email && !user.ephemeral_pubkey_email) {
          throw new Error(
            t(
              'adminDeployment.keyMigration.decryptFailed',
              'Data integrity error: encrypted email for user {{id}} is missing ephemeral pubkey. Migration aborted.',
              { id: user.id }
            )
          );
        }
        if (user.encrypted_name && !user.ephemeral_pubkey_name) {
          throw new Error(
            t(
              'adminDeployment.keyMigration.decryptFailed',
              'Data integrity error: encrypted name for user {{id}} is missing ephemeral pubkey. Migration aborted.',
              { id: user.id }
            )
          );
        }

        if (user.encrypted_email && user.ephemeral_pubkey_email) {
          const email = await decryptField({
            ciphertext: user.encrypted_email,
            ephemeral_pubkey: user.ephemeral_pubkey_email,
          });
          if (email === null) {
            throw new Error(
              t(
                'adminDeployment.keyMigration.decryptFailed',
                'Failed to decrypt email for user {{id}}. Migration aborted to prevent data loss.',
                { id: user.id }
              )
            );
          }
          decryptedUser.email = email;
        }

        if (user.encrypted_name && user.ephemeral_pubkey_name) {
          const name = await decryptField({
            ciphertext: user.encrypted_name,
            ephemeral_pubkey: user.ephemeral_pubkey_name,
          });
          if (name === null) {
            throw new Error(
              t(
                'adminDeployment.keyMigration.decryptFailed',
                'Failed to decrypt name for user {{id}}. Migration aborted to prevent data loss.',
                { id: user.id }
              )
            );
          }
          decryptedUser.name = name;
        }

        decryptedUsers.push(decryptedUser);
      }

      // Step 2: Decrypt all field values
      setMigrationProgress(
        t(
          'adminDeployment.keyMigration.decryptingFields',
          'Decrypting field values...'
        )
      );
      const decryptedFieldValues: DecryptedFieldValue[] = [];

      for (const field of migrationPrepareData.field_values) {
        // Guard: encrypted data must have its ephemeral pubkey
        if (field.encrypted_value && !field.ephemeral_pubkey) {
          throw new Error(
            t(
              'adminDeployment.keyMigration.decryptFieldFailed',
              'Data integrity error: encrypted field {{id}} is missing ephemeral pubkey. Migration aborted.',
              { id: field.id }
            )
          );
        }

        if (field.encrypted_value && field.ephemeral_pubkey) {
          const value = await decryptField({
            ciphertext: field.encrypted_value,
            ephemeral_pubkey: field.ephemeral_pubkey,
          });
          if (value === null) {
            throw new Error(
              t(
                'adminDeployment.keyMigration.decryptFieldFailed',
                'Failed to decrypt field value {{id}}. Migration aborted to prevent data loss.',
                { id: field.id }
              )
            );
          }
          decryptedFieldValues.push({ id: field.id, value });
        }
      }

      // Step 3: Sign authorization event
      setMigrationProgress(
        t('adminDeployment.keyMigration.signing', 'Requesting signature...')
      );

      if (!window.nostr) {
        throw new Error(
          t(
            'adminDeployment.keyMigration.noExtension',
            'No Nostr extension found'
          )
        );
      }

      const unsignedEvent = {
        kind: 22242,
        created_at: Math.floor(Date.now() / 1000),
        tags: [
          ['action', 'admin_key_migration'],
          ['new_pubkey', newAdminPubkey],
        ],
        content: '',
      };

      const signedEvent = await window.nostr.signEvent(unsignedEvent);

      // Step 4: Submit migration
      setMigrationProgress(
        t('adminDeployment.keyMigration.submitting', 'Submitting migration...')
      );

      const result = await executeMigration(
        newAdminPubkey,
        decryptedUsers,
        decryptedFieldValues,
        signedEvent
      );

      setMigrationResult({
        success: true,
        message: result.message,
        usersMigrated: result.users_migrated,
        fieldValuesMigrated: result.field_values_migrated,
      });
      setMigrationStep('complete');
    } catch (err) {
      console.error('Migration failed:', err);
      setMigrationResult({
        success: false,
        message:
          err instanceof Error
            ? err.message
            : t('adminDeployment.keyMigration.failed', 'Migration failed'),
      });
      setMigrationStep('error');
    } finally {
      isExecutingMigration.current = false;
    }
  };

  const handleMigrationComplete = () => {
    // Clear session and redirect to login
    clearAdminAuth();
    navigate('/admin');
  };

  useFocusTrap(showMigrationModal, migrationModalRef);
  useFocusTrap(showEmailHelpModal, emailHelpModalRef);
  useFocusTrap(showLlmHelpModal, llmHelpModalRef);
  useFocusTrap(showEmbeddingHelpModal, embeddingHelpModalRef);
  useFocusTrap(showDomainsHelpModal, domainsHelpModalRef);
  useFocusTrap(showStorageHelpModal, storageHelpModalRef);
  useFocusTrap(showSearchHelpModal, searchHelpModalRef);
  useFocusTrap(showSecurityHelpModal, securityHelpModalRef);
  useFocusTrap(showSslHelpModal, sslHelpModalRef);

  // Close item help popover on outside click
  useEffect(() => {
    if (!openHelpItemKey) return;
    const handleClick = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (target.closest(`[data-help-item="${openHelpItemKey}"]`)) return;
      setOpenHelpItemKey(null);
    };
    document.addEventListener('click', handleClick, true);
    return () => document.removeEventListener('click', handleClick, true);
  }, [openHelpItemKey]);

  // Email help pages data
  const EMAIL_HELP_PAGES = [
    {
      title: t(
        'adminDeployment.emailHelp.overviewTitle',
        'SMTP Field Reference'
      ),
      content: 'overview',
    },
    {
      title: 'Gmail',
      hint: t(
        'adminDeployment.emailHelp.gmailHint',
        'Requires App Password from myaccount.google.com/apppasswords'
      ),
      config: {
        SMTP_HOST: 'smtp.gmail.com',
        SMTP_PORT: '587',
        SMTP_USER: 'yourname@gmail.com',
        SMTP_PASS: 'xxxx-xxxx-xxxx-xxxx',
        SMTP_FROM: 'Enclave <yourname@gmail.com>',
      },
    },
    {
      title: 'Mailgun',
      hint: t(
        'adminDeployment.emailHelp.mailgunHint',
        'Use your domain-specific SMTP credentials'
      ),
      config: {
        SMTP_HOST: 'smtp.mailgun.org',
        SMTP_PORT: '587',
        SMTP_USER: 'postmaster@mg.yourdomain.com',
        SMTP_PASS: 'your-mailgun-smtp-password',
        SMTP_FROM: 'Enclave <noreply@mg.yourdomain.com>',
      },
    },
    {
      title: 'SendGrid',
      hint: t(
        'adminDeployment.emailHelp.sendgridHint',
        'SMTP_USER is literally "apikey" (not your email)'
      ),
      config: {
        SMTP_HOST: 'smtp.sendgrid.net',
        SMTP_PORT: '587',
        SMTP_USER: 'apikey',
        SMTP_PASS: 'SG.your-sendgrid-api-key',
        SMTP_FROM: 'Enclave <noreply@yourdomain.com>',
      },
    },
    {
      title: 'Amazon SES',
      hint: t(
        'adminDeployment.emailHelp.sesHint',
        'Use your region (e.g., us-east-1). FROM address must be verified.'
      ),
      config: {
        SMTP_HOST: 'email-smtp.us-east-1.amazonaws.com',
        SMTP_PORT: '587',
        SMTP_USER: 'your-ses-smtp-username',
        SMTP_PASS: 'your-ses-smtp-password',
        SMTP_FROM: 'Enclave <noreply@yourdomain.com>',
      },
    },
    {
      title: 'Postmark',
      hint: t(
        'adminDeployment.emailHelp.postmarkHint',
        'User and password are both your Server API Token'
      ),
      config: {
        SMTP_HOST: 'smtp.postmarkapp.com',
        SMTP_PORT: '587',
        SMTP_USER: 'your-server-api-token',
        SMTP_PASS: 'your-server-api-token',
        SMTP_FROM: 'Enclave <noreply@yourdomain.com>',
      },
    },
    {
      title: 'Brevo',
      hint: t(
        'adminDeployment.emailHelp.brevoHint',
        'Formerly Sendinblue. Use your SMTP key, not your account password.'
      ),
      config: {
        SMTP_HOST: 'smtp-relay.brevo.com',
        SMTP_PORT: '587',
        SMTP_USER: 'your-brevo-login-email',
        SMTP_PASS: 'your-smtp-key',
        SMTP_FROM: 'Enclave <noreply@yourdomain.com>',
      },
    },
  ];

  // LLM help pages data
  const LLM_HELP_PAGES = [
    {
      title: t(
        'adminDeployment.llmHelp.overviewTitle',
        'Sage Runtime Overview'
      ),
      content: 'overview',
    },
    {
      title: 'Sage + Tinfoil',
      hint: t(
        'adminDeployment.llmHelp.sageHint',
        'This prototype routes AI traffic through Sage and uses Tinfoil for model inference.'
      ),
      config: {
        LLM_PROVIDER: 'sage',
        LLM_API_KEY: 'your-tinfoil-api-key',
        LLM_MODEL: DEFAULT_TINFOIL_MODEL,
      },
      extra: t('adminDeployment.llmHelp.sageExtra', {
        tinfoilSignupUrl: TINFOIL_SIGNUP_URL,
        defaultValue:
          'Set LLM_API_KEY to your Tinfoil key from {{tinfoilSignupUrl}}. The stack maps it to Sage and the local Tinfoil proxy.',
      }),
    },
  ];

  // Embedding help pages data
  const EMBEDDING_HELP_PAGES = [
    {
      title: t(
        'adminDeployment.embeddingHelp.overviewTitle',
        'What are Embeddings?'
      ),
      content: 'overview',
    },
    {
      title: t('adminDeployment.embeddingHelp.modelsTitle', 'Model Options'),
      content: 'models',
    },
    {
      title: t(
        'adminDeployment.embeddingHelp.performanceTitle',
        'Performance Settings'
      ),
      content: 'performance',
    },
  ];

  const DOMAINS_HELP_PAGES = [
    {
      title: t(
        'adminDeployment.domainsHelp.overviewTitle',
        'Domains & URLs Overview'
      ),
      content: 'overview',
    },
    {
      title: t('adminDeployment.domainsHelp.urlsTitle', 'Public URLs & CORS'),
      content: 'urls',
    },
    {
      title: t('adminDeployment.domainsHelp.dnsTitle', 'DNS Records (Email)'),
      content: 'dns',
    },
    {
      title: t('adminDeployment.domainsHelp.edgeTitle', 'CDN & Webhooks'),
      content: 'edge',
    },
  ];

  const STORAGE_HELP_PAGES = [
    {
      title: t(
        'adminDeployment.storageHelp.overviewTitle',
        'Data Storage Overview'
      ),
      content: 'overview',
    },
    {
      title: t('adminDeployment.storageHelp.pathsTitle', 'Paths & Volumes'),
      content: 'paths',
    },
    {
      title: t('adminDeployment.storageHelp.backupsTitle', 'Backups & Moves'),
      content: 'backups',
    },
  ];

  const SEARCH_HELP_PAGES = [
    {
      title: t(
        'adminDeployment.searchHelp.overviewTitle',
        'Web Search Overview'
      ),
      content: 'overview',
    },
    {
      title: t('adminDeployment.searchHelp.configTitle', 'Configure SearXNG'),
      content: 'config',
    },
    {
      title: t('adminDeployment.searchHelp.privacyTitle', 'Privacy & Limits'),
      content: 'privacy',
    },
  ];

  const SECURITY_HELP_PAGES = [
    {
      title: t(
        'adminDeployment.securityHelp.overviewTitle',
        'Security Overview'
      ),
      content: 'overview',
    },
    {
      title: t('adminDeployment.securityHelp.devTitle', 'Development Flags'),
      content: 'dev',
    },
    {
      title: t(
        'adminDeployment.securityHelp.frontendTitle',
        'Frontend & Sessions'
      ),
      content: 'frontend',
    },
  ];

  const SSL_HELP_PAGES = [
    {
      title: t('adminDeployment.sslHelp.overviewTitle', 'SSL & HTTPS Overview'),
      content: 'overview',
    },
    {
      title: t('adminDeployment.sslHelp.certsTitle', 'Certificates & Proxies'),
      content: 'certs',
    },
    {
      title: t('adminDeployment.sslHelp.httpsTitle', 'HTTPS Behavior'),
      content: 'https',
    },
  ];

  // Get icon for category
  const getCategoryIcon = (category: string) => {
    switch (category) {
      case 'llm':
        return <Server className="w-4 h-4 text-text-muted" />;
      case 'embedding':
        return <Database className="w-4 h-4 text-text-muted" />;
      case 'email':
        return <Mail className="w-4 h-4 text-text-muted" />;
      case 'storage':
        return <Database className="w-4 h-4 text-text-muted" />;
      case 'security':
        return <Shield className="w-4 h-4 text-text-muted" />;
      case 'search':
        return <Search className="w-4 h-4 text-text-muted" />;
      case 'domains':
        return <Globe className="w-4 h-4 text-text-muted" />;
      case 'ssl':
        return <Lock className="w-4 h-4 text-text-muted" />;
      default:
        return <Server className="w-4 h-4 text-text-muted" />;
    }
  };

  // Get status icon for service health
  const getStatusIcon = (status: ServiceHealthItem['status']) => {
    switch (status) {
      case 'healthy':
        return <CheckCircle className="w-5 h-5 text-success" />;
      case 'unhealthy':
        return <XCircle className="w-5 h-5 text-error" />;
      default:
        return <AlertCircle className="w-5 h-5 text-warning" />;
    }
  };

  const runtimeStateClass = (status?: string) => {
    if (
      status === 'current' ||
      status === 'configured' ||
      status === 'matches_desired'
    )
      return 'border-success/20 bg-success/10 text-success';
    if (
      status === 'stale' ||
      status === 'restart_required' ||
      status === 'not_generated' ||
      status === 'drifted'
    )
      return 'border-warning/20 bg-warning/10 text-warning';
    return 'border-border bg-surface text-text-muted';
  };

  const formatRuntimeState = (status?: string) => {
    if (!status) return t('adminDeployment.runtimeEnv.unknown', 'Unknown');
    return status
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (char) => char.toUpperCase());
  };

  // Check if a config key is a secret (should be masked in audit log)
  const isSecretKey = (configKey: string): boolean => {
    if (!deploymentConfig) return false;
    const allConfigs = [
      ...(deploymentConfig.llm || []),
      ...(deploymentConfig.email || []),
      ...(deploymentConfig.embedding || []),
      ...(deploymentConfig.storage || []),
      ...(deploymentConfig.security || []),
      ...(deploymentConfig.search || []),
      ...(deploymentConfig.domains || []),
      ...(deploymentConfig.ssl || []),
      ...(deploymentConfig.general || []),
    ];
    const configItem = allConfigs.find((c) => c.key === configKey);
    return configItem?.is_secret ?? false;
  };

  // Get translated deployment config item metadata
  const deploymentConfigItemMeta = getDeploymentConfigItemMeta(t);

  // Render a config item
  const renderConfigItem = (item: DeploymentConfigItem) => {
    const isEditing = editingKey === item.key;
    const isShowingSecret = showSecret === item.key;
    const meta = deploymentConfigItemMeta[item.key as DeploymentConfigItemKey];
    const label = meta?.label || item.key;
    const description = meta?.description || item.description;
    const hint = meta?.hint;
    const helpText = hint || description || item.description;

    return (
      <div
        key={item.key}
        className="bg-surface border border-border rounded-lg p-3 hover:border-border-strong transition-all"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <p className="text-sm font-medium text-text">{label}</p>
              {helpText && (
                <div className="relative" data-help-item={item.key}>
                  <button
                    onClick={(event) => {
                      event.stopPropagation();
                      setOpenHelpItemKey((current) =>
                        current === item.key ? null : item.key
                      );
                    }}
                    className="text-text-muted hover:text-accent transition-colors"
                    aria-label={t(
                      'adminDeployment.openHelpAria',
                      'Open help for {{label}}',
                      {
                        label,
                      }
                    )}
                    aria-expanded={openHelpItemKey === item.key}
                    aria-controls={`help-item-popover-${item.key}`}
                    aria-describedby={
                      openHelpItemKey === item.key
                        ? `help-item-popover-${item.key}`
                        : undefined
                    }
                    type="button"
                  >
                    <HelpCircle className="w-4 h-4" />
                  </button>
                  {openHelpItemKey === item.key && (
                    <div
                      id={`help-item-popover-${item.key}`}
                      role="tooltip"
                      className="absolute left-1/2 -translate-x-1/2 top-full mt-2 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-surface p-3 text-xs text-text-muted shadow-xl z-10"
                    >
                      {helpText}
                    </div>
                  )}
                </div>
              )}
              {item.requires_restart && (
                <span className="text-[10px] bg-warning/10 text-warning px-1.5 py-0.5 rounded">
                  {t('adminDeployment.requiresRestart', 'Requires Restart')}
                </span>
              )}
              {item.is_secret && (
                <span className="text-[10px] bg-error/10 text-error px-1.5 py-0.5 rounded">
                  {t('adminDeployment.secret', 'Secret')}
                </span>
              )}
            </div>
            <p className="text-[11px] font-mono text-text-muted mt-1">
              {item.key}
            </p>
            {description && (
              <p className="text-xs text-text-muted mt-1">{description}</p>
            )}
            {hint && (
              <p className="text-xs text-text-muted/70 mt-1 leading-relaxed">
                {hint}
              </p>
            )}
          </div>
          {!isEditing && (
            <div className="flex items-center gap-2">
              {item.is_secret && (
                <button
                  onClick={() => handleToggleSecret(item.key)}
                  disabled={revealingSecret === item.key}
                  className="text-text-muted hover:text-text transition-colors disabled:opacity-50"
                >
                  {revealingSecret === item.key ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : isShowingSecret ? (
                    <EyeOff className="w-4 h-4" />
                  ) : (
                    <Eye className="w-4 h-4" />
                  )}
                </button>
              )}
              <button
                onClick={() => handleEdit(item)}
                className="text-xs text-accent hover:text-accent-hover transition-colors"
              >
                {t('common.edit')}
              </button>
            </div>
          )}
        </div>

        {isEditing ? (
          <div className="mt-3 space-y-3">
            <input
              type={item.is_secret ? 'password' : 'text'}
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              placeholder={
                item.is_secret
                  ? t(
                      'adminDeployment.leaveEmptyForSecret',
                      'Leave empty to keep current value'
                    )
                  : item.value || ''
              }
              className="w-full border border-border rounded-lg px-3 py-2 bg-surface text-text placeholder:text-text-muted text-sm font-mono focus:outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
            />

            {saveError && (
              <div className="flex items-center gap-2 text-error text-xs">
                <AlertCircle className="w-3 h-3" />
                {saveError}
              </div>
            )}

            <div className="flex gap-2">
              <button
                onClick={handleCancel}
                className="flex-1 bg-surface-overlay border border-border text-text rounded-lg px-3 py-2 text-sm font-medium hover:bg-surface transition-all"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="flex-1 bg-accent text-accent-text rounded-lg px-3 py-2 text-sm font-medium hover:bg-accent-hover transition-all disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {saving ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Save className="w-4 h-4" />
                )}
                {t('common.save')}
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-2">
            <code className="text-xs text-text-muted bg-surface-overlay rounded px-1.5 py-0.5">
              {item.is_secret
                ? isShowingSecret
                  ? revealedSecrets[item.key] || t('admin.database.notSet')
                  : '********'
                : item.value || t('admin.database.notSet')}
            </code>
          </div>
        )}
      </div>
    );
  };

  // Get translated config categories
  const configCategories = getConfigCategories(t);

  const renderInferenceVerificationPanel = () => {
    const provider = inferenceVerificationStatus?.configured_provider;
    const statusLabel = formatVerificationStatus(
      inferenceVerificationStatus?.status
    );
    const statusTone =
      inferenceVerificationStatus?.status === 'current'
        ? 'text-success bg-success/10 border-success/20'
        : 'text-warning bg-warning/10 border-warning/20';

    return (
      <div
        id="inference-verification"
        role="group"
        aria-label={t(
          'adminDeployment.inferenceVerification.ariaLabel',
          'Inference Verification'
        )}
        className="mb-4 rounded-lg border border-border bg-surface p-4"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h4 className="text-sm font-semibold text-text flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-accent" />
              {t(
                'adminDeployment.inferenceVerification.title',
                'Inference Verification'
              )}
            </h4>
            <p className="mt-1 text-xs text-text-muted">
              {t(
                'adminDeployment.inferenceVerification.summary',
                'Admin-only evidence that protected inference is running on the expected Model Provider.'
              )}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => void loadInferenceVerification()}
              disabled={
                inferenceVerificationLoading || manualVerificationLoading
              }
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text disabled:opacity-50"
            >
              <RefreshCw
                className={`w-3.5 h-3.5 ${inferenceVerificationLoading ? 'animate-spin' : ''}`}
              />
              {t('adminDeployment.refresh', 'Refresh')}
            </button>
            <button
              onClick={() => void handleManualInferenceVerification()}
              disabled={manualVerificationLoading}
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1.5 text-xs font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50"
            >
              {manualVerificationLoading ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <ShieldCheck className="w-3.5 h-3.5" />
              )}
              {t(
                'adminDeployment.inferenceVerification.verifyNow',
                'Verify Now'
              )}
            </button>
          </div>
        </div>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="rounded-lg border border-border bg-surface-overlay p-3">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs uppercase tracking-wide text-text-muted">
                {t('adminDeployment.inferenceVerification.status', 'Status')}
              </span>
              <span
                className={`rounded-full border px-2 py-0.5 text-xs font-medium ${statusTone}`}
              >
                {statusLabel}
              </span>
            </div>
            <dl className="mt-3 space-y-2 text-xs">
              <div className="flex justify-between gap-3">
                <dt className="text-text-muted">
                  {t(
                    'adminDeployment.inferenceVerification.checkedAt',
                    'Checked'
                  )}
                </dt>
                <dd className="text-right text-text">
                  {formatTimestamp(
                    inferenceVerificationStatus?.checked_at ??
                      inferenceVerificationStatus?.record?.checked_at
                  )}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-text-muted">
                  {t(
                    'adminDeployment.inferenceVerification.expiresAt',
                    'Expires'
                  )}
                </dt>
                <dd className="text-right text-text">
                  {formatTimestamp(
                    inferenceVerificationStatus?.expires_at ??
                      inferenceVerificationStatus?.record?.expires_at
                  )}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="sr-only">
                  {t(
                    'adminDeployment.inferenceVerification.expectedClaims',
                    'Expected claims'
                  )}
                </dt>
                <dd className="max-w-full truncate text-right font-mono text-text">
                  {t(
                    'adminDeployment.inferenceVerification.expectedClaimsValue',
                    'Expected claims: {{fingerprint}}',
                    {
                      fingerprint:
                        inferenceVerificationStatus?.expected_claims_fingerprint ??
                        t('common.unknown', 'Unknown'),
                    }
                  )}
                </dd>
              </div>
            </dl>
          </div>

          <div className="rounded-lg border border-border bg-surface-overlay p-3">
            <span className="text-xs uppercase tracking-wide text-text-muted">
              {t(
                'adminDeployment.inferenceVerification.providerClaims',
                'Provider Claims'
              )}
            </span>
            <dl className="mt-3 space-y-2 text-xs">
              <div className="flex justify-between gap-3">
                <dt className="text-text-muted">
                  {t(
                    'adminDeployment.inferenceVerification.provider',
                    'Provider'
                  )}
                </dt>
                <dd className="text-right text-text">
                  {provider?.provider_identity ??
                    t('common.unknown', 'Unknown')}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-text-muted">
                  {t('adminDeployment.inferenceVerification.model', 'Model')}
                </dt>
                <dd className="text-right text-text">
                  {provider?.model_identifier ?? t('common.unknown', 'Unknown')}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-text-muted">
                  {t(
                    'adminDeployment.inferenceVerification.endpoint',
                    'Endpoint'
                  )}
                </dt>
                <dd className="max-w-[12rem] truncate text-right text-text">
                  {provider?.provider_endpoint ??
                    t('common.unknown', 'Unknown')}
                </dd>
              </div>
            </dl>
          </div>
        </div>

        {inferenceVerificationError && (
          <p className="mt-3 text-xs text-error">
            {inferenceVerificationError}
          </p>
        )}
        {manualVerificationMessage && (
          <p className="mt-3 text-xs font-medium text-success">
            {manualVerificationMessage}
          </p>
        )}
        {manualVerificationError && (
          <p className="mt-3 text-xs font-medium text-error">
            {manualVerificationError}
          </p>
        )}

        <div className="mt-4 border-t border-border pt-4">
          <h5 className="text-xs font-semibold uppercase tracking-wide text-text-muted flex items-center gap-2">
            <History className="w-3.5 h-3.5" />
            {t(
              'adminDeployment.inferenceVerification.history',
              'Verification History'
            )}
          </h5>
          <div className="mt-2 space-y-2">
            {inferenceVerificationRecords.length === 0 ? (
              <p className="text-xs text-text-muted">
                {t(
                  'adminDeployment.inferenceVerification.emptyHistory',
                  'No verification records yet.'
                )}
              </p>
            ) : (
              inferenceVerificationRecords.map((record) => (
                <div
                  key={record.id}
                  className="rounded-lg border border-border bg-surface-overlay p-3"
                >
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <p className="text-sm font-medium text-text">
                        {t(
                          'adminDeployment.inferenceVerification.recordTitle',
                          'Record #{{id}}',
                          { id: record.id }
                        )}
                      </p>
                      <p className="text-xs text-text-muted">
                        {formatVerificationStatus(record.status)} ·{' '}
                        {record.trigger ?? t('common.unknown', 'Unknown')} ·{' '}
                        {formatTimestamp(record.checked_at)}
                      </p>
                    </div>
                    <button
                      onClick={() =>
                        void handleInspectInferenceVerificationRecord(record.id)
                      }
                      className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text"
                    >
                      <Eye className="w-3.5 h-3.5" />
                      {t(
                        'adminDeployment.inferenceVerification.inspectAttestation',
                        'Inspect Attestation'
                      )}
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>

          {selectedVerificationRecordId && (
            <div className="mt-3 rounded-lg border border-border bg-surface-overlay p-3">
              <h6 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                {t(
                  'adminDeployment.inferenceVerification.fullAttestation',
                  'Full Attestation Material'
                )}
              </h6>
              {selectedVerificationLoading && (
                <div className="mt-2 flex items-center gap-2 text-xs text-text-muted">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  {t('common.loading', 'Loading')}
                </div>
              )}
              {selectedVerificationError && (
                <p className="mt-2 text-xs text-error">
                  {selectedVerificationError}
                </p>
              )}
              {selectedVerificationRecord && (
                <pre className="mt-2 max-h-72 overflow-auto rounded-md bg-surface px-3 py-2 text-xs text-text font-mono whitespace-pre-wrap">
                  {JSON.stringify(
                    selectedVerificationRecord.attestation_material ??
                      selectedVerificationRecord,
                    null,
                    2
                  )}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
    );
  };

  const renderDeploymentReadinessPanel = () => {
    const summary = deploymentReadiness?.summary;
    const items = deploymentReadiness?.items ?? [];
    const status =
      deploymentReadiness?.status ??
      (readinessLoading ? 'loading' : readinessError ? 'failed' : 'unknown');
    const wizardItem =
      items[Math.min(readinessWizardIndex, Math.max(items.length - 1, 0))];
    const wizardTarget = wizardItem ? readinessReviewTarget(wizardItem) : null;
    const isBlocked = status === 'blocked';
    const isReady = status === 'ready';
    const statusIcon = isReady ? (
      <CheckCircle className="w-4 h-4 text-success" />
    ) : isBlocked ? (
      <AlertCircle className="w-4 h-4 text-error" />
    ) : (
      <AlertCircle className="w-4 h-4 text-warning" />
    );

    return (
      <Card
        role="group"
        aria-label={t(
          'adminDeployment.readiness.title',
          'Deployment Readiness'
        )}
        className="bg-surface-overlay"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h3 className="heading-sm flex items-center gap-2">
              {statusIcon}
              {t(
                'adminDeployment.readiness.statusTitle',
                'Deployment Readiness: {{status}}',
                {
                  status: formatLifecycleStatus(status),
                }
              )}
            </h3>
            <p className="mt-1 text-sm text-text-secondary">
              {t(
                'adminDeployment.readiness.description',
                'Review the checks for this prototype launch. Warnings are advisory unless marked as blockers.'
              )}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setReadinessWizardIndex(0);
                setShowReadinessWizard(true);
              }}
              disabled={items.length === 0}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text disabled:opacity-50"
            >
              <ShieldCheck className="w-3.5 h-3.5" />
              {t(
                'adminDeployment.readiness.openWizard',
                'Open Readiness Review'
              )}
            </button>
            <button
              type="button"
              onClick={() => void refreshReadiness()}
              disabled={readinessLoading}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text disabled:opacity-50"
            >
              <RefreshCw
                className={`w-3.5 h-3.5 ${readinessLoading ? 'animate-spin' : ''}`}
              />
              {t('adminDeployment.refresh', 'Refresh')}
            </button>
          </div>
        </div>

        {summary && (
          <div className="mt-4 grid grid-cols-3 gap-2 text-center text-xs">
            <div className="rounded-lg border border-error/20 bg-error/10 px-2 py-2 text-error">
              {t(
                'adminDeployment.readiness.blockerCount',
                '{{count}} blockers',
                { count: summary.blockers }
              )}
            </div>
            <div className="rounded-lg border border-warning/20 bg-warning/10 px-2 py-2 text-warning">
              {t(
                'adminDeployment.readiness.warningCount',
                '{{count}} warnings',
                { count: summary.warnings }
              )}
            </div>
            <div className="rounded-lg border border-success/20 bg-success/10 px-2 py-2 text-success">
              {t('adminDeployment.readiness.readyCount', '{{count}} ready', {
                count: summary.ready,
              })}
            </div>
          </div>
        )}

        {readinessError && (
          <p className="mt-4 text-sm text-error">{readinessError}</p>
        )}

        <div className="mt-4 space-y-2">
          {items.length === 0 ? (
            <p className="text-sm text-text-muted">
              {readinessLoading
                ? t('common.loading', 'Loading')
                : t(
                    'adminDeployment.readiness.empty',
                    'No Deployment Readiness checks are available yet.'
                  )}
            </p>
          ) : (
            items.map((item) => {
              const target = readinessReviewTarget(item);
              return (
                <div
                  key={item.key}
                  className="rounded-lg border border-border bg-surface p-3"
                >
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <p className="text-sm font-medium text-text">
                        {readinessLabel(item)}
                      </p>
                      <p className="mt-1 text-xs text-text-secondary">
                        {readinessSummary(item)}
                      </p>
                      <p className="mt-2 text-xs text-text-muted">
                        {readinessNextAction(item)}
                      </p>
                    </div>
                    <span
                      className={`self-start rounded-full border px-2 py-0.5 text-xs font-medium ${readinessToneClass(item.severity)}`}
                    >
                      {formatReadinessSeverity(item.severity)}
                    </span>
                  </div>
                  {target && (
                    <a
                      href={target.href}
                      className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:text-accent-hover"
                    >
                      {target.label}
                    </a>
                  )}
                  {item.conversation_blocking && (
                    <p className="mt-2 text-xs font-medium text-error">
                      {t(
                        'adminDeployment.readiness.conversationBlocking',
                        'Blocks normal Conversations'
                      )}
                    </p>
                  )}
                </div>
              );
            })
          )}
        </div>

        {showReadinessWizard && wizardItem && wizardTarget && (
          <div
            role="dialog"
            aria-label={t('adminDeployment.wizard.title', 'Deployment Wizard')}
            className="mt-4 rounded-lg border border-border bg-surface p-4"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-text-muted">
                  {t(
                    'adminDeployment.wizard.step',
                    'Step {{current}} of {{total}}',
                    {
                      current: Math.min(readinessWizardIndex + 1, items.length),
                      total: items.length,
                    }
                  )}
                </p>
                <h4 className="mt-1 text-base font-semibold text-text">
                  {t('adminDeployment.wizard.title', 'Deployment Wizard')}
                </h4>
              </div>
              <button
                type="button"
                onClick={() => setShowReadinessWizard(false)}
                className="rounded-md p-1 text-text-muted hover:text-text"
                aria-label={t('common.close', 'Close')}
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="mt-4 rounded-lg border border-border bg-surface-overlay p-3">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <p className="text-sm font-semibold text-text">
                    {readinessLabel(wizardItem)}
                  </p>
                  <p className="mt-1 text-xs text-text-secondary">
                    {readinessSummary(wizardItem)}
                  </p>
                  <p className="mt-2 text-xs text-text-muted">
                    {readinessNextAction(wizardItem)}
                  </p>
                </div>
                <span
                  className={`self-start rounded-full border px-2 py-0.5 text-xs font-medium ${readinessToneClass(wizardItem.severity)}`}
                >
                  {formatReadinessSeverity(wizardItem.severity)}
                </span>
              </div>
              <a
                href={wizardTarget.href}
                className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:text-accent-hover"
              >
                {wizardTarget.label}
              </a>
            </div>

            <div className="mt-4 flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() =>
                  setReadinessWizardIndex((current) => Math.max(current - 1, 0))
                }
                disabled={readinessWizardIndex === 0}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text disabled:opacity-50"
              >
                <ChevronLeft className="w-3.5 h-3.5" />
                {t('common.previous', 'Previous')}
              </button>
              <button
                type="button"
                onClick={() =>
                  setReadinessWizardIndex((current) =>
                    Math.min(current + 1, items.length - 1)
                  )
                }
                disabled={readinessWizardIndex >= items.length - 1}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text-secondary hover:text-text disabled:opacity-50"
              >
                {t('common.next', 'Next')}
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )}
      </Card>
    );
  };

  // Render a config category section
  const renderCategory = (
    category: ConfigCategory,
    items: DeploymentConfigItem[]
  ) => {
    if (items.length === 0) return null;

    const meta = configCategories[category];
    const helpText = meta.hint || meta.description;
    const hasModalHelp =
      category === 'email' ||
      category === 'llm' ||
      category === 'embedding' ||
      category === 'domains' ||
      category === 'storage' ||
      category === 'search' ||
      category === 'security' ||
      category === 'ssl';
    return (
      <Card
        key={category}
        role="group"
        aria-label={t(
          'adminDeployment.categorySettingsAria',
          '{{category}} Settings',
          {
            category: meta.label,
          }
        )}
        className="bg-surface-overlay"
      >
        <h3 className="heading-sm mb-2 flex items-center gap-2">
          {getCategoryIcon(category)}
          {meta.label}
          {category === 'email' && (
            <button
              onClick={() => setShowEmailHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.emailHelp.ariaLabel',
                'Email configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {category === 'llm' && (
            <button
              onClick={() => setShowLlmHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.llmHelp.ariaLabel',
                'Sage + Tinfoil configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {category === 'embedding' && (
            <button
              onClick={() => setShowEmbeddingHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.embeddingHelp.ariaLabel',
                'Embedding configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {category === 'domains' && (
            <button
              onClick={() => setShowDomainsHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.domainsHelp.ariaLabel',
                'Domains and DNS configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {category === 'storage' && (
            <button
              onClick={() => setShowStorageHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.storageHelp.ariaLabel',
                'Data storage configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {category === 'search' && (
            <button
              onClick={() => setShowSearchHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.searchHelp.ariaLabel',
                'Web search configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {category === 'security' && (
            <button
              onClick={() => setShowSecurityHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.securityHelp.ariaLabel',
                'Security configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {category === 'ssl' && (
            <button
              onClick={() => setShowSslHelpModal(true)}
              className="ml-1 text-text-muted hover:text-accent transition-colors"
              aria-label={t(
                'adminDeployment.sslHelp.ariaLabel',
                'SSL and HTTPS configuration help'
              )}
              title={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </button>
          )}
          {!hasModalHelp && helpText && (
            <span
              className="ml-1 text-text-muted"
              title={helpText}
              aria-label={helpText}
            >
              <HelpCircle className="w-5 h-5" />
            </span>
          )}
        </h3>
        <p className="text-sm text-text-secondary mb-1">{meta.description}</p>
        {'hint' in meta && meta.hint && (
          <p className="text-xs text-text-muted mb-4">{meta.hint}</p>
        )}

        {category === 'llm' && renderInferenceVerificationPanel()}

        <div className="space-y-2">{items.map(renderConfigItem)}</div>
      </Card>
    );
  };

  const footer = (
    <Link
      to="/admin/setup"
      className="text-text-muted hover:text-text transition-colors"
    >
      {t('common.backToAdminDashboard', 'Back to Admin Dashboard')}
    </Link>
  );

  if (!authChecked || configLoading) {
    return (
      <div className="min-h-screen bg-surface flex items-center justify-center">
        <Loader2 className="w-8 h-8 text-accent animate-spin" />
      </div>
    );
  }

  return (
    <OnboardingCard
      size="xl"
      title={t('adminDeployment.title', 'Deployment Configuration')}
      subtitle={t(
        'adminDeployment.subtitle',
        'Manage your server connections and infrastructure settings'
      )}
      footer={footer}
    >
      <div className="space-y-6">
        <div id="deployment-settings" className="sr-only" aria-hidden="true" />

        {/* Error display */}
        {configError && (
          <div className="bg-error/10 border border-error/20 rounded-xl p-4">
            <p className="text-sm text-error">{translatedConfigError}</p>
          </div>
        )}

        {renderDeploymentReadinessPanel()}

        {/* Service Health Section */}
        <Card id="service-health">
          <div className="flex items-center justify-between mb-2">
            <h3 className="heading-sm flex items-center gap-2">
              <Server className="w-4 h-4 text-text-muted" />
              {t('adminDeployment.serviceHealth', 'Service Health')}
            </h3>
            <button
              onClick={refreshHealth}
              disabled={healthLoading}
              className="flex items-center gap-1.5 text-xs text-accent hover:text-accent-hover transition-colors disabled:opacity-50"
            >
              <RefreshCw
                className={`w-3.5 h-3.5 ${healthLoading ? 'animate-spin' : ''}`}
              />
              {t('adminDeployment.refresh', 'Refresh')}
            </button>
          </div>
          <p className="text-sm text-text-secondary mb-1">
            {t(
              'adminDeployment.serviceHealthDesc',
              'Monitor your connected services'
            )}
          </p>
          <p className="text-xs text-text-muted mb-4">
            {t(
              'adminDeployment.serviceHealthHint',
              'Green means the service is responding normally. If a service shows red, check its configuration below.'
            )}
          </p>

          {health?.runtime_env?.sage && (
            <div
              id="runtime-config-alignment"
              className="rounded-lg border border-border p-3 mb-4"
            >
              <div className="flex items-center justify-between gap-3 mb-3">
                <div>
                  <h4 className="text-sm font-medium text-text">
                    {t(
                      'adminDeployment.runtimeEnv.title',
                      'Runtime Config Alignment'
                    )}
                  </h4>
                  <p className="text-xs text-text-muted mt-1">
                    {t(
                      'adminDeployment.runtimeEnv.description',
                      'Compares desired Deployment Settings, generated Sage env, and observable running state.'
                    )}
                  </p>
                </div>
                <button
                  onClick={handleExportSageRuntimeEnv}
                  className="shrink-0 flex items-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface"
                >
                  <Download className="w-3.5 h-3.5" />
                  {t('adminDeployment.exportSageRuntimeEnv', 'Export Sage env')}
                </button>
                <button
                  onClick={handleExportCoreBackendRuntimeEnv}
                  className="shrink-0 flex items-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface"
                >
                  <Download className="w-3.5 h-3.5" />
                  {t(
                    'adminDeployment.exportCoreBackendRuntimeEnv',
                    'Export core-backend env'
                  )}
                </button>
              </div>
              <div className="grid gap-2 md:grid-cols-3">
                <div
                  className={`rounded-lg border p-3 ${runtimeStateClass(health.runtime_env.sage.desired?.status)}`}
                >
                  <p className="text-xs font-medium">
                    {t('adminDeployment.runtimeEnv.desired', 'Desired')}
                  </p>
                  <p className="text-sm font-semibold mt-1">
                    {formatRuntimeState(
                      health.runtime_env.sage.desired?.status
                    )}
                  </p>
                  <p className="text-xs mt-1 text-text-muted">
                    {t(
                      'adminDeployment.runtimeEnv.configuredKeys',
                      '{{count}} of {{total}} keys configured',
                      {
                        count:
                          health.runtime_env.sage.desired?.configured_keys ?? 0,
                        total: health.runtime_env.sage.desired?.total_keys ?? 0,
                      }
                    )}
                  </p>
                </div>
                <div
                  className={`rounded-lg border p-3 ${runtimeStateClass(health.runtime_env.sage.generated?.status)}`}
                >
                  <p className="text-xs font-medium">
                    {t('adminDeployment.runtimeEnv.generated', 'Generated')}
                  </p>
                  <p className="text-sm font-semibold mt-1">
                    {formatRuntimeState(
                      health.runtime_env.sage.generated?.status
                    )}
                  </p>
                  <p className="text-xs mt-1 text-text-muted">
                    {health.runtime_env.sage.generated?.latest_export_at
                      ? t(
                          'adminDeployment.runtimeEnv.exportedAt',
                          'Exported {{time}}',
                          {
                            time: formatTimestamp(
                              health.runtime_env.sage.generated.latest_export_at
                            ),
                          }
                        )
                      : t(
                          'adminDeployment.runtimeEnv.notExported',
                          'No Sage env export recorded'
                        )}
                  </p>
                </div>
                <div
                  className={`rounded-lg border p-3 ${runtimeStateClass(health.runtime_env.sage.running?.status)}`}
                >
                  <p className="text-xs font-medium">
                    {t('adminDeployment.runtimeEnv.running', 'Running')}
                  </p>
                  <p className="text-sm font-semibold mt-1">
                    {formatRuntimeState(
                      health.runtime_env.sage.running?.status
                    )}
                  </p>
                  <p className="text-xs mt-1 text-text-muted">
                    {health.runtime_env.sage.running?.summary ||
                      t(
                        'adminDeployment.runtimeEnv.runningUnknown',
                        'Running state is not directly introspected yet.'
                      )}
                  </p>
                </div>
              </div>
              {health.runtime_env.core_backend && (
                <div className="mt-3 border-t border-border pt-3">
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <p className="text-sm font-medium text-text">
                      {t(
                        'adminDeployment.runtimeEnv.coreBackend',
                        'Core Backend'
                      )}
                    </p>
                    <p className="text-xs text-text-muted">
                      {t(
                        'adminDeployment.runtimeEnv.coreBackendHint',
                        'Backend process env alignment'
                      )}
                    </p>
                  </div>
                  <div className="grid gap-2 md:grid-cols-3">
                    <div
                      className={`rounded-lg border p-3 ${runtimeStateClass(health.runtime_env.core_backend.desired?.status)}`}
                    >
                      <p className="text-xs font-medium">
                        {t(
                          'adminDeployment.runtimeEnv.coreDesired',
                          'Core Desired'
                        )}
                      </p>
                      <p className="text-sm font-semibold mt-1">
                        {formatRuntimeState(
                          health.runtime_env.core_backend.desired?.status
                        )}
                      </p>
                      <p className="text-xs mt-1 text-text-muted">
                        {t(
                          'adminDeployment.runtimeEnv.coreConfiguredKeys',
                          'Core backend: {{count}} of {{total}} keys configured',
                          {
                            count:
                              health.runtime_env.core_backend.desired
                                ?.configured_keys ?? 0,
                            total:
                              health.runtime_env.core_backend.desired
                                ?.total_keys ?? 0,
                          }
                        )}
                      </p>
                    </div>
                    <div
                      className={`rounded-lg border p-3 ${runtimeStateClass(health.runtime_env.core_backend.generated?.status)}`}
                    >
                      <p className="text-xs font-medium">
                        {t(
                          'adminDeployment.runtimeEnv.coreGenerated',
                          'Core Generated'
                        )}
                      </p>
                      <p className="text-sm font-semibold mt-1">
                        {formatRuntimeState(
                          health.runtime_env.core_backend.generated?.status
                        )}
                      </p>
                      <p className="text-xs mt-1 text-text-muted">
                        {health.runtime_env.core_backend.generated
                          ?.latest_export_at
                          ? t(
                              'adminDeployment.runtimeEnv.exportedAt',
                              'Exported {{time}}',
                              {
                                time: formatTimestamp(
                                  health.runtime_env.core_backend.generated
                                    .latest_export_at
                                ),
                              }
                            )
                          : t(
                              'adminDeployment.runtimeEnv.coreNotExported',
                              'No core backend env export recorded'
                            )}
                      </p>
                    </div>
                    <div
                      className={`rounded-lg border p-3 ${runtimeStateClass(health.runtime_env.core_backend.running?.status)}`}
                    >
                      <p className="text-xs font-medium">
                        {t(
                          'adminDeployment.runtimeEnv.coreRunning',
                          'Core Running'
                        )}
                      </p>
                      <p className="text-sm font-semibold mt-1">
                        {formatRuntimeState(
                          health.runtime_env.core_backend.running?.status
                        )}
                      </p>
                      <p className="text-xs mt-1 text-text-muted">
                        {health.runtime_env.core_backend.running?.summary ||
                          t(
                            'adminDeployment.runtimeEnv.coreRunningUnknown',
                            'Core backend running state is not directly introspected yet.'
                          )}
                      </p>
                    </div>
                  </div>
                </div>
              )}
              {coreBackendRuntimeEnvExported && (
                <div className="mt-3 rounded-lg border border-warning/20 bg-warning/10 p-3">
                  <p className="text-sm font-semibold text-warning">
                    {t(
                      'adminDeployment.runtimeEnv.coreExportedTitle',
                      'Core-backend env exported'
                    )}
                  </p>
                  <p className="mt-1 text-xs text-text-secondary">
                    {t(
                      'adminDeployment.runtimeEnv.coreSensitiveArtifact',
                      'Treat core-backend.env as sensitive deployment material.'
                    )}
                  </p>
                  <code className="mt-2 block overflow-x-auto rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text">
                    docker compose --env-file .env --env-file
                    runtime/generated/core-backend.env -f
                    docker-compose.infra.yml -f docker-compose.app.yml up -d
                    core-backend
                  </code>
                  <p className="mt-2 text-xs text-text-secondary">
                    {t(
                      'adminDeployment.runtimeEnv.coreExportDoesNotApply',
                      'Exporting the artifact does not change the running backend process until an Operator applies it and restarts core-backend.'
                    )}
                  </p>
                </div>
              )}
              {sageRuntimeEnvExported && (
                <div className="mt-3 rounded-lg border border-warning/20 bg-warning/10 p-3">
                  <p className="text-sm font-semibold text-warning">
                    {t(
                      'adminDeployment.runtimeEnv.exportedTitle',
                      'Sage env exported'
                    )}
                  </p>
                  <p className="mt-1 text-xs text-text-secondary">
                    {t(
                      'adminDeployment.runtimeEnv.sensitiveArtifact',
                      'Treat sage.env as sensitive deployment material.'
                    )}
                  </p>
                  <code className="mt-2 block overflow-x-auto rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text">
                    docker compose --env-file .env --env-file
                    runtime/generated/sage.env -f docker-compose.infra.yml -f
                    docker-compose.app.yml up -d sage
                  </code>
                  <p className="mt-2 text-xs text-text-secondary">
                    {t(
                      'adminDeployment.runtimeEnv.exportDoesNotApply',
                      'Exporting the artifact does not change the running Sage process until an Operator applies it and restarts Sage.'
                    )}
                  </p>
                </div>
              )}
            </div>
          )}

          <div
            id="restart-required"
            className={`rounded-lg border p-3 mb-4 ${
              health?.restart_required
                ? 'border-warning/20 bg-warning/10'
                : 'border-success/20 bg-success/10'
            }`}
          >
            <div
              className={`flex items-center gap-2 text-sm font-medium ${health?.restart_required ? 'text-warning' : 'text-success'}`}
            >
              {health?.restart_required ? (
                <AlertCircle className="w-4 h-4" />
              ) : (
                <CheckCircle className="w-4 h-4" />
              )}
              {health?.restart_required
                ? t(
                    'adminDeployment.restartRequired',
                    'Service restart required'
                  )
                : t('adminDeployment.restartCurrent', 'No restart required')}
            </div>
            {health?.restart_required ? (
              Array.isArray(health.changed_keys_requiring_restart) &&
              health.changed_keys_requiring_restart.length > 0 && (
                <p className="text-xs text-text-muted mt-1">
                  {t(
                    'adminDeployment.extracted.changed_keys_6dd885',
                    'Changed keys:'
                  )}{' '}
                  {health.changed_keys_requiring_restart.join(', ')}
                </p>
              )
            ) : (
              <p className="text-xs text-text-muted mt-1">
                {t(
                  'adminDeployment.restartCurrentDesc',
                  'No restart-required Deployment Settings have changed since service start.'
                )}
              </p>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            {health?.services.map((service) => (
              <div
                key={service.name}
                className={`bg-surface border rounded-lg p-3 ${
                  service.status === 'healthy'
                    ? 'border-success/30'
                    : service.status === 'unhealthy'
                      ? 'border-error/30'
                      : 'border-border'
                }`}
              >
                <div className="flex items-center gap-2">
                  {getStatusIcon(service.status)}
                  <span className="text-sm font-medium text-text">
                    {service.name}
                  </span>
                </div>
                {service.response_time_ms != null && (
                  <p className="text-xs text-text-muted mt-1 flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {service.response_time_ms}ms
                  </p>
                )}
                {service.error && (
                  <p className="text-xs text-error mt-1">{service.error}</p>
                )}
                {/* Add test email button for SMTP service */}
                {service.name === 'SMTP' && (
                  <button
                    onClick={() => setShowTestEmailModal(true)}
                    className="mt-2 flex items-center gap-1.5 text-xs text-accent hover:text-accent-hover transition-colors"
                  >
                    <Send className="w-3 h-3" />
                    {t('adminDeployment.sendTestEmail', 'Send Test Email')}
                  </button>
                )}
              </div>
            ))}
          </div>
        </Card>

        {/* Data Lifecycle Status Section */}
        <Card
          id="data-lifecycle-status"
          role="group"
          aria-label={t(
            'adminDeployment.lifecycle.title',
            'Data Lifecycle Status'
          )}
        >
          <div className="flex items-start justify-between gap-3 mb-2">
            <h3 className="heading-sm flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-text-muted" />
              {t('adminDeployment.lifecycle.title', 'Data Lifecycle Status')}
            </h3>
            {lifecycleLoading && (
              <Loader2 className="w-4 h-4 animate-spin text-text-muted" />
            )}
          </div>
          <p className="text-sm text-text-secondary mb-4">
            {t(
              'adminDeployment.lifecycle.description',
              'Current Operator-Controlled Privacy coverage across Instance data.'
            )}
          </p>
          {lifecycleStatus?.lifecycle_scope && (
            <div className="mb-4 rounded-lg border border-border bg-surface p-3">
              <p className="text-xs font-medium text-text">
                {lifecycleStatus.lifecycle_scope.label}
              </p>
              <p className="mt-1 text-xs text-text-secondary">
                {lifecycleStatus.lifecycle_scope.summary}
              </p>
              <p className="mt-1 text-xs text-text-muted">
                {t(
                  'adminDeployment.lifecycle.scopeExcludes',
                  'Excludes: {{excludes}}',
                  { excludes: lifecycleStatus.lifecycle_scope.excludes }
                )}
              </p>
            </div>
          )}
          {lifecycleStatus?.secure_erase && (
            <div className="mb-4 rounded-lg border border-border bg-surface p-3">
              <p className="text-xs font-medium text-text">
                {t(
                  'adminDeployment.lifecycle.secureEraseStatus',
                  'Secure Erase: {{status}}',
                  {
                    status: formatLifecycleStatus(
                      lifecycleStatus.secure_erase.status
                    ),
                  }
                )}
              </p>
              <p className="mt-1 text-xs text-text-secondary">
                {lifecycleStatus.secure_erase.summary}
              </p>
            </div>
          )}
          {lifecycleStatus?.lifecycle_readiness && (
            <div
              role="status"
              className="mb-4 rounded-lg border border-warning/30 bg-warning/10 p-3"
            >
              <p className="text-xs font-medium text-text">
                {t(
                  'adminDeployment.lifecycle.readinessStatus',
                  'Lifecycle Readiness: {{status}}',
                  {
                    status: formatLifecycleStatus(
                      lifecycleStatus.lifecycle_readiness.status
                    ),
                  }
                )}
              </p>
              <p className="mt-1 text-xs text-text-secondary">
                {lifecycleStatus.lifecycle_readiness.summary}
              </p>
              <p className="mt-1 text-xs text-text-muted">
                {t(
                  'adminDeployment.lifecycle.readinessNonBlocking',
                  'User Conversations are not blocked by Lifecycle Readiness warnings in v1.'
                )}
              </p>
              {lifecycleStatus.lifecycle_readiness.stale_reason && (
                <p className="mt-1 text-xs text-text-muted">
                  {t(
                    'adminDeployment.lifecycle.readinessStaleReason',
                    'Reason: {{reason}}',
                    {
                      reason: formatLifecycleStatus(
                        lifecycleStatus.lifecycle_readiness.stale_reason
                      ),
                    }
                  )}
                </p>
              )}
            </div>
          )}
          {(lifecycleStatus?.content_encryption ||
            lifecycleStatus?.artifact_encryption ||
            lifecycleStatus?.retention_scheduler) && (
            <div className="mb-4 grid gap-3 md:grid-cols-3">
              {lifecycleStatus?.content_encryption && (
                <div className="rounded-lg border border-border bg-surface p-3">
                  <p className="text-xs font-medium text-text">
                    {t(
                      'adminDeployment.lifecycle.contentEncryptionKey',
                      'Content Encryption Key: {{status}}',
                      {
                        status: formatLifecycleStatus(
                          lifecycleStatus.content_encryption.status
                        ),
                      }
                    )}
                  </p>
                  <p className="mt-1 text-xs text-text-secondary">
                    {lifecycleStatus.content_encryption.summary}
                  </p>
                </div>
              )}
              {lifecycleStatus?.artifact_encryption && (
                <div className="rounded-lg border border-border bg-surface p-3">
                  <p className="text-xs font-medium text-text">
                    {t(
                      'adminDeployment.lifecycle.artifactEncryptionPosture',
                      'Artifact Encryption Posture: {{status}}',
                      {
                        status: formatLifecycleStatus(
                          lifecycleStatus.artifact_encryption.status
                        ),
                      }
                    )}
                  </p>
                  <p className="mt-1 text-xs text-text-secondary">
                    {lifecycleStatus.artifact_encryption.summary}
                  </p>
                  <div
                    className="mt-3 inline-flex rounded-lg border border-border bg-background p-1"
                    role="group"
                    aria-label={t(
                      'adminDeployment.lifecycle.artifactPostureLabel',
                      'Artifact encryption posture'
                    )}
                  >
                    {(['required', 'disabled'] as const).map((posture) => (
                      <button
                        key={posture}
                        type="button"
                        onClick={() => handleArtifactPostureChange(posture)}
                        disabled={artifactPostureUpdating}
                        aria-pressed={
                          lifecycleStatus.artifact_encryption?.posture ===
                          posture
                        }
                        className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                          lifecycleStatus.artifact_encryption?.posture ===
                          posture
                            ? 'bg-accent text-white'
                            : 'text-text-secondary hover:bg-surface-overlay'
                        } disabled:cursor-not-allowed disabled:opacity-70`}
                      >
                        {posture === 'required'
                          ? t(
                              'adminDeployment.lifecycle.artifactPostureRequired',
                              'Required'
                            )
                          : t(
                              'adminDeployment.lifecycle.artifactPostureDisabled',
                              'Disabled'
                            )}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {lifecycleStatus?.retention_scheduler && (
                <div className="rounded-lg border border-border bg-surface p-3">
                  <p className="text-xs font-medium text-text">
                    {t(
                      'adminDeployment.lifecycle.retentionSchedulerStatus',
                      'Retention Scheduler: {{status}}',
                      {
                        status: formatLifecycleStatus(
                          lifecycleStatus.retention_scheduler.status
                        ),
                      }
                    )}
                  </p>
                  <p className="mt-1 text-xs text-text-secondary">
                    {lifecycleStatus.retention_scheduler.summary}
                  </p>
                  {lifecycleStatus.retention_scheduler.observation &&
                    (() => {
                      const observation =
                        lifecycleStatus.retention_scheduler.observation;
                      const enabledClasses = Array.isArray(
                        observation.enabled_classes
                      )
                        ? observation.enabled_classes
                        : [];
                      const observationStatus =
                        typeof observation.status === 'string'
                          ? formatLifecycleStatus(observation.status)
                          : t('common.unknown', 'Unknown');
                      const lastRunStatus =
                        typeof observation.last_run?.status === 'string'
                          ? formatLifecycleStatus(observation.last_run.status)
                          : t('common.unknown', 'Unknown');
                      return (
                        <div className="mt-2 space-y-1 text-xs text-text-secondary">
                          <p className="font-medium text-text">
                            {t(
                              'adminDeployment.lifecycle.schedulerObservation',
                              'Observation: {{status}}',
                              {
                                status: observationStatus,
                              }
                            )}
                          </p>
                          <p>{observation.summary}</p>
                          <p>
                            {t(
                              'adminDeployment.lifecycle.schedulerEnabledClasses',
                              'Scheduler enabled classes: {{classes}}',
                              {
                                classes:
                                  enabledClasses.length > 0
                                    ? enabledClasses.join(', ')
                                    : t('common.none', 'None'),
                              }
                            )}
                          </p>
                          {observation.last_run && (
                            <>
                              <p>
                                {t(
                                  'adminDeployment.lifecycle.schedulerLastRunStatus',
                                  'Last run status: {{status}}',
                                  {
                                    status: lastRunStatus,
                                  }
                                )}
                              </p>
                              <p>
                                {t(
                                  'adminDeployment.lifecycle.schedulerLastRun',
                                  'Last run: {{trigger}} by {{actor}}',
                                  {
                                    trigger: observation.last_run.trigger,
                                    actor: observation.last_run.actor,
                                  }
                                )}
                              </p>
                            </>
                          )}
                        </div>
                      );
                    })()}
                </div>
              )}
            </div>
          )}
          <div className="mb-4 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={handlePreviewRetention}
              disabled={retentionPreviewLoading}
              className="inline-flex items-center justify-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {retentionPreviewLoading ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Eye className="w-3.5 h-3.5" />
              )}
              {t(
                'adminDeployment.lifecycle.previewRetention',
                'Preview Retention'
              )}
            </button>
            <button
              type="button"
              onClick={handleRunScheduledRetention}
              disabled={scheduledRetentionLoading}
              className="inline-flex items-center justify-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {scheduledRetentionLoading ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Clock className="w-3.5 h-3.5" />
              )}
              {t(
                'adminDeployment.lifecycle.runScheduledRetention',
                'Run Scheduled'
              )}
            </button>
          </div>
          {retentionPreview && (
            <p className="mb-3 text-xs text-text-secondary">
              {t(
                'adminDeployment.lifecycle.previewRetentionResult',
                'Preview: {{conversations}} conversations, {{artifacts}} document artifacts, {{skipped}} skipped classes.',
                {
                  conversations:
                    retentionPreview.counts?.stale_conversations ?? 0,
                  artifacts: retentionPreview.counts?.document_artifacts ?? 0,
                  skipped: retentionPreview.counts?.skipped_classes ?? 0,
                }
              )}
            </p>
          )}
          {scheduledRetentionResult && (
            <p className="mb-3 text-xs text-text-secondary">
              {t(
                'adminDeployment.lifecycle.scheduledRetentionResult',
                'Scheduled run: {{status}} with {{retries}} tombstone retries.',
                {
                  status: scheduledRetentionResult.status ?? 'unknown',
                  retries: scheduledRetentionResult.retry_results?.length ?? 0,
                }
              )}
            </p>
          )}
          {lifecycleStatus?.scheduled_retention &&
            (() => {
              const enabledClasses = Array.isArray(
                lifecycleStatus.scheduled_retention.enabled_classes
              )
                ? lifecycleStatus.scheduled_retention.enabled_classes
                : [];
              return (
                <p className="mb-3 text-xs text-text-secondary">
                  {t(
                    'adminDeployment.lifecycle.scheduledRetentionStatus',
                    'Scheduled classes: {{classes}}',
                    {
                      classes:
                        enabledClasses.length > 0
                          ? enabledClasses.join(', ')
                          : t('common.none', 'None'),
                    }
                  )}
                </p>
              );
            })()}
          {lifecycleStatus?.audit_coverage?.summary && (
            <p className="mb-3 text-xs text-text-secondary">
              {t(
                'adminDeployment.lifecycle.auditCoverageStatus',
                'Audit coverage: {{audited}} audited, {{exceptions}} exceptions, {{missing}} missing.',
                {
                  audited: lifecycleStatus.audit_coverage.summary.audited,
                  exceptions:
                    lifecycleStatus.audit_coverage.summary
                      .documented_exceptions,
                  missing: lifecycleStatus.audit_coverage.summary.missing,
                }
              )}
            </p>
          )}

          {Array.isArray(lifecycleStatus?.data_classes) &&
          lifecycleStatus.data_classes.length > 0 ? (
            <div className="grid gap-3 md:grid-cols-2">
              {lifecycleStatus.data_classes.map((dataClass) => (
                <div
                  key={dataClass.key}
                  className="bg-surface border border-border rounded-lg p-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h4 className="text-sm font-medium text-text">
                        {dataClass.label}
                      </h4>
                      <p className="text-xs text-text-muted mt-1">
                        {t(
                          'adminDeployment.lifecycle.owner',
                          'Owner: {{owner}}',
                          { owner: dataClass.owner }
                        )}
                      </p>
                    </div>
                    <span className="text-[10px] uppercase tracking-wide bg-surface-overlay text-text-secondary px-2 py-1 rounded">
                      {dataClass.storage_targets.join(', ')}
                    </span>
                  </div>
                  <div className="mt-3 grid gap-1 text-xs text-text-secondary">
                    <p>
                      {t(
                        'adminDeployment.lifecycle.deletion',
                        'Deletion: {{status}}',
                        {
                          status: formatLifecycleStatus(
                            dataClass.deletion.status
                          ),
                        }
                      )}
                    </p>
                    <p>
                      {t(
                        'adminDeployment.lifecycle.retention',
                        'Retention: {{status}}',
                        {
                          status: formatLifecycleStatus(
                            dataClass.retention.status
                          ),
                        }
                      )}
                    </p>
                    <p>
                      {t(
                        'adminDeployment.lifecycle.audit',
                        'Audit: {{status}}',
                        {
                          status: formatLifecycleStatus(dataClass.audit.status),
                        }
                      )}
                    </p>
                    {dataClass.confidentiality && (
                      <p>
                        {t(
                          'adminDeployment.lifecycle.confidentiality',
                          'Confidentiality: {{status}}',
                          {
                            status: formatLifecycleStatus(
                              dataClass.confidentiality.status
                            ),
                          }
                        )}
                      </p>
                    )}
                    {lifecycleStatus.deletion_tombstones?.by_class?.[
                      dataClass.key
                    ] && (
                      <div className="pt-2 mt-1 border-t border-border/60 grid gap-1">
                        <p>
                          {t(
                            'adminDeployment.lifecycle.incompleteTombstones',
                            'Incomplete tombstones: {{count}}',
                            {
                              count:
                                lifecycleStatus.deletion_tombstones.by_class[
                                  dataClass.key
                                ].incomplete,
                            }
                          )}
                        </p>
                        <p>
                          {t(
                            'adminDeployment.lifecycle.completedTombstones',
                            'Completed tombstones: {{count}}',
                            {
                              count:
                                lifecycleStatus.deletion_tombstones.by_class[
                                  dataClass.key
                                ].completed,
                            }
                          )}
                        </p>
                      </div>
                    )}
                  </div>
                  {dataClass.retention_policy && (
                    <div className="mt-3 border-t border-border/60 pt-3">
                      <div className="flex flex-wrap items-center gap-3">
                        <label className="inline-flex items-center gap-2 text-xs text-text-secondary">
                          <input
                            type="checkbox"
                            checked={
                              retentionPolicyDrafts[dataClass.key]?.enabled ??
                              dataClass.retention_policy.enabled
                            }
                            onChange={(event) =>
                              updateRetentionPolicyDraft(dataClass.key, {
                                enabled: event.target.checked,
                              })
                            }
                            className="h-4 w-4 rounded border-border text-accent focus:ring-accent"
                          />
                          {t(
                            'adminDeployment.lifecycle.retentionEnabled',
                            'Enabled'
                          )}
                        </label>
                        <label className="inline-flex items-center gap-2 text-xs text-text-secondary">
                          {t(
                            'adminDeployment.lifecycle.retentionWindow',
                            'Window'
                          )}
                          <input
                            type="number"
                            min={1}
                            value={
                              retentionPolicyDrafts[dataClass.key]
                                ?.retention_window_days ??
                              dataClass.retention_policy.retention_window_days
                            }
                            onChange={(event) =>
                              updateRetentionPolicyDraft(dataClass.key, {
                                retention_window_days: Number(
                                  event.target.value
                                ),
                              })
                            }
                            className="w-20 rounded-md border border-border bg-background px-2 py-1 text-xs text-text"
                          />
                          {t('adminDeployment.lifecycle.retentionDays', 'days')}
                        </label>
                        <label className="inline-flex items-center gap-2 text-xs text-text-secondary">
                          <input
                            type="checkbox"
                            checked={
                              retentionPolicyDrafts[dataClass.key]
                                ?.scheduled_enforcement_enabled ??
                              dataClass.retention_policy
                                .scheduled_enforcement_enabled
                            }
                            onChange={(event) =>
                              updateRetentionPolicyDraft(dataClass.key, {
                                scheduled_enforcement_enabled:
                                  event.target.checked,
                              })
                            }
                            className="h-4 w-4 rounded border-border text-accent focus:ring-accent"
                          />
                          {t(
                            'adminDeployment.lifecycle.scheduledEnforcement',
                            'Scheduled'
                          )}
                        </label>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleSaveRetentionPolicy(dataClass.key)}
                        disabled={savingRetentionPolicyKey === dataClass.key}
                        className="mt-3 inline-flex items-center justify-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {savingRetentionPolicyKey === dataClass.key ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <Save className="w-3.5 h-3.5" />
                        )}
                        {t(
                          'adminDeployment.lifecycle.saveRetentionPolicy',
                          'Save Policy'
                        )}
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-text-muted">
              {t(
                'adminDeployment.lifecycle.empty',
                'Lifecycle status is not available.'
              )}
            </p>
          )}

          {retentionPolicyError && (
            <p className="mt-3 text-xs text-danger">
              {t(
                'adminDeployment.lifecycle.retentionPolicySaveFailed',
                'Unable to save retention policy.'
              )}
            </p>
          )}
          {retentionPreviewError && (
            <p className="mt-3 text-xs text-danger">
              {t(
                'adminDeployment.lifecycle.retentionPreviewFailed',
                'Unable to preview retention.'
              )}
            </p>
          )}
          {scheduledRetentionError && (
            <p className="mt-3 text-xs text-danger">
              {t(
                'adminDeployment.lifecycle.scheduledRetentionFailed',
                'Unable to run scheduled retention.'
              )}
            </p>
          )}
          {artifactPostureError && (
            <p className="mt-3 text-xs text-danger">
              {t(
                'adminDeployment.lifecycle.artifactPostureFailed',
                'Unable to update artifact encryption posture.'
              )}
            </p>
          )}

          {((Array.isArray(lifecycleStatus?.unsupported_deployment_surfaces) &&
            lifecycleStatus.unsupported_deployment_surfaces.length > 0) ||
            (Array.isArray(
              lifecycleStatus?.unsupported_deployment_surface_categories
            ) &&
              lifecycleStatus.unsupported_deployment_surface_categories.length >
                0)) && (
            <div className="mt-5 border-t border-border pt-4">
              <div className="flex items-start justify-between gap-3 mb-3">
                <div>
                  <h4 className="text-sm font-medium text-text">
                    {t(
                      'adminDeployment.lifecycle.unsupportedSurfacesTitle',
                      'Unsupported Deployment Surfaces'
                    )}
                  </h4>
                  <p className="text-xs text-text-secondary mt-1">
                    {t(
                      'adminDeployment.lifecycle.unsupportedSurfacesDescription',
                      'These operational traces are disclosed here but remain outside product lifecycle control.'
                    )}
                  </p>
                </div>
              </div>
              {unsupportedSurfaceError && (
                <p className="text-xs text-danger mb-3">
                  {t(
                    'adminDeployment.lifecycle.unsupportedSurfaceAckFailed',
                    'Unable to update acknowledgement.'
                  )}
                </p>
              )}
              {Array.isArray(
                lifecycleStatus?.unsupported_deployment_surface_categories
              ) &&
              lifecycleStatus.unsupported_deployment_surface_categories.length >
                0 ? (
                <div className="grid gap-3 md:grid-cols-2">
                  {lifecycleStatus.unsupported_deployment_surface_categories.map(
                    (category) => (
                      <div
                        key={category.category}
                        className="bg-surface border border-border rounded-lg p-3"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <h5 className="text-sm font-medium text-text">
                              {category.label}
                            </h5>
                            <p className="text-xs text-text-secondary mt-1">
                              {category.guidance}
                            </p>
                            <p className="text-xs text-text-muted mt-1">
                              {category.surfaces
                                .map((surface) => surface.label)
                                .join(', ')}
                            </p>
                          </div>
                          <span className="shrink-0 text-[10px] uppercase tracking-wide bg-surface-overlay text-text-secondary px-2 py-1 rounded">
                            {formatLifecycleStatus(category.status)}
                          </span>
                        </div>
                        <button
                          type="button"
                          onClick={() =>
                            handleAcknowledgeUnsupportedSurfaceCategory(
                              category.category,
                              !category.acknowledged
                            )
                          }
                          disabled={
                            acknowledgingSurfaceCategory === category.category
                          }
                          aria-pressed={category.acknowledged}
                          className="mt-3 inline-flex items-center justify-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {acknowledgingSurfaceCategory ===
                          category.category ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : category.acknowledged ? (
                            <CheckCircle className="w-3.5 h-3.5" />
                          ) : (
                            <AlertCircle className="w-3.5 h-3.5" />
                          )}
                          {category.acknowledged
                            ? t(
                                'adminDeployment.lifecycle.acknowledgedSurface',
                                'Acknowledged'
                              )
                            : t(
                                'adminDeployment.lifecycle.acknowledgeSurface',
                                'Acknowledge'
                              )}
                        </button>
                      </div>
                    )
                  )}
                </div>
              ) : (
                <div className="grid gap-3 md:grid-cols-2">
                  {(lifecycleStatus.unsupported_deployment_surfaces ?? []).map(
                    (surface) => (
                      <div
                        key={surface.key}
                        className="bg-surface border border-border rounded-lg p-3"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <h5 className="text-sm font-medium text-text">
                              {surface.label}
                            </h5>
                            <p className="text-xs text-text-secondary mt-1">
                              {surface.summary}
                            </p>
                          </div>
                          <span className="shrink-0 text-[10px] uppercase tracking-wide bg-surface-overlay text-text-secondary px-2 py-1 rounded">
                            {formatLifecycleStatus(surface.status)}
                          </span>
                        </div>
                        <button
                          type="button"
                          onClick={() =>
                            handleAcknowledgeUnsupportedSurface(
                              surface.key,
                              !surface.acknowledged
                            )
                          }
                          disabled={acknowledgingSurfaceKey === surface.key}
                          aria-pressed={surface.acknowledged}
                          className="mt-3 inline-flex items-center justify-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {acknowledgingSurfaceKey === surface.key ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : surface.acknowledged ? (
                            <CheckCircle className="w-3.5 h-3.5" />
                          ) : (
                            <AlertCircle className="w-3.5 h-3.5" />
                          )}
                          {surface.acknowledged
                            ? t(
                                'adminDeployment.lifecycle.acknowledgedSurface',
                                'Acknowledged'
                              )
                            : t(
                                'adminDeployment.lifecycle.acknowledgeSurface',
                                'Acknowledge'
                              )}
                        </button>
                      </div>
                    )
                  )}
                </div>
              )}
            </div>
          )}
        </Card>

        <Card
          id="operational-readiness"
          role="group"
          aria-label={t(
            'adminDeployment.operationalReadiness.title',
            'Operational Readiness'
          )}
        >
          <h3 className="heading-sm mb-2 flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-text-muted" />
            {t(
              'adminDeployment.operationalReadiness.title',
              'Operational Readiness'
            )}
          </h3>
          <p className="text-sm text-text-secondary">
            {t(
              'adminDeployment.operationalReadiness.description',
              'Review backup, restore, monitoring, and recovery drills before normal operation.'
            )}
          </p>
        </Card>

        <Card
          role="group"
          aria-label={t(
            'adminDeployment.lifecycle.tombstonesTitle',
            'Deletion Tombstones'
          )}
        >
          <div className="flex items-start justify-between gap-3 mb-2">
            <h3 className="heading-sm flex items-center gap-2">
              <History className="w-4 h-4 text-text-muted" />
              {t(
                'adminDeployment.lifecycle.tombstonesTitle',
                'Deletion Tombstones'
              )}
            </h3>
            {tombstonesLoading && (
              <Loader2 className="w-4 h-4 animate-spin text-text-muted" />
            )}
          </div>
          <p className="text-sm text-text-secondary mb-4">
            {t(
              'adminDeployment.lifecycle.tombstonesDescription',
              'Retry incomplete lifecycle deletions without exposing deleted content.'
            )}
          </p>
          <div
            className="mb-4 inline-flex rounded-lg border border-border bg-surface p-1"
            role="group"
            aria-label={t(
              'adminDeployment.lifecycle.tombstoneStatusFilter',
              'Tombstone status filter'
            )}
          >
            {tombstoneStatusFilters.map((filter) => {
              const selected = tombstoneStatusFilter === filter.value;
              return (
                <button
                  key={filter.value}
                  type="button"
                  aria-label={filter.ariaLabel}
                  aria-pressed={selected}
                  onClick={() => setTombstoneStatusFilter(filter.value)}
                  className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                    selected
                      ? 'bg-surface-raised text-text shadow-sm'
                      : 'text-text-secondary hover:text-text hover:bg-surface-overlay'
                  }`}
                >
                  {filter.label}
                </button>
              );
            })}
          </div>
          {tombstoneRetryError && (
            <p className="text-xs text-danger mb-3">
              {t(
                'adminDeployment.lifecycle.tombstoneRetryFailed',
                'Retry failed.'
              )}
            </p>
          )}
          {tombstonesError && !tombstonesLoading ? (
            <p className="text-xs text-danger">
              {t(
                'adminDeployment.lifecycle.tombstonesFetchFailed',
                'Unable to load deletion tombstones.'
              )}
            </p>
          ) : tombstones.length > 0 ? (
            <div className="space-y-3">
              {tombstones.map((tombstone) => {
                const detail = tombstone.deletion?.results?.[0]?.detail;
                const canRetry =
                  tombstone.status !== 'completed' &&
                  tombstone.deletion?.retryable !== false;
                const updatedAt =
                  tombstone.updated_at ??
                  tombstone.last_retry_at ??
                  tombstone.created_at;
                return (
                  <div
                    key={tombstone.id}
                    className="bg-surface border border-border rounded-lg p-3"
                  >
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium text-text break-all">
                            {tombstone.lifecycle_data_class ===
                            'sage_user_runtime'
                              ? `${t('adminUserManager.columns.user')} ${tombstone.conversation_id.replace(/^user:/, '')}`
                              : tombstone.conversation_id}
                          </span>
                          <span className="text-[10px] uppercase tracking-wide bg-surface-overlay text-text-secondary px-2 py-1 rounded">
                            {formatLifecycleStatus(tombstone.status)}
                          </span>
                        </div>
                        <div className="mt-2 grid gap-1 text-xs text-text-secondary">
                          <p>
                            {t(
                              'adminDeployment.lifecycle.tombstoneClass',
                              'Class: {{className}}',
                              { className: tombstone.lifecycle_data_class }
                            )}
                          </p>
                          <p>
                            {t(
                              'adminDeployment.lifecycle.tombstoneSource',
                              'Source: {{source}}',
                              { source: tombstone.source }
                            )}
                          </p>
                          <p>
                            {t(
                              'adminDeployment.lifecycle.tombstoneRetries',
                              'Retries: {{count}}',
                              { count: tombstone.retry_count ?? 0 }
                            )}
                          </p>
                          {updatedAt && (
                            <p>
                              {t(
                                'adminDeployment.lifecycle.tombstoneUpdated',
                                'Updated: {{updated}}',
                                { updated: formatTimestamp(updatedAt) }
                              )}
                            </p>
                          )}
                          {detail && <p>{detail}</p>}
                        </div>
                      </div>
                      {canRetry && (
                        <button
                          type="button"
                          onClick={() => handleRetryTombstone(tombstone.id)}
                          disabled={retryingTombstoneId === tombstone.id}
                          aria-label={t(
                            'adminDeployment.lifecycle.retryTombstoneLabel',
                            'Retry deletion tombstone {{id}}',
                            { id: tombstone.id }
                          )}
                          className="inline-flex items-center justify-center gap-1.5 border border-border hover:border-accent/50 text-text rounded-lg px-3 py-2 text-xs font-medium transition-all hover:bg-surface disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {retryingTombstoneId === tombstone.id ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : (
                            <RotateCcw className="w-3.5 h-3.5" />
                          )}
                          {t('adminDeployment.lifecycle.retry', 'Retry')}
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-xs text-text-muted">
              {t(
                'adminDeployment.lifecycle.tombstonesEmpty',
                'No deletion tombstones are waiting for retry.'
              )}
            </p>
          )}
        </Card>

        {/* Actions Section */}
        <div className="flex gap-3">
          <button
            onClick={handleValidate}
            disabled={validationLoading}
            className="flex-1 flex items-center justify-center gap-2 border border-border hover:border-accent/50 text-text rounded-lg px-4 py-2.5 text-sm font-medium transition-all hover:bg-surface disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {validationLoading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <CheckCircle className="w-4 h-4" />
            )}
            {validationLoading
              ? t('adminDeployment.validating', 'Validating...')
              : t('adminDeployment.validate', 'Validate Config')}
          </button>
          <button
            onClick={handleExport}
            className="flex-1 flex items-center justify-center gap-2 border border-border hover:border-accent/50 text-text rounded-lg px-4 py-2.5 text-sm font-medium transition-all hover:bg-surface"
          >
            <Download className="w-4 h-4" />
            {t('adminDeployment.exportEnv', 'Export .env')}
          </button>
          <button
            onClick={handleExportSageRuntimeEnv}
            className="flex-1 flex items-center justify-center gap-2 border border-border hover:border-accent/50 text-text rounded-lg px-4 py-2.5 text-sm font-medium transition-all hover:bg-surface"
          >
            <Download className="w-4 h-4" />
            {t('adminDeployment.exportSageRuntimeEnv', 'Export Sage env')}
          </button>
          <button
            onClick={handleExportCoreBackendRuntimeEnv}
            className="flex-1 flex items-center justify-center gap-2 border border-border hover:border-accent/50 text-text rounded-lg px-4 py-2.5 text-sm font-medium transition-all hover:bg-surface"
          >
            <Download className="w-4 h-4" />
            {t(
              'adminDeployment.exportCoreBackendRuntimeEnv',
              'Export core-backend env'
            )}
          </button>
          <button
            onClick={() => setShowAuditLog(!showAuditLog)}
            aria-label={t('adminDeployment.auditLog', 'Recent Changes')}
            className="flex items-center justify-center gap-2 border border-border hover:border-accent/50 text-text rounded-lg px-4 py-2.5 text-sm font-medium transition-all hover:bg-surface"
          >
            <History className="w-4 h-4" />
          </button>
        </div>

        {/* Export Error */}
        {exportError && (
          <div className="bg-error/10 border border-error/20 rounded-xl p-4">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-4 h-4 text-error" />
              <p className="text-sm text-error">{exportError}</p>
            </div>
          </div>
        )}

        {/* Reveal Secret Error */}
        {revealError && (
          <div className="bg-error/10 border border-error/20 rounded-xl p-4">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-4 h-4 text-error" />
              <p className="text-sm text-error">{revealError}</p>
            </div>
          </div>
        )}

        {/* Validation Result */}
        {validationState && !validationDismissed && (
          <div
            className={`border rounded-xl p-4 ${
              validationIsStale
                ? 'bg-warning/5 border-warning/30'
                : validationState.result.valid
                  ? 'bg-success/5 border-success/30'
                  : 'bg-error/5 border-error/30'
            }`}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-start gap-2">
                {validationIsStale ? (
                  <AlertCircle className="w-5 h-5 text-warning" />
                ) : validationState.result.valid ? (
                  <CheckCircle className="w-5 h-5 text-success" />
                ) : (
                  <XCircle className="w-5 h-5 text-error" />
                )}
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span
                      className={`font-medium ${
                        validationIsStale
                          ? 'text-warning'
                          : validationState.result.valid
                            ? 'text-success'
                            : 'text-error'
                      }`}
                    >
                      {validationIsStale
                        ? t(
                            'adminDeployment.validationStaleTitle',
                            'Validation Out of Date'
                          )
                        : validationState.result.valid
                          ? t(
                              'adminDeployment.configValid',
                              'Configuration Valid'
                            )
                          : t(
                              'adminDeployment.configInvalid',
                              'Configuration Invalid'
                            )}
                    </span>
                    {validationIsStale && (
                      <span className="text-[10px] bg-warning/20 text-warning px-2 py-0.5 rounded-full">
                        {t(
                          'adminDeployment.validationStalePill',
                          'Out-of-date'
                        )}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-text-secondary mt-1">
                    {t(
                      'adminDeployment.validationTimestamp',
                      'Validated {{time}}',
                      { time: formatTimestamp(validationState.validatedAt) }
                    )}
                    {configSignature.lastUpdatedAt && (
                      <>
                        {' '}
                        ·{' '}
                        {t(
                          'adminDeployment.validationConfigUpdated',
                          'Config updated {{time}}',
                          {
                            time: formatTimestamp(
                              configSignature.lastUpdatedAt
                            ),
                          }
                        )}
                      </>
                    )}
                  </p>
                  {validationIsStale && (
                    <p className="text-xs text-warning mt-2">
                      {t(
                        'adminDeployment.validationStaleDesc',
                        'Configuration changed since last validation. Revalidate to confirm.'
                      )}
                    </p>
                  )}
                </div>
              </div>
              <button
                onClick={() => setValidationDismissed(true)}
                className="text-text-secondary hover:text-text transition-colors"
                aria-label={t('common.close', 'Close')}
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
              <span className="text-text-secondary">
                {validationIsStale
                  ? t(
                      'adminDeployment.validationSummaryStale',
                      'Last result: {{errors}} errors, {{warnings}} warnings',
                      {
                        errors: validationState.result.errors.length,
                        warnings: validationState.result.warnings.length,
                      }
                    )
                  : t(
                      'adminDeployment.validationSummary',
                      '{{errors}} errors, {{warnings}} warnings',
                      {
                        errors: validationState.result.errors.length,
                        warnings: validationState.result.warnings.length,
                      }
                    )}
              </span>
              {validationIsStale && (
                <button
                  onClick={handleValidate}
                  disabled={validationLoading}
                  className="inline-flex items-center gap-1.5 text-xs text-accent hover:text-accent-hover transition-colors disabled:opacity-50"
                >
                  {validationLoading ? (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  ) : (
                    <RefreshCw className="w-3 h-3" />
                  )}
                  {t('adminDeployment.revalidate', 'Revalidate')}
                </button>
              )}
            </div>

            {validationState.result.errors.length === 0 &&
              validationState.result.warnings.length === 0 && (
                <p className="text-xs text-text-secondary mt-3">
                  {t('adminDeployment.validationNoIssues', 'No issues found')}
                </p>
              )}

            {validationState.result.errors.length > 0 && (
              <>
                <p className="text-xs font-medium text-error mt-3">
                  {t('adminDeployment.validationErrors', 'Errors')}
                </p>
                <ul className="text-sm text-error list-disc list-inside mt-1">
                  {validationState.result.errors.map((err, i) => (
                    <li key={i}>{err}</li>
                  ))}
                </ul>
              </>
            )}

            {validationState.result.warnings.length > 0 && (
              <>
                <p className="text-xs font-medium text-warning mt-3">
                  {t('adminDeployment.validationWarnings', 'Warnings')}
                </p>
                <ul className="text-sm text-warning list-disc list-inside mt-1">
                  {validationState.result.warnings.map((warn, i) => (
                    <li key={i}>{warn}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}

        {/* Audit Log */}
        {showAuditLog && (
          <Card>
            <div className="flex items-center justify-between mb-4">
              <h3 className="heading-sm flex items-center gap-2">
                <History className="w-4 h-4 text-text-muted" />
                {t('adminDeployment.auditLog', 'Recent Changes')}
              </h3>
              <button
                onClick={refreshAudit}
                disabled={auditLoading}
                className="text-xs text-accent hover:text-accent-hover transition-colors"
              >
                {t('adminDeployment.refresh', 'Refresh')}
              </button>
            </div>

            {auditLog?.entries.length ? (
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {auditLog.entries.map((entry) => {
                  // Mask secret values in audit log display
                  const secret = isSecretKey(entry.config_key);
                  const emptyValue = `(${t('common.none', 'None')})`;
                  const displayOld = secret
                    ? '********'
                    : entry.old_value
                      ? `"${entry.old_value}"`
                      : emptyValue;
                  const displayNew = secret
                    ? '********'
                    : entry.new_value
                      ? `"${entry.new_value}"`
                      : emptyValue;

                  return (
                    <div
                      key={entry.id}
                      className="bg-surface border border-border rounded-lg p-2 text-xs"
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-text">
                          {entry.config_key}
                        </span>
                        <span className="text-text-muted">
                          {(() => {
                            const date = new Date(entry.changed_at);
                            return isNaN(date.getTime())
                              ? entry.changed_at
                              : date.toLocaleString();
                          })()}
                        </span>
                      </div>
                      <p className="text-text-muted mt-1">
                        {displayOld} → {displayNew}
                      </p>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-text-muted text-center py-4">
                {t('adminDeployment.noRecentChanges', 'No recent changes')}
              </p>
            )}
          </Card>
        )}

        {/* Configuration Categories */}
        {deploymentConfig && (
          <>
            {renderCategory('llm', deploymentConfig.llm)}
            {renderCategory('embedding', deploymentConfig.embedding)}
            {renderCategory('email', deploymentConfig.email)}
            {renderCategory('storage', deploymentConfig.storage)}
            {renderCategory('search', deploymentConfig.search)}
            {renderCategory('security', deploymentConfig.security)}
            {renderCategory('domains', deploymentConfig.domains)}
            {renderCategory('ssl', deploymentConfig.ssl)}
            {renderCategory('general', deploymentConfig.general)}
          </>
        )}

        {/* Admin Key Migration Section */}
        <Card>
          <h3 className="heading-sm mb-2 flex items-center gap-2">
            <Key className="w-4 h-4 text-text-muted" />
            {t('adminDeployment.keyMigration.title', 'Admin Key Migration')}
          </h3>
          <p className="text-sm text-text-secondary mb-1">
            {t(
              'adminDeployment.keyMigration.description',
              'Migrate to a new Nostr private key'
            )}
          </p>
          <p className="text-xs text-text-muted mb-4">
            {t(
              'adminDeployment.keyMigration.hint',
              'Re-encrypts all user PII to a new admin pubkey. Use this if you need to change your admin key.'
            )}
          </p>

          <div className="bg-surface border border-border rounded-lg p-3">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs text-text-muted mb-1">
                  {t(
                    'adminDeployment.keyMigration.currentAdmin',
                    'Current Admin'
                  )}
                </p>
                <p className="text-sm font-mono text-text">
                  {localStorage.getItem(STORAGE_KEYS.ADMIN_PUBKEY)
                    ? `${localStorage.getItem(STORAGE_KEYS.ADMIN_PUBKEY)?.slice(0, 8)}...${localStorage.getItem(STORAGE_KEYS.ADMIN_PUBKEY)?.slice(-8)}`
                    : t('adminDeployment.keyMigration.unknown', 'Unknown')}
                </p>
              </div>
              <Button
                onClick={handleOpenMigrationModal}
                variant="secondary"
                size="sm"
                leadingIcon={<Key className="w-4 h-4" />}
              >
                {t(
                  'adminDeployment.keyMigration.migrateButton',
                  'Migrate to New Key'
                )}
              </Button>
            </div>
          </div>
        </Card>

        {/* Navigation */}
        <div className="flex gap-3">
          <Link
            to="/admin/setup"
            className="flex-1 flex items-center justify-center gap-2 border border-border hover:border-accent/50 text-text rounded-xl px-4 py-3 text-sm font-medium transition-all hover:bg-surface"
          >
            <ArrowLeft className="w-4 h-4" />
            {t('common.backToAdminDashboard', 'Back to Admin Dashboard')}
          </Link>
        </div>

        {/* Test Email Modal */}
        {showTestEmailModal && (
          <div
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="test-email-modal-title"
            onKeyDown={(e) => e.key === 'Escape' && handleCloseTestEmailModal()}
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-md mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="test-email-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <Mail className="w-5 h-5" />
                  {t('adminDeployment.testEmailTitle', 'Send Test Email')}
                </h3>
                <button
                  onClick={handleCloseTestEmailModal}
                  className="text-text-muted hover:text-text transition-colors"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <p className="text-sm text-text-muted mb-4">
                {t(
                  'adminDeployment.testEmailDesc',
                  'Send a test email to verify your SMTP configuration is working correctly.'
                )}
              </p>

              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-text mb-1">
                    {t('adminDeployment.emailAddress', 'Email Address')}
                  </label>
                  <input
                    type="email"
                    value={testEmailAddress}
                    onChange={(e) => setTestEmailAddress(e.target.value)}
                    placeholder={t(
                      'adminDeployment.extracted.test_example_com_567159',
                      'test@example.com'
                    )}
                    className="w-full border border-border rounded-lg px-3 py-2 bg-surface text-text placeholder:text-text-muted text-sm focus:outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
                    disabled={testEmailSending}
                  />
                </div>

                {/* Result display */}
                {testEmailResult && (
                  <div
                    className={`rounded-lg p-3 ${testEmailResult.success ? 'bg-success/10 border border-success/20' : 'bg-error/10 border border-error/20'}`}
                  >
                    <div className="flex items-center gap-2">
                      {testEmailResult.success ? (
                        <CheckCircle className="w-4 h-4 text-success" />
                      ) : (
                        <XCircle className="w-4 h-4 text-error" />
                      )}
                      <span
                        className={`text-sm font-medium ${testEmailResult.success ? 'text-success' : 'text-error'}`}
                      >
                        {testEmailResult.message}
                      </span>
                    </div>
                    {testEmailResult.error && (
                      <p className="text-xs text-error mt-1 pl-6">
                        {testEmailResult.error}
                      </p>
                    )}
                  </div>
                )}

                <div className="flex gap-3">
                  <button
                    onClick={handleCloseTestEmailModal}
                    className="flex-1 bg-surface-overlay border border-border text-text rounded-lg px-4 py-2.5 text-sm font-medium hover:bg-surface transition-all"
                  >
                    {t('common.cancel', 'Cancel')}
                  </button>
                  <button
                    onClick={handleSendTestEmail}
                    disabled={testEmailSending || !testEmailAddress.trim()}
                    className="flex-1 bg-accent text-accent-text rounded-lg px-4 py-2.5 text-sm font-medium hover:bg-accent-hover transition-all disabled:opacity-50 flex items-center justify-center gap-2"
                  >
                    {testEmailSending ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Send className="w-4 h-4" />
                    )}
                    {t('adminDeployment.send', 'Send')}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Email Help Modal */}
        {showEmailHelpModal && (
          <div
            ref={emailHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="email-help-modal-title"
            onKeyDown={(e) => e.key === 'Escape' && handleCloseEmailHelpModal()}
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="email-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {EMAIL_HELP_PAGES[emailHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseEmailHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              {/* Content */}
              <div className="min-h-[280px]">
                {EMAIL_HELP_PAGES[emailHelpPage].content === 'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.emailHelp.overviewDesc',
                        "Here's what each SMTP field does:"
                      )}
                    </p>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-mono text-text">
                          MOCK_EMAIL
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.emailHelp.mockEmail',
                            'Enable for development. Emails are logged to console instead of being sent.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-mono text-text">SMTP_FROM</p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.emailHelp.smtpFrom',
                            'The "from" address recipients will see. Format: Name <email@domain.com>'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-mono text-text">SMTP_HOST</p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.emailHelp.smtpHost',
                            "Your email provider's SMTP server address (e.g., smtp.gmail.com)"
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-mono text-text">SMTP_PORT</p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.emailHelp.smtpPort',
                            'Usually 587 (TLS/STARTTLS) or 465 (SSL). Most providers use 587.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-mono text-text">SMTP_USER</p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.emailHelp.smtpUser',
                            'Login username. Varies by provider (email address, API key name, or token).'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-mono text-text">SMTP_PASS</p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.emailHelp.smtpPass',
                            'Password, app password, or API key depending on your provider.'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {EMAIL_HELP_PAGES[emailHelpPage].hint && (
                      <div className="bg-accent/10 border border-accent/20 rounded-lg p-3">
                        <p className="text-sm text-accent">
                          {EMAIL_HELP_PAGES[emailHelpPage].hint}
                        </p>
                      </div>
                    )}
                    <p className="text-sm text-text-muted">
                      {t(
                        'adminDeployment.emailHelp.exampleConfig',
                        'Example configuration:'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3 font-mono text-xs space-y-1">
                      {EMAIL_HELP_PAGES[emailHelpPage].config &&
                        Object.entries(
                          EMAIL_HELP_PAGES[emailHelpPage].config!
                        ).map(([key, value]) => (
                          <div key={key} className="flex">
                            <span className="text-accent">{key}</span>
                            <span className="text-text-muted">=</span>
                            <span className="text-text">{value}</span>
                          </div>
                        ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Pagination */}
              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setEmailHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={emailHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                {/* Page indicators */}
                <div className="flex items-center gap-1.5">
                  {EMAIL_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setEmailHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === emailHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.emailHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setEmailHelpPage((prev) =>
                      Math.min(EMAIL_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={emailHelpPage === EMAIL_HELP_PAGES.length - 1}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* LLM Help Modal */}
        {showLlmHelpModal && (
          <div
            ref={llmHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="llm-help-modal-title"
            onKeyDown={(e) => e.key === 'Escape' && handleCloseLlmHelpModal()}
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="llm-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {LLM_HELP_PAGES[llmHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseLlmHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              {/* Content */}
              <div className="min-h-[280px]">
                {LLM_HELP_PAGES[llmHelpPage].content === 'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.llmHelp.overviewDesc',
                        "This prototype uses Sage as the public Agent Runtime and Tinfoil as the Model Provider transport. Configure the Model Provider through LLM_* Deployment Settings; the stack maps them to Sage's Tinfoil runtime. The web app keeps the same user-facing API surface while the Gateway routes AI requests to Sage."
                      )}
                    </p>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          LLM_PROVIDER
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.llmHelp.providerField',
                            'Set this to "sage".'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          LLM_MODEL
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.llmHelp.modelField',
                            `Tinfoil model identifier Sage should use (for example: ${DEFAULT_TINFOIL_MODEL}).`
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          {t(
                            'adminDeployment.llmHelp.apiKeyLabel',
                            'Tinfoil API Key'
                          )}
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.llmHelp.apiKeyField',
                            'Canonical Tinfoil credential for verification, diagnostics, Sage, and the local proxy.'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {LLM_HELP_PAGES[llmHelpPage].hint && (
                      <div className="bg-accent/10 border border-accent/20 rounded-lg p-3">
                        <p className="text-sm text-accent">
                          {LLM_HELP_PAGES[llmHelpPage].hint}
                        </p>
                      </div>
                    )}
                    <p className="text-sm text-text-muted">
                      {t(
                        'adminDeployment.llmHelp.exampleConfig',
                        'Example configuration:'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3 font-mono text-xs space-y-1">
                      {LLM_HELP_PAGES[llmHelpPage].config &&
                        Object.entries(LLM_HELP_PAGES[llmHelpPage].config!).map(
                          ([key, value]) => (
                            <div key={key} className="flex">
                              <span className="text-accent">{key}</span>
                              <span className="text-text-muted">=</span>
                              <span className="text-text">{value}</span>
                            </div>
                          )
                        )}
                    </div>
                    {LLM_HELP_PAGES[llmHelpPage].extra && (
                      <p className="text-xs text-text-muted mt-3">
                        {LLM_HELP_PAGES[llmHelpPage].extra}
                      </p>
                    )}
                  </div>
                )}
              </div>

              {/* Pagination */}
              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setLlmHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={llmHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                {/* Page indicators */}
                <div className="flex items-center gap-1.5">
                  {LLM_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setLlmHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === llmHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.llmHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setLlmHelpPage((prev) =>
                      Math.min(LLM_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={llmHelpPage === LLM_HELP_PAGES.length - 1}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Embedding Help Modal */}
        {showEmbeddingHelpModal && (
          <div
            ref={embeddingHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="embedding-help-modal-title"
            onKeyDown={(e) =>
              e.key === 'Escape' && handleCloseEmbeddingHelpModal()
            }
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="embedding-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {EMBEDDING_HELP_PAGES[embeddingHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseEmbeddingHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              {/* Content */}
              <div className="min-h-[280px]">
                {EMBEDDING_HELP_PAGES[embeddingHelpPage].content ===
                'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.embeddingHelp.overviewDesc',
                        'Embeddings convert your documents into numbers that computers can compare. This is what makes searching your knowledge base possible.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-4">
                      <p className="text-sm font-medium text-text mb-2">
                        {t(
                          'adminDeployment.embeddingHelp.howItWorks',
                          'How it works:'
                        )}
                      </p>
                      <ol className="text-xs text-text-muted space-y-2 list-decimal list-inside">
                        <li>
                          {t(
                            'adminDeployment.embeddingHelp.step1',
                            'Your documents are split into chunks'
                          )}
                        </li>
                        <li>
                          {t(
                            'adminDeployment.embeddingHelp.step2',
                            'Each chunk is converted to a vector (list of numbers)'
                          )}
                        </li>
                        <li>
                          {t(
                            'adminDeployment.embeddingHelp.step3',
                            'When users ask questions, their query is also converted'
                          )}
                        </li>
                        <li>
                          {t(
                            'adminDeployment.embeddingHelp.step4',
                            'The system finds chunks with similar vectors'
                          )}
                        </li>
                      </ol>
                    </div>
                    <p className="text-xs text-text-muted">
                      {t(
                        'adminDeployment.embeddingHelp.overviewNote',
                        'Think of it like creating a fingerprint for each piece of text that captures its meaning.'
                      )}
                    </p>
                  </div>
                ) : EMBEDDING_HELP_PAGES[embeddingHelpPage].content ===
                  'models' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.embeddingHelp.modelsDesc',
                        'Different embedding models have different strengths:'
                      )}
                    </p>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          multilingual-e5-base
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.embeddingHelp.e5Desc',
                            'Default choice. Good balance of speed and quality. Supports 100+ languages. Runs locally.'
                          )}
                        </p>
                        <p className="text-xs text-success mt-1">
                          {t(
                            'adminDeployment.embeddingHelp.recommended',
                            'Recommended'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          intfloat/multilingual-e5-small
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.embeddingHelp.e5SmallDesc',
                            'Faster and lighter than the base model, with lower memory usage.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          intfloat/multilingual-e5-large
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.embeddingHelp.e5LargeDesc',
                            'Higher quality retrieval than base/small, but with slower indexing and more RAM use.'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.embeddingHelp.performanceDesc',
                        'These settings affect processing speed and resource usage:'
                      )}
                    </p>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          EMBEDDING_MODEL
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.embeddingHelp.modelPerfDesc',
                            'Choose smaller models for faster indexing, or larger models for better recall at higher CPU/RAM cost.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          {t(
                            'adminDeployment.extracted.hf_hub_offline_transformers_offline_65a6e3',
                            'HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE'
                          )}
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.embeddingHelp.cacheDesc',
                            'Set to 1 in air-gapped environments to force cached model assets and avoid network fetches.'
                          )}
                        </p>
                      </div>
                    </div>
                    <div className="bg-warning/10 border border-warning/20 rounded-lg p-3 mt-4">
                      <p className="text-xs text-warning">
                        {t(
                          'adminDeployment.embeddingHelp.warning',
                          'Changing the embedding model after uploading documents requires re-processing all documents.'
                        )}
                      </p>
                    </div>
                  </div>
                )}
              </div>

              {/* Pagination */}
              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setEmbeddingHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={embeddingHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                {/* Page indicators */}
                <div className="flex items-center gap-1.5">
                  {EMBEDDING_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setEmbeddingHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === embeddingHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.embeddingHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setEmbeddingHelpPage((prev) =>
                      Math.min(EMBEDDING_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={
                    embeddingHelpPage === EMBEDDING_HELP_PAGES.length - 1
                  }
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Domains Help Modal */}
        {showDomainsHelpModal && (
          <div
            ref={domainsHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="domains-help-modal-title"
            onKeyDown={(e) =>
              e.key === 'Escape' && handleCloseDomainsHelpModal()
            }
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="domains-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {DOMAINS_HELP_PAGES[domainsHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseDomainsHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="min-h-[280px]">
                {DOMAINS_HELP_PAGES[domainsHelpPage].content === 'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.domainsHelp.overviewDesc',
                        'These settings control where your app lives on the internet and how services find each other. Defaults are set for local development.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-4">
                      <p className="text-sm font-medium text-text mb-2">
                        {t(
                          'adminDeployment.domainsHelp.whatToSet',
                          'Set these when you go live:'
                        )}
                      </p>
                      <ul className="text-xs text-text-muted space-y-2 list-disc list-inside">
                        <li>
                          {t(
                            'adminDeployment.domainsHelp.overviewUrl',
                            'INSTANCE_URL / API_BASE_URL / ADMIN_BASE_URL for public entry points'
                          )}
                        </li>
                        <li>
                          {t(
                            'adminDeployment.domainsHelp.overviewCORS',
                            'CORS_ORIGINS to allow your frontend domain'
                          )}
                        </li>
                        <li>
                          {t(
                            'adminDeployment.domainsHelp.overviewDns',
                            'Email DNS (DKIM/SPF/DMARC) for deliverability'
                          )}
                        </li>
                      </ul>
                    </div>
                    <p className="text-xs text-text-muted">
                      {t(
                        'adminDeployment.domainsHelp.overviewNote',
                        'If you are staying on localhost, you can keep the defaults.'
                      )}
                    </p>
                  </div>
                ) : DOMAINS_HELP_PAGES[domainsHelpPage].content === 'urls' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.domainsHelp.urlsDesc',
                        'Public URLs and CORS origins must match exactly (scheme + domain + port).'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3 font-mono text-xs space-y-1">
                      <div>
                        {t(
                          'adminDeployment.extracted.instance_url_https_app_example_com_0700b1',
                          'INSTANCE_URL=https://app.example.com'
                        )}
                      </div>
                      <div>
                        {t(
                          'adminDeployment.extracted.api_base_url_https_api_example_com_5249b4',
                          'API_BASE_URL=https://api.example.com'
                        )}
                      </div>
                      <div>
                        {t(
                          'adminDeployment.extracted.admin_base_url_https_admin_example_com_f9b733',
                          'ADMIN_BASE_URL=https://admin.example.com'
                        )}
                      </div>
                      <div>
                        {t(
                          'adminDeployment.extracted.cors_origins_https_app_example_com_https_admin_664e5d',
                          'CORS_ORIGINS=https://app.example.com,https://admin.example.com'
                        )}
                      </div>
                    </div>
                    <p className="text-xs text-text-muted">
                      {t(
                        'adminDeployment.domainsHelp.urlsNote',
                        'If your API is served from the same domain as the app, you can leave API_BASE_URL empty.'
                      )}
                    </p>
                  </div>
                ) : DOMAINS_HELP_PAGES[domainsHelpPage].content === 'dns' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.domainsHelp.dnsDesc',
                        'These values help you create DNS records for email deliverability.'
                      )}
                    </p>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs text-text-muted mb-1">SPF</p>
                        <p className="text-xs text-text">
                          {t(
                            'adminDeployment.domainsHelp.spfExample',
                            'Example TXT record: v=spf1 include:sendgrid.net ~all'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs text-text-muted mb-1">DKIM</p>
                        <p className="text-xs text-text">
                          {t(
                            'adminDeployment.domainsHelp.dkimExample',
                            'Use the selector from DKIM_SELECTOR and the public key from your provider.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs text-text-muted mb-1">DMARC</p>
                        <p className="text-xs text-text">
                          {t(
                            'adminDeployment.domainsHelp.dmarcExample',
                            'Example TXT record: v=DMARC1; p=none; rua=mailto:dmarc@example.com'
                          )}
                        </p>
                      </div>
                    </div>
                    <p className="text-xs text-text-muted">
                      {t(
                        'adminDeployment.domainsHelp.dnsNote',
                        "Use your email provider's recommended records for best deliverability."
                      )}
                    </p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.domainsHelp.edgeDesc',
                        'Optional settings for advanced setups.'
                      )}
                    </p>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs font-medium text-text">
                          CDN_DOMAINS
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.domainsHelp.cdnDesc',
                            'Comma-separated CDN hostnames for static assets. Leave blank if not using a CDN.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs font-medium text-text">
                          WEBHOOK_BASE_URL
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.domainsHelp.webhookDesc',
                            'Base URL used to construct webhook callbacks. Use your public API domain.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs font-medium text-text">
                          CUSTOM_SEARXNG_URL
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.domainsHelp.searxDesc',
                            'Only needed if your SearXNG instance lives on a different host.'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setDomainsHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={domainsHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                <div className="flex items-center gap-1.5">
                  {DOMAINS_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setDomainsHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === domainsHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.domainsHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setDomainsHelpPage((prev) =>
                      Math.min(DOMAINS_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={domainsHelpPage === DOMAINS_HELP_PAGES.length - 1}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Storage Help Modal */}
        {showStorageHelpModal && (
          <div
            ref={storageHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="storage-help-modal-title"
            onKeyDown={(e) =>
              e.key === 'Escape' && handleCloseStorageHelpModal()
            }
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="storage-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {STORAGE_HELP_PAGES[storageHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseStorageHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="min-h-[280px]">
                {STORAGE_HELP_PAGES[storageHelpPage].content === 'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.storageHelp.overviewDesc',
                        'This section controls where Enclave keeps its data. Most admins can leave the defaults. You only need to change this if you’re moving files to a new disk, using external storage, or running on custom infrastructure.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.storageHelp.overviewWhen',
                          'When you’d change this: migrating servers, using a managed database, or adjusting volume mounts.'
                        )}
                      </p>
                    </div>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          {t(
                            'adminDeployment.extracted.sqlite_database_d9c1be',
                            'SQLite Database'
                          )}
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.storageHelp.sqliteDesc',
                            'Stores users, settings, and job status. Controlled by SQLITE_PATH.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          {t(
                            'adminDeployment.extracted.uploads_folder_5402a4',
                            'Uploads Folder'
                          )}
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.storageHelp.uploadsDesc',
                            'Raw documents are saved here (UPLOADS_DIR). Mounted to the backend container.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          {t(
                            'adminDeployment.extracted.vector_database_eb5526',
                            'Vector Database'
                          )}
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.storageHelp.qdrantDesc',
                            'Embeddings are stored in Qdrant at QDRANT_HOST:QDRANT_PORT.'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                ) : STORAGE_HELP_PAGES[storageHelpPage].content === 'paths' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.storageHelp.pathsDesc',
                        'These are internal container paths. If you change them, you must also update your Docker volume mounts to match.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.storageHelp.pathsWhen',
                          'Use this if you need to store data on a different disk or a mounted network drive.'
                        )}
                      </p>
                    </div>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          SQLITE_PATH
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          /data/enclave.db
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          UPLOADS_DIR
                        </p>
                        <p className="text-xs text-text-muted mt-1">/uploads</p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          {t(
                            'adminDeployment.extracted.qdrant_host_qdrant_port_d0cf55',
                            'QDRANT_HOST / QDRANT_PORT'
                          )}
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.extracted.qdrant_6333_289ff3',
                            'qdrant / 6333'
                          )}
                        </p>
                      </div>
                    </div>
                    <div className="bg-warning/10 border border-warning/20 rounded-lg p-3 mt-4">
                      <p className="text-xs text-warning">
                        {t(
                          'adminDeployment.storageHelp.pathsWarning',
                          'Changing these requires a service restart and matching volume mounts.'
                        )}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.storageHelp.backupsDesc',
                        'For backups or migrations, you’ll want copies of the SQLite database and uploads folder. If you use Qdrant in production, snapshot it too.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.storageHelp.backupsWhen',
                          'Use this before upgrading servers or switching hosting providers.'
                        )}
                      </p>
                    </div>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs text-text-muted mb-1">
                          {t(
                            'adminDeployment.storageHelp.backupStep1',
                            '1) Stop services'
                          )}
                        </p>
                        <p className="text-xs text-text">
                          {t(
                            'adminDeployment.storageHelp.backupStep1Desc',
                            'docker compose down'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs text-text-muted mb-1">
                          {t(
                            'adminDeployment.storageHelp.backupStep2',
                            '2) Copy data'
                          )}
                        </p>
                        <p className="text-xs text-text">
                          {t(
                            'adminDeployment.storageHelp.backupStep2Desc',
                            'Copy /data/enclave.db and /uploads, plus Qdrant snapshots if used.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-xs text-text-muted mb-1">
                          {t(
                            'adminDeployment.storageHelp.backupStep3',
                            '3) Restore'
                          )}
                        </p>
                        <p className="text-xs text-text">
                          {t(
                            'adminDeployment.storageHelp.backupStep3Desc',
                            'Mount the same paths and restart services.'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setStorageHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={storageHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                <div className="flex items-center gap-1.5">
                  {STORAGE_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setStorageHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === storageHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.storageHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setStorageHelpPage((prev) =>
                      Math.min(STORAGE_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={storageHelpPage === STORAGE_HELP_PAGES.length - 1}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Search Help Modal */}
        {showSearchHelpModal && (
          <div
            ref={searchHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="search-help-modal-title"
            onKeyDown={(e) =>
              e.key === 'Escape' && handleCloseSearchHelpModal()
            }
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="search-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {SEARCH_HELP_PAGES[searchHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseSearchHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="min-h-[280px]">
                {SEARCH_HELP_PAGES[searchHelpPage].content === 'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.searchHelp.overviewDesc',
                        'Web search lets the assistant fetch current information. It relies on SearXNG, an open-source search proxy you control.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.searchHelp.overviewWhen',
                          'Change this when you want the assistant to search the web or if your SearXNG instance moves.'
                        )}
                      </p>
                    </div>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted mb-1">
                        {t(
                          'adminDeployment.searchHelp.overviewNote',
                          'If you disable SearXNG, the AI will only use your uploaded documents.'
                        )}
                      </p>
                    </div>
                  </div>
                ) : SEARCH_HELP_PAGES[searchHelpPage].content === 'config' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.searchHelp.configDesc',
                        'Point SEARXNG_URL to your SearXNG instance. In Docker Compose, the default service name works.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3 font-mono text-xs">
                      {t(
                        'adminDeployment.extracted.searxng_url_http_searxng_8080_ede53c',
                        'SEARXNG_URL=http://searxng:8080'
                      )}
                    </div>
                    <p className="text-xs text-text-muted">
                      {t(
                        'adminDeployment.searchHelp.configNote',
                        'If SearXNG is hosted externally, use its public URL instead.'
                      )}
                    </p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.searchHelp.privacyDesc',
                        'Search queries are sent to your SearXNG instance. Configure logging and rate limits there based on your privacy requirements.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.searchHelp.privacyWhen',
                          'Use this section when your org has specific privacy or compliance requirements.'
                        )}
                      </p>
                    </div>
                    <div className="bg-warning/10 border border-warning/20 rounded-lg p-3">
                      <p className="text-xs text-warning">
                        {t(
                          'adminDeployment.searchHelp.privacyWarning',
                          'If you do not want external requests, disable web search by clearing SEARXNG_URL.'
                        )}
                      </p>
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setSearchHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={searchHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                <div className="flex items-center gap-1.5">
                  {SEARCH_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setSearchHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === searchHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.searchHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setSearchHelpPage((prev) =>
                      Math.min(SEARCH_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={searchHelpPage === SEARCH_HELP_PAGES.length - 1}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Security Help Modal */}
        {showSecurityHelpModal && (
          <div
            ref={securityHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="security-help-modal-title"
            onKeyDown={(e) =>
              e.key === 'Escape' && handleCloseSecurityHelpModal()
            }
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="security-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {SECURITY_HELP_PAGES[securityHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseSecurityHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="min-h-[280px]">
                {SECURITY_HELP_PAGES[securityHelpPage].content ===
                'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.securityHelp.overviewDesc',
                        'These settings affect authentication behavior and the URLs used in magic-link emails. Defaults are safe for local development.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted mb-1">
                        {t(
                          'adminDeployment.securityHelp.overviewNote',
                          'For production, set a correct FRONTEND_URL and keep auth flows tied to real email and Nostr verification.'
                        )}
                      </p>
                      <p className="text-xs text-text-muted mt-2">
                        {t(
                          'adminDeployment.securityHelp.overviewWhen',
                          'Change these when moving from local testing to a public deployment.'
                        )}
                      </p>
                    </div>
                  </div>
                ) : SECURITY_HELP_PAGES[securityHelpPage].content === 'dev' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.securityHelp.devDesc',
                        'Prototype simulated auth flags have been removed from the supported deployment surface.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.securityHelp.devNote',
                          'Use MOCK_EMAIL for local email testing; user and admin sessions still require the normal verification flow.'
                        )}
                      </p>
                    </div>
                    <div className="bg-warning/10 border border-warning/20 rounded-lg p-3 mt-4">
                      <p className="text-xs text-warning">
                        {t(
                          'adminDeployment.securityHelp.devWarning',
                          'Do not reintroduce auth bypass flags in production or local deployment settings.'
                        )}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.securityHelp.frontendDesc',
                        'FRONTEND_URL is used for magic-link emails and should match your public UI domain.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3 font-mono text-xs">
                      {t(
                        'adminDeployment.extracted.frontend_url_https_app_example_com_05514a',
                        'FRONTEND_URL=https://app.example.com'
                      )}
                    </div>
                    <p className="text-xs text-text-muted">
                      {t(
                        'adminDeployment.securityHelp.frontendNote',
                        'If this is wrong, login links may send users to the wrong place.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.securityHelp.frontendWhen',
                          'Update this whenever your public domain changes.'
                        )}
                      </p>
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setSecurityHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={securityHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                <div className="flex items-center gap-1.5">
                  {SECURITY_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setSecurityHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === securityHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.securityHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setSecurityHelpPage((prev) =>
                      Math.min(SECURITY_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={securityHelpPage === SECURITY_HELP_PAGES.length - 1}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* SSL Help Modal */}
        {showSslHelpModal && (
          <div
            ref={sslHelpModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="ssl-help-modal-title"
            onKeyDown={(e) => e.key === 'Escape' && handleCloseSslHelpModal()}
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="ssl-help-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <HelpCircle className="w-5 h-5" />
                  {SSL_HELP_PAGES[sslHelpPage].title}
                </h3>
                <button
                  onClick={handleCloseSslHelpModal}
                  className="text-text-muted hover:text-text transition-colors"
                  aria-label={t('common.close', 'Close')}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="min-h-[280px]">
                {SSL_HELP_PAGES[sslHelpPage].content === 'overview' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.sslHelp.overviewDesc',
                        'Use these settings when enabling HTTPS. If a reverse proxy handles TLS for you, only the proxy settings are needed.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted mb-1">
                        {t(
                          'adminDeployment.sslHelp.overviewNote',
                          'If you use a reverse proxy like Nginx or Caddy, set TRUSTED_PROXIES and leave SSL_CERT_PATH/SSL_KEY_PATH empty.'
                        )}
                      </p>
                      <p className="text-xs text-text-muted mt-2">
                        {t(
                          'adminDeployment.sslHelp.overviewWhen',
                          'Change this when you move from HTTP to HTTPS or add a proxy/CDN.'
                        )}
                      </p>
                    </div>
                  </div>
                ) : SSL_HELP_PAGES[sslHelpPage].content === 'certs' ? (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.sslHelp.certsDesc',
                        'Only set certificate paths when the backend handles TLS directly.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.sslHelp.certsWhen',
                          'Use this if you are not terminating TLS in a reverse proxy.'
                        )}
                      </p>
                    </div>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          SSL_CERT_PATH
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          /etc/ssl/certs/your-cert.pem
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          SSL_KEY_PATH
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          /etc/ssl/private/your-key.pem
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          TRUSTED_PROXIES
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.sslHelp.proxiesDesc',
                            'Comma-separated proxy identifiers (cloudflare, aws, custom).'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <p className="text-sm text-text-muted mb-4">
                      {t(
                        'adminDeployment.sslHelp.httpsDesc',
                        'Control HTTPS behavior and monitoring once TLS is enabled.'
                      )}
                    </p>
                    <div className="bg-surface-overlay border border-border rounded-lg p-3">
                      <p className="text-xs text-text-muted">
                        {t(
                          'adminDeployment.sslHelp.httpsWhen',
                          'Adjust these after HTTPS is working and you want stricter security.'
                        )}
                      </p>
                    </div>
                    <div className="space-y-2">
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          FORCE_HTTPS
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.sslHelp.forceDesc',
                            'Redirect HTTP requests to HTTPS.'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          HSTS_MAX_AGE
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.sslHelp.hstsDesc',
                            'Browser HSTS max-age in seconds (only enable after HTTPS is stable).'
                          )}
                        </p>
                      </div>
                      <div className="bg-surface-overlay border border-border rounded-lg p-3">
                        <p className="text-sm font-medium text-text">
                          MONITORING_URL
                        </p>
                        <p className="text-xs text-text-muted mt-1">
                          {t(
                            'adminDeployment.sslHelp.monitoringDesc',
                            'Public health endpoint used by monitoring services.'
                          )}
                        </p>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between mt-6 pt-4 border-t border-border">
                <button
                  onClick={() =>
                    setSslHelpPage((prev) => Math.max(0, prev - 1))
                  }
                  disabled={sslHelpPage === 0}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                  {t('common.previous', 'Previous')}
                </button>

                <div className="flex items-center gap-1.5">
                  {SSL_HELP_PAGES.map((_, index) => (
                    <button
                      key={index}
                      onClick={() => setSslHelpPage(index)}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        index === sslHelpPage
                          ? 'bg-accent'
                          : 'bg-border hover:bg-text-muted'
                      }`}
                      aria-label={`${t('adminDeployment.sslHelp.goToPage', 'Go to page')} ${index + 1}`}
                    />
                  ))}
                </div>

                <button
                  onClick={() =>
                    setSslHelpPage((prev) =>
                      Math.min(SSL_HELP_PAGES.length - 1, prev + 1)
                    )
                  }
                  disabled={sslHelpPage === SSL_HELP_PAGES.length - 1}
                  className="flex items-center gap-1 text-sm text-text-muted hover:text-text disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  {t('common.next', 'Next')}
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Key Migration Modal */}
        {showMigrationModal && (
          <div
            ref={migrationModalRef}
            className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
            role="dialog"
            aria-modal="true"
            aria-labelledby="migration-modal-title"
            onKeyDown={(e) =>
              e.key === 'Escape' &&
              migrationStep !== 'progress' &&
              handleCloseMigrationModal()
            }
            tabIndex={-1}
          >
            <div className="bg-surface border border-border rounded-xl p-6 w-full max-w-md mx-4 shadow-xl">
              <div className="flex items-center justify-between mb-4">
                <h3
                  id="migration-modal-title"
                  className="text-lg font-semibold text-text flex items-center gap-2"
                >
                  <Key className="w-5 h-5" />
                  {t(
                    'adminDeployment.keyMigration.modalTitle',
                    'Admin Key Migration'
                  )}
                </h3>
                {migrationStep !== 'progress' && (
                  <button
                    onClick={handleCloseMigrationModal}
                    className="text-text-muted hover:text-text transition-colors"
                    aria-label={t('common.close', 'Close')}
                  >
                    <X className="w-5 h-5" />
                  </button>
                )}
              </div>

              {/* Input Step */}
              {migrationStep === 'input' && (
                <div className="space-y-4">
                  <Callout
                    label={t(
                      'adminDeployment.keyMigration.inputWarningLabel',
                      'Admin key migration warning'
                    )}
                    tone="warning"
                  >
                    <p className="text-xs text-warning">
                      {t(
                        'adminDeployment.keyMigration.warning',
                        'This operation is irreversible. Make sure you have access to the new private key before proceeding.'
                      )}
                    </p>
                  </Callout>

                  <TextField
                    className="font-mono"
                    label={t(
                      'adminDeployment.keyMigration.newPubkeyLabel',
                      'New Admin Pubkey'
                    )}
                    value={newAdminPubkey}
                    onChange={(e) => setNewAdminPubkey(e.target.value)}
                    placeholder={t(
                      'adminDeployment.extracted.npub1_or_64_char_hex_b8e906',
                      'npub1... or 64-char hex'
                    )}
                    description={t(
                      'adminDeployment.keyMigration.pubkeyHint',
                      'Enter the public key (npub or hex) of the new admin'
                    )}
                  />

                  <div className="flex gap-3">
                    <Button
                      onClick={handleCloseMigrationModal}
                      className="flex-1"
                      variant="secondary"
                    >
                      {t('common.cancel', 'Cancel')}
                    </Button>
                    <Button
                      onClick={handleMigrationPrepare}
                      disabled={!newAdminPubkey.trim() || migrationLoading}
                      className="flex-1"
                      variant="secondary"
                      leadingIcon={
                        migrationLoading ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          <Key className="w-4 h-4" />
                        )
                      }
                    >
                      {t('common.continue', 'Continue')}
                    </Button>
                  </div>
                </div>
              )}

              {/* Confirm Step */}
              {migrationStep === 'confirm' && migrationPrepareData && (
                <div className="space-y-4">
                  <div className="bg-surface-overlay border border-border rounded-lg p-4 space-y-2">
                    <div className="flex justify-between text-sm">
                      <span className="text-text-muted">
                        {t(
                          'adminDeployment.keyMigration.usersToMigrate',
                          'Users to migrate:'
                        )}
                      </span>
                      <span className="text-text font-medium">
                        {migrationPrepareData.user_count}
                      </span>
                    </div>
                    <div className="flex justify-between text-sm">
                      <span className="text-text-muted">
                        {t(
                          'adminDeployment.keyMigration.fieldsToMigrate',
                          'Field values to migrate:'
                        )}
                      </span>
                      <span className="text-text font-medium">
                        {migrationPrepareData.field_value_count}
                      </span>
                    </div>
                    <div className="pt-2 border-t border-border">
                      <div className="text-xs text-text-muted mb-1">
                        {t('adminDeployment.keyMigration.fromKey', 'From:')}
                      </div>
                      <div className="text-xs font-mono text-text truncate">
                        {migrationPrepareData.admin_pubkey}
                      </div>
                    </div>
                    <div>
                      <div className="text-xs text-text-muted mb-1">
                        {t('adminDeployment.keyMigration.toKey', 'To:')}
                      </div>
                      <div className="text-xs font-mono text-text truncate">
                        {newAdminPubkey}
                      </div>
                    </div>
                  </div>

                  <Callout
                    label={t(
                      'adminDeployment.keyMigration.destructiveWarningLabel',
                      'Admin key migration destructive warning'
                    )}
                    tone="error"
                  >
                    <p className="text-xs text-error font-medium mb-1">
                      {t(
                        'adminDeployment.keyMigration.confirmWarningTitle',
                        'This action cannot be undone'
                      )}
                    </p>
                    <p className="text-xs text-error">
                      {t(
                        'adminDeployment.keyMigration.confirmWarning',
                        'You will be signed out after migration and must log in with the new key.'
                      )}
                    </p>
                  </Callout>

                  <div className="flex gap-3">
                    <Button
                      onClick={() => setMigrationStep('input')}
                      className="flex-1"
                      variant="secondary"
                    >
                      {t('common.back', 'Back')}
                    </Button>
                    <Button
                      onClick={handleMigrationExecute}
                      className="flex-1"
                      variant="danger"
                      leadingIcon={<Key className="w-4 h-4" />}
                    >
                      {t(
                        'adminDeployment.keyMigration.confirmButton',
                        'Migrate Now'
                      )}
                    </Button>
                  </div>
                </div>
              )}

              {/* Progress Step */}
              {migrationStep === 'progress' && (
                <div className="space-y-4 py-4">
                  <div className="flex flex-col items-center gap-4">
                    <Loader2 className="w-12 h-12 text-accent animate-spin" />
                    <p className="text-sm text-text-muted text-center">
                      {migrationProgress}
                    </p>
                  </div>
                  <p className="text-xs text-text-muted text-center">
                    {t(
                      'adminDeployment.keyMigration.doNotClose',
                      'Please do not close this window.'
                    )}
                  </p>
                </div>
              )}

              {/* Complete Step */}
              {migrationStep === 'complete' && migrationResult && (
                <div className="space-y-4">
                  <div className="flex flex-col items-center gap-4 py-4">
                    <CheckCircle className="w-12 h-12 text-success" />
                    <p className="text-sm text-text text-center font-medium">
                      {migrationResult.message}
                    </p>
                  </div>

                  <div className="bg-surface-overlay border border-border rounded-lg p-3 space-y-1">
                    <div className="flex justify-between text-sm">
                      <span className="text-text-muted">
                        {t(
                          'adminDeployment.keyMigration.usersMigrated',
                          'Users migrated:'
                        )}
                      </span>
                      <span className="text-success font-medium">
                        {migrationResult.usersMigrated}
                      </span>
                    </div>
                    <div className="flex justify-between text-sm">
                      <span className="text-text-muted">
                        {t(
                          'adminDeployment.keyMigration.fieldsMigrated',
                          'Fields migrated:'
                        )}
                      </span>
                      <span className="text-success font-medium">
                        {migrationResult.fieldValuesMigrated}
                      </span>
                    </div>
                  </div>

                  <p className="text-xs text-text-muted text-center">
                    {t(
                      'adminDeployment.keyMigration.signInPrompt',
                      'Click below to sign in with your new key.'
                    )}
                  </p>

                  <button
                    onClick={handleMigrationComplete}
                    className="w-full bg-accent text-accent-text rounded-lg px-4 py-2.5 text-sm font-medium hover:bg-accent-hover transition-all"
                  >
                    {t('adminDeployment.keyMigration.goToLogin', 'Go to Login')}
                  </button>
                </div>
              )}

              {/* Error Step */}
              {migrationStep === 'error' && migrationResult && (
                <div className="space-y-4">
                  <div className="flex flex-col items-center gap-4 py-4">
                    <XCircle className="w-12 h-12 text-error" />
                    <p className="text-sm text-error text-center">
                      {migrationResult.message}
                    </p>
                  </div>

                  <button
                    onClick={handleCloseMigrationModal}
                    className="w-full bg-surface-overlay border border-border text-text rounded-lg px-4 py-2.5 text-sm font-medium hover:bg-surface transition-all"
                  >
                    {t('common.close', 'Close')}
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </OnboardingCard>
  );
}
