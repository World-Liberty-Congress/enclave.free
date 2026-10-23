"""Durable analytics classification for instance-derived Admin test identities.

The provisioning persona is independent of the user's current User Type. This
registry identifies synthetic usage; it never authorizes impersonation.
"""

import hmac
from itertools import chain

import database
import impersonation


def _normalized_pubkey(value: str | None) -> str | None:
    from nostr_keys import normalize_pubkey

    try:
        return normalize_pubkey(value) if value else None
    except ValueError:
        return None


def record_provisioning(user_id: int, admin_pubkey: str, persona_id: int | None) -> None:
    """Record only a proven derived identity, never a name or email heuristic."""
    expected = impersonation.derive_test_user_pubkey(admin_pubkey, persona_id)
    with database.get_cursor() as cursor:
        cursor.execute('SELECT pubkey FROM users WHERE id=?', (user_id,))
        row = cursor.fetchone()
        actual = _normalized_pubkey(row['pubkey']) if row else None
        if actual is None or not hmac.compare_digest(actual, expected):
            raise ValueError('User is not the derived test identity')
        cursor.execute(
            '''INSERT INTO admin_test_provenance (user_id, pubkey, provisioning_type_id)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   pubkey=excluded.pubkey, provisioning_type_id=excluded.provisioning_type_id''',
            (user_id, expected, persona_id),
        )


def initialize() -> None:
    """Add the registry and backfill existing identities without changing users.

    User Type IDs are positive SQLite AUTOINCREMENT IDs. Its durable high-water
    mark includes deleted personas, so migrated legacy identities can be proven
    even after their original type, audit history and review logs were deleted.
    Repeating the scan also handles provisioning by an older rolled-back image.
    """
    with database.get_cursor() as cursor:
        cursor.execute('''CREATE TABLE IF NOT EXISTS admin_test_provenance (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            pubkey TEXT NOT NULL,
            provisioning_type_id INTEGER
        )''')
        cursor.execute('SELECT pubkey FROM admins')
        admins = cursor.fetchall()
        if len(admins) != 1 or not admins[0]['pubkey']:
            return
        admin_pubkey = admins[0]['pubkey']
        cursor.execute('''SELECT u.id, u.pubkey FROM users u
            LEFT JOIN admin_test_provenance p ON p.user_id=u.id AND p.pubkey=u.pubkey
            WHERE p.user_id IS NULL AND u.pubkey IS NOT NULL''')
        candidates = {key: row['id'] for row in cursor.fetchall()
                      if (key := _normalized_pubkey(row['pubkey'])) is not None}
        cursor.execute("SELECT seq FROM sqlite_sequence WHERE name='user_types'")
        sequence = cursor.fetchone()
        max_type_id = int(sequence['seq']) if sequence else 0
    if not candidates:
        return
    for persona_id in chain([None], range(1, max_type_id + 1)):
        expected = impersonation.derive_test_user_pubkey(admin_pubkey, persona_id)
        user_id = candidates.pop(expected, None)
        if user_id is not None:
            record_provisioning(user_id, admin_pubkey, persona_id)
        if not candidates:
            break


def is_test_identity(user_id: int) -> bool:
    """Classify retained usage independently of mutable persona membership."""
    with database.get_cursor() as cursor:
        cursor.execute('''SELECT u.pubkey, p.pubkey AS provisioned_pubkey
            FROM users u LEFT JOIN admin_test_provenance p ON p.user_id=u.id
            WHERE u.id=?''', (user_id,))
        row = cursor.fetchone()
    if row is None:
        return False
    actual = _normalized_pubkey(row['pubkey'])
    if actual and row['provisioned_pubkey']:
        return hmac.compare_digest(actual, row['provisioned_pubkey'])
    # Compatibility with identities created through older callers in this
    # process. Startup backfill handles accounts already migrated before upgrade.
    return impersonation.is_provisioned_test_user(user_id)
