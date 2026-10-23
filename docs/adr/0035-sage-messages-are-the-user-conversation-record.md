# Sage messages are the User Conversation record

Status: Accepted.

The encrypted Admin review copy of a User Conversation (`session_logs` with `source='user'`) is built from Sage's stored messages, not from what the browser remembers. After each exchange Sage sends the backend every retained user and assistant message of the Conversation, oldest first, each with its Sage message ID. The browser sends only the new message and the Conversation ID. Every transcript message carries a stable `message_id`, recorded usage (`turn_count`) is the number of recorded messages, and review feedback is saved against a message ID rather than a position in the transcript.

The record only moves forward. Each update carries the Sage sequence number of its newest message, and Sage numbers messages in the order it stores them, so a later read of a Conversation always has a higher version, compared as (sequence number, message count). The backend ignores an update at or below the stored version, such as a retry or a late arrival, and writes with a compare-and-set on that version, so concurrent or delayed updates cannot replace a newer record with an older one. The record's retention is separate from Sage Session Memory. A newer history that no longer contains the recorded messages, such as a Conversation continued after its Session Memory was deleted, therefore does not overwrite the record: it is archived intact, with its feedback, and the continuation starts a new record. The backend cannot merge them because it cannot decrypt the stored transcript.

Sage marks a Conversation's record as owed when a turn starts, once the user's message is stored, and before each update, by setting the time it should next be retried, five minutes ahead. An update clears only its own mark, and only if no newer messages exist. Every minute, starting at boot, Sage resends records whose retry time has passed, rescheduling any that fail again behind the others. An update lost to a backend failure, a dropped connection, or a restart is therefore repaired from Sage's own records. Because each turn pushes the retry time out, retries do not normally overlap a turn in progress, and an overlap is harmless because the backend ignores updates that are not newer.

Test User Session records are still saved once by the Admin browser at the end of a trial, using the browser's turn IDs as message IDs.

## Consequences

- Long Conversations record every message, and feedback stays on the answer it was given for as the Conversation grows.
- Each update re-encrypts the whole transcript. That is acceptable at current Conversation sizes; incremental storage can replace it behind `session_logs.record_user_conversation` without changing Sage, chat, or feedback.
- A Conversation that continues after an Admin deleted its record gets a new record containing everything Sage still retains, not only the messages since the deletion. Recording a deletion point is a possible follow-up.
- Feedback saved before this decision stays on its answer only when that is reliable, meaning the transcript has not changed since the rating; it is then named `legacy-<index>` (or the recorded message ID at that position). Other existing feedback is kept as `legacy-unplaced-<id>`, attached to no answer. When Sage next updates a legacy record, its legacy feedback also detaches, because a positional snapshot cannot be matched reliably to Sage messages. Detached feedback remains in the database and exports. Logs saved before transcripts recorded their message order open read-only.

## Rejected alternatives

- Send more browser history. Rejected because the browser's copy is bounded, truncated, and lost on refresh, and the record would still be a snapshot of whatever the browser held.
- Keep positions and remap them on each update. Rejected because a position names a slot, not an answer; any change to what the snapshot contains moves feedback to a different answer.
