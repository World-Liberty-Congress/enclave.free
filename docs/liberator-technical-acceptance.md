# Liberator Technical Acceptance record

Prepared October 15, 2026. **Technical verification is complete; the record is ready for organization sign-off.** The operator confirms the organization handoff is complete. This records technical readiness for Liberator's opening decision; it does not record organization sign-off, authorize Public Opening or retire the personal demo.

## Deployment and custody

Production is `https://liberator.global`, running `release-65448c1` from Enclave source `65448c14c3bd5fe61e7e1646c89aaed04c98fa29` and Sage `ad12cc9fa51c6b0db03753055a7673dadf4b0153`. DigitalOcean deployment/backup storage and the Enclave/Sage repositories are under the organization accounts. The migration changes remain in [draft PR #1](https://github.com/World-Liberty-Congress/enclave.free/pull/1); this record does not merge or change that review state.

Liberator is Owner of the **Liberator Recovery** Proton Pass vault. The personal account remains Admin. The organization account can read all eight items: server SSH access, backup storage credentials, backup decryption password, content-encryption key, complete production configuration, recovery guide, backup alert recipient and Nostr Admin recovery key. The Admin-key copy exactly matches the original and persists after reload; the personal original remains saved. The operator explicitly confirms that everything has been handed off. No credential values are included in this record.

## Verification

| Acceptance check | Result and evidence |
| --- | --- |
| Organization configuration and knowledge | Filtered replay preserves accepted settings, 30 active files, 964 chunks and 910 vectors; exact metadata, plaintext hashes, Sage settings and retrieval pass. Production starts with zero Users, one dedicated Admin and no migrated conversations. [Replay receipt](production-final-replay-verification.json), [fixed-release checks](production-fix-promotion-verification.json). |
| Admin recovery and protected access | Real Zen NIP-07 sign-in, dashboard access, encrypted profile/transcript decryption, logout invalidation and sole-Admin recovery pass. Legacy dev token, signed dev-mode payload and missing bearer each receive 401 on listing/chat. [Fixed-release receipt](production-fix-promotion-verification.json). |
| User login and application behavior | Actual Mailgun sign-in email reaches Inbox; HTTPS magic-link login, onboarding and User chat pass. Three supported personas, User-only access and deletion with retained encrypted logs pass. Temporary verification data is removed. [Application record](liberator-production-preparation.md#actual-email-login-and-onboarding--2026-10-12), [promotion receipt](production-fix-promotion-verification.json). |
| Primary and fallback providers | Real Tinfoil inference/embeddings and protected Admin knowledge retrieval pass. Real Maple supplies Admin and streamed User answers after two injected primary HTTP 429 responses; normal primary routing recovers. [Fallback receipt](production-maple-notification-verification.json). |
| Encrypted recovery | Fixed-release snapshot `e3d2f3cee334c1dfb53432090c72ab8d1ae234ceaae54dd0fd8f12fe87bc9e50` passes isolated application/data/vector/Admin recovery and rejection checks; isolated startup takes 114.1 seconds. October 14 snapshot `8968959cf98aeda3934c71f206a1f9900e33f7b0968bcc0974225a177918289a` passes verified file restoration with eleven configuration/operations files byte-equal. Both temporary environments are removed. These are historical restore receipts, not promises that every checkpoint remains retained. [Recovery receipts](production-fix-promotion-verification.json), [handoff receipt](production-recovery-handoff-verification.json). |
| Daily automation and retention | First timer-triggered run succeeds October 15, 04:10:37–04:15:29 UTC, exit 0. Snapshot `09d12fe9780f1585d7db122efcbd0d0a3a70732fd84872f772ff636381f14cab` has 26 sources and three custom images and is the newest complete snapshot in the repository. Retention applies 14 daily/4 weekly copies successfully. A restore of this scheduled snapshot was not repeated. [Live receipt](production-technical-acceptance-verification.json). |
| Failure notifications | Installed systemd hook covers backup, application recovery and retention failures. A labelled failure test reaches the selected Liberator Proton Inbox; operator and Safari inspection confirm delivery. The recipient and recovery instructions are in the vault. [Notification receipt](production-maple-notification-verification.json). |
| Current health and access gate | October 15, 20:04 UTC: all ten services healthy, no failed systemd units, active source revision unchanged. Installed Caddy configuration exactly matches the closed template; public HTTPS returns 503. Timer enabled/active/waiting, next run October 16, 04:14:16 UTC. [Live receipt](production-technical-acceptance-verification.json). |
| Recovery custody and ownership | Organization owns the eight-item vault; both-account ownership and organization readability are verified. Operator confirms complete handoff. [Current custody receipt](production-recovery-handoff-verification.json). |

October 15 verification used read-only service, journal, repository, container and HTTPS checks. It did not stop services, send additional email or open production. Earlier dated receipts remain historical and are superseded by this record for current custody and scheduling status.

## Organization decisions and operating notes

- **Technical Acceptance sign-off:** ready for the organization to record; no sign-off is inferred from credential handoff.
- **Public Opening:** Liberator chooses whether and when to open. The Technical Operator can apply the production Caddy template and check sign-in, email, chat and Admin access after authorization.
- **Demo Retirement:** retain the personal demo until acceptance and a separate retirement decision. Verify production remains healthy and recovery access is available before deleting personal resources.
- **Ongoing backups:** inspect service failures and successful snapshots, respond to the organization mailbox alerts, and keep the vault updated after credential/configuration changes. Consistent backups stop the application writers briefly each day; the first scheduled job took 4 minutes 52 seconds in total, including preparation, restart and retention, so this is not a measured public outage duration.
- **Separate product follow-up:** User deletion detaches retained Session Logs but does not purge Sage-owned conversations/memory. This remains a lifecycle follow-up; migration verification conversations were removed through their normal product endpoints. [Promotion details](production-fix-promotion-verification.json).

Organization sign-off: **not yet recorded**. Public Opening: **closed**. Personal demo: **retained**.
