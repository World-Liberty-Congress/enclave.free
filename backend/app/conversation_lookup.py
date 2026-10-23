"""Admin-only retained review ciphertext projection; never reads Sage plaintext."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

import auth
import database
import session_logs

router = APIRouter(prefix='/admin/conversation-lookup')
Id = Annotated[StrictInt, Field(gt=0, le=9007199254740991)]
Position = Annotated[StrictInt, Field(ge=0, le=9007199254740991)]
MAX_CIPHERTEXT_BYTES = 2 * 1024 * 1024


def utc_time(value: str) -> str:
    if len(value) > 40:
        raise ValueError('Invalid UTC interval')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('Explicit UTC interval required')
    return parsed.astimezone(timezone.utc).isoformat()


class Projection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_ids: list[Id] | None = Field(default=None, min_length=1, max_length=20)
    start: StrictStr | None = None
    end: StrictStr | None = None
    feedback: Literal['any', 'up', 'down', 'none'] | None = None
    source: Literal['user', 'admin_test', 'all'] = 'user'
    after_id: Position = 0
    through_id: Position | None = None
    log_id: Annotated[StrictStr, Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')] | None = None

    @model_validator(mode='after')
    def validate_selection(self) -> Self:
        if self.user_ids and len(set(self.user_ids)) != len(self.user_ids):
            raise ValueError('Duplicate User IDs')
        if self.start is not None:
            self.start = utc_time(self.start)
        if self.end is not None:
            self.end = utc_time(self.end)
        if self.start and self.end and self.start >= self.end:
            raise ValueError('Empty time interval')
        if self.through_id is not None and self.after_id > self.through_id:
            raise ValueError('Invalid ID window')
        return self


async def current_admin(request: Request) -> dict[str, Any]:
    # This channel must represent the live Admin browser session. A bearer may
    # select an impersonated User even while the browser retains Admin cookies.
    if request.headers.get('authorization'):
        raise HTTPException(403, 'Admin cookie session required')
    token = request.cookies.get(auth.ADMIN_SESSION_COOKIE_NAME)
    admin = await auth.require_admin(authorization=None, admin_session_cookie=token)
    if request.method == 'POST':
        cookie = request.cookies.get(auth.CSRF_COOKIE_NAME, '')
        header = request.headers.get('x-csrf-token', '')
        if not cookie or not hmac.compare_digest(cookie, header):
            raise HTTPException(403, 'CSRF token required')
    return admin


@router.get('/session')
async def lookup_session(request: Request, response: Response, admin: dict = Depends(current_admin)) -> dict:
    response.headers['Cache-Control'] = 'no-store'
    return {'pubkey': admin['pubkey'], 'session_fingerprint': hashlib.sha256(request.cookies[auth.ADMIN_SESSION_COOKIE_NAME].encode()).hexdigest()}


def project(selection: Projection) -> dict[str, Any]:
    clauses, params = ['1=1'], []
    if selection.source != 'all':
        clauses.append('l.source = ?')
        params.append(selection.source)
    if selection.user_ids:
        clauses.append('l.subject_user_id IN (' + ','.join('?' for _ in selection.user_ids) + ')')
        params.extend(selection.user_ids)
    # Message timestamps are inside ciphertext. A log created before the
    # interval can contain matching recent messages; retain all candidates.
    if selection.feedback:
        exists = 'EXISTS (SELECT 1 FROM session_log_feedback f WHERE f.log_id = l.log_id'
        if selection.feedback in ('up', 'down'):
            exists += ' AND f.rating = ?'
            params.append(selection.feedback)
        clauses.append(('NOT ' if selection.feedback == 'none' else '') + exists + ')')
    where = ' AND '.join(clauses)
    with database.get_cursor() as cursor:
        through = selection.through_id
        if through is None:
            cursor.execute(f'SELECT COALESCE(MAX(l.id),0) AS n FROM session_logs l WHERE {where}', params)
            through = cursor.fetchone()['n']
        window, bounded = where + ' AND l.id <= ?', [*params, through]
        cursor.execute(f'SELECT COUNT(*) AS n FROM session_logs l WHERE {window}', bounded)
        eligible = cursor.fetchone()['n']
        extra, values = ' AND l.id > ?', [*bounded, selection.after_id]
        if selection.log_id:
            extra += ' AND l.log_id = ?'
            values.append(selection.log_id)
        cursor.execute(f'SELECT l.id, l.log_id FROM session_logs l WHERE {window}{extra} ORDER BY l.id LIMIT 2', values)
        rows = cursor.fetchall()
    projected = []
    if rows:
        row = rows[0]
        # Use the retained review store, including its legacy encrypted files.
        # No fallback to raw Sage messages is permitted after review deletion.
        log = session_logs.get_session_log(row['log_id'])
        if log is not None:
            feedback = [{key: item.get(key) for key in ('message_id', 'rating', 'comment_ciphertext', 'comment_ephemeral_pubkey', 'created_at')} for item in log['feedback']]
            record = {
                'id': row['id'], 'conversation_id': log['log_id'],
                'user_id': log['subject_user_id'], 'source': log['source'],
                'created_at': log['created_at'], 'encrypted_to_pubkey': log['encrypted_to_pubkey'],
                'transcript': {'ciphertext': log['transcript_ciphertext'], 'ephemeral_pubkey': log['transcript_ephemeral_pubkey']} if log['transcript_ciphertext'] else None,
                'feedback': feedback, 'status': 'ok',
            }
            if len(json.dumps(record, ensure_ascii=False).encode()) > MAX_CIPHERTEXT_BYTES:
                record.update(transcript=None, feedback=[], status='oversized')
            projected.append(record)
    return {'logs': projected, 'through_id': through, 'eligible_logs': eligible,
            'next_after_id': rows[0]['id'] if len(rows) > 1 else None,
            'time_basis': 'message_ts', 'consistency': 'live_id_window'}


@router.post('/projection')
async def lookup_projection(selection: Projection, response: Response, admin: dict = Depends(current_admin)) -> dict:
    response.headers['Cache-Control'] = 'no-store'
    return project(selection)
