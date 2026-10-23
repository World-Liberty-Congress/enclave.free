import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FeedbackView } from './FeedbackView';
import { decryptField, hasNip04Support } from '../../../utils/encryption';
import {
  deleteSessionLog,
  exportSessionLog,
  getSessionLog,
  listSessionLogs,
  recordSessionLogPlaintextExport,
  setMessageFeedback,
} from '../../../utils/sessionLogsApi';

vi.mock('../../../utils/encryption', () => ({
  decryptField: vi.fn(),
  hasNip04Support: vi.fn(),
}));

vi.mock('../../../utils/sessionLogsApi', () => ({
  deleteSessionLog: vi.fn(),
  exportSessionLog: vi.fn(),
  getSessionLog: vi.fn(),
  listSessionLogs: vi.fn(),
  recordSessionLogPlaintextExport: vi.fn(),
  setMessageFeedback: vi.fn(),
}));

const mockDecryptField = vi.mocked(decryptField);
const mockHasNip04Support = vi.mocked(hasNip04Support);
const mockDeleteSessionLog = vi.mocked(deleteSessionLog);
const mockExportSessionLog = vi.mocked(exportSessionLog);
const mockGetSessionLog = vi.mocked(getSessionLog);
const mockListSessionLogs = vi.mocked(listSessionLogs);
const mockRecordSessionLogPlaintextExport = vi.mocked(
  recordSessionLogPlaintextExport
);
const mockSetMessageFeedback = vi.mocked(setMessageFeedback);
const mockCreateObjectURL = vi.fn(() => 'blob:test-feedback-log-1');
const mockRevokeObjectURL = vi.fn();
const mockAnchorClick = vi.fn();
let originalCreateObjectURLDescriptor: PropertyDescriptor | undefined;
let originalRevokeObjectURLDescriptor: PropertyDescriptor | undefined;
let originalAnchorClickDescriptor: PropertyDescriptor | undefined;

function restoreProperty(
  target: object,
  property: PropertyKey,
  descriptor: PropertyDescriptor | undefined
) {
  if (descriptor) {
    Object.defineProperty(target, property, descriptor);
  } else {
    Reflect.deleteProperty(target, property);
  }
}

async function openStudentTrial() {
  const user = userEvent.setup();
  const trialButton = (await screen.findByText('Student trial')).closest(
    'button'
  );
  await user.click(trialButton as HTMLButtonElement);
}

async function expectTranscriptIntegrityFailure() {
  await openStudentTrial();
  expect(
    await screen.findByText(
      'The transcript is unavailable or incomplete and cannot be exported.'
    )
  ).toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: 'Export' })
  ).not.toBeInTheDocument();
  expect(mockRecordSessionLogPlaintextExport).not.toHaveBeenCalled();
  expect(mockAnchorClick).not.toHaveBeenCalled();
}

describe('FeedbackView', () => {
  beforeEach(() => {
    originalCreateObjectURLDescriptor = Object.getOwnPropertyDescriptor(
      URL,
      'createObjectURL'
    );
    originalRevokeObjectURLDescriptor = Object.getOwnPropertyDescriptor(
      URL,
      'revokeObjectURL'
    );
    originalAnchorClickDescriptor = Object.getOwnPropertyDescriptor(
      HTMLAnchorElement.prototype,
      'click'
    );

    mockDecryptField.mockReset();
    mockHasNip04Support.mockReset();
    mockDeleteSessionLog.mockReset();
    mockExportSessionLog.mockReset();
    mockGetSessionLog.mockReset();
    mockListSessionLogs.mockReset();
    mockRecordSessionLogPlaintextExport.mockReset();
    mockSetMessageFeedback.mockReset();
    mockCreateObjectURL.mockClear();
    mockRevokeObjectURL.mockClear();
    mockAnchorClick.mockClear();

    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: mockCreateObjectURL,
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: mockRevokeObjectURL,
    });
    Object.defineProperty(HTMLAnchorElement.prototype, 'click', {
      configurable: true,
      value: mockAnchorClick,
    });

    mockHasNip04Support.mockReturnValue(true);
    mockExportSessionLog.mockResolvedValue(
      new Blob(['zip'], { type: 'application/zip' })
    );
    mockRecordSessionLogPlaintextExport.mockResolvedValue();
    mockListSessionLogs.mockResolvedValue([
      {
        log_id: 'log-1',
        source: 'admin_test',
        title: 'Student trial',
        subject_user_id: 42,
        user_type_id: 1,
        sage_session_id: 'sage-1',
        turn_count: 2,
        status: 'completed',
        created_by: 'admin',
        created_at: null,
        updated_at: null,
        completed_at: null,
        has_transcript: true,
      },
    ]);
    mockGetSessionLog.mockResolvedValue({
      log_id: 'log-1',
      source: 'admin_test',
      title: 'Student trial',
      subject_user_id: 42,
      user_type_id: 1,
      sage_session_id: 'sage-1',
      turn_count: 2,
      status: 'completed',
      created_by: 'admin',
      created_at: null,
      updated_at: null,
      completed_at: null,
      has_transcript: true,
      transcript_ciphertext: 'encrypted-transcript',
      transcript_ephemeral_pubkey: 'ephemeral-pubkey',
      encrypted_to_pubkey: 'admin-pubkey',
      transcript_messages: [
        { message_id: 'm-question', role: 'user' },
        { message_id: 'm-answer', role: 'assistant' },
      ],
      feedback: [],
    });
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [
          { role: 'user', content: 'Can you help me?' },
          { role: 'assistant', content: 'Yes, here is a plan.' },
        ],
      })
    );
  });

  afterEach(() => {
    cleanup();
    restoreProperty(URL, 'createObjectURL', originalCreateObjectURLDescriptor);
    restoreProperty(URL, 'revokeObjectURL', originalRevokeObjectURLDescriptor);
    restoreProperty(
      HTMLAnchorElement.prototype,
      'click',
      originalAnchorClickDescriptor
    );
  });

  it('shows a clear error when rating feedback is rejected', async () => {
    const user = userEvent.setup();
    mockSetMessageFeedback.mockRejectedValue(
      new Error('Feedback can only be saved for assistant turns')
    );

    render(<FeedbackView />);

    expect(await screen.findByText('Test User Session')).toBeInTheDocument();
    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    expect(trialButton).toBeInstanceOf(HTMLButtonElement);
    await user.click(trialButton as HTMLButtonElement);
    await screen.findByText('Yes, here is a plan.');
    await user.click(screen.getByTitle('Needs work'));

    expect(
      await screen.findByText('Feedback can only be saved for assistant turns')
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText('Saved')).not.toBeInTheDocument();
    });
    expect(screen.getByTitle('Needs work')).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('exports the selected session log as a decrypted plaintext JSON download', async () => {
    const user = userEvent.setup();

    render(<FeedbackView />);

    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    expect(trialButton).toBeInstanceOf(HTMLButtonElement);
    await user.click(trialButton as HTMLButtonElement);
    await screen.findByText('Yes, here is a plan.');

    await user.click(screen.getByRole('button', { name: 'Export' }));

    await waitFor(() => {
      expect(mockCreateObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    });
    expect(mockRecordSessionLogPlaintextExport).toHaveBeenCalledWith('log-1');
    // The backend ciphertext (.zip) export is no longer used.
    expect(mockExportSessionLog).not.toHaveBeenCalled();
    expect(mockAnchorClick).toHaveBeenCalled();
    expect(mockRevokeObjectURL).toHaveBeenCalledWith(
      'blob:test-feedback-log-1'
    );

    // The download is decrypted plaintext JSON containing the transcript.
    const blob = (mockCreateObjectURL.mock.calls[0] as unknown as [Blob])[0];
    expect(blob.type).toBe('application/json');
    const text = await blob.text();
    expect(text).toContain('Yes, here is a plan.');
    expect(text).toContain('"log_id": "log-1"');
    expect(text).toContain('"tools_used": []');
  });

  it('does not download a plaintext export when audit recording fails', async () => {
    const user = userEvent.setup();
    mockRecordSessionLogPlaintextExport.mockRejectedValue(
      new Error('Export could not be audited')
    );

    render(<FeedbackView />);

    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    await user.click(trialButton as HTMLButtonElement);
    await screen.findByText('Yes, here is a plan.');
    await user.click(screen.getByRole('button', { name: 'Export' }));

    expect(
      await screen.findByText('Export could not be audited')
    ).toBeInTheDocument();
    expect(mockAnchorClick).not.toHaveBeenCalled();
  });

  it('blocks plaintext export when the transcript is missing its decryption key', async () => {
    const sessionLog = await mockGetSessionLog('log-1');
    mockGetSessionLog.mockResolvedValue({
      ...sessionLog,
      transcript_ephemeral_pubkey: null,
    });

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
    expect(mockDecryptField).not.toHaveBeenCalled();
  });

  it('blocks plaintext export when decrypted transcript data has no turns array', async () => {
    mockDecryptField.mockResolvedValue(JSON.stringify({}));

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
  });

  it('blocks plaintext export when the decrypted transcript is incomplete', async () => {
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [{ role: 'user', content: 'Can you help me?' }],
      })
    );

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
  });

  it('blocks plaintext export when decrypted transcript turns are malformed', async () => {
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [
          { role: 'user', content: 'Can you help me?', tools_used: [] },
          { role: 'assistant', content: 42, tools_used: [] },
        ],
      })
    );

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
  });

  it('blocks plaintext export when a decrypted tools-used record is malformed', async () => {
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [
          { role: 'user', content: 'Can you help me?', tools_used: [] },
          {
            role: 'assistant',
            content: 'Yes, here is a plan.',
            tools_used: [null],
          },
        ],
      })
    );

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
  });

  it('blocks plaintext export when a decrypted trace tool record is malformed', async () => {
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [
          { role: 'user', content: 'Can you help me?', tools_used: [] },
          {
            role: 'assistant',
            content: 'Yes, here is a plan.',
            tools_used: [],
            trace: { tools: [null] },
          },
        ],
      })
    );

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
  });

  it('keeps plaintext export disabled for a log with no saved transcript', async () => {
    const user = userEvent.setup();
    const sessionLog = await mockGetSessionLog('log-1');
    mockGetSessionLog.mockResolvedValue({
      ...sessionLog,
      turn_count: 0,
      has_transcript: false,
      transcript_ciphertext: null,
      transcript_ephemeral_pubkey: null,
      transcript_messages: [],
    });

    render(<FeedbackView />);

    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    await user.click(trialButton as HTMLButtonElement);

    expect(
      await screen.findByRole('button', { name: 'Export' })
    ).toBeDisabled();
    expect(mockDecryptField).not.toHaveBeenCalled();
    expect(mockRecordSessionLogPlaintextExport).not.toHaveBeenCalled();
    expect(mockAnchorClick).not.toHaveBeenCalled();
  });

  it('blocks plaintext export when an encrypted feedback comment is not decrypted', async () => {
    const user = userEvent.setup();
    const sessionLog = await mockGetSessionLog('log-1');
    mockGetSessionLog.mockResolvedValue({
      ...sessionLog,
      feedback: [
        {
          message_id: 'm-answer',
          turn_index: 1,
          rating: 'down',
          comment_ciphertext: 'encrypted-comment',
          comment_ephemeral_pubkey: 'comment-pubkey',
          created_by: 'admin',
          created_at: null,
          updated_at: null,
        },
      ],
    });
    mockDecryptField
      .mockResolvedValueOnce(
        JSON.stringify({
          turns: [
            { role: 'user', content: 'Can you help me?', tools_used: [] },
            {
              role: 'assistant',
              content: 'Yes, here is a plan.',
              tools_used: [],
            },
          ],
        })
      )
      .mockResolvedValueOnce(null);

    render(<FeedbackView />);

    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    await user.click(trialButton as HTMLButtonElement);
    await screen.findByText('Yes, here is a plan.');
    // Export is now prevented rather than refused after the click: the button
    // is disabled and the reason sits behind the info control (#643).
    const exportButton = screen.getByRole('button', { name: 'Export' });
    expect(exportButton).toBeDisabled();

    await user.click(
      screen.getByRole('button', { name: 'Why is export disabled?' })
    );
    expect(
      await screen.findByText(
        'Some feedback comments could not be decrypted. Reopen the transcript and approve every decryption request before exporting.'
      )
    ).toBeInTheDocument();

    // Clicking the disabled control must still produce no file and no audit
    // record — the failure mode behind #493.
    await user.click(exportButton);
    expect(mockRecordSessionLogPlaintextExport).not.toHaveBeenCalled();
    expect(mockAnchorClick).not.toHaveBeenCalled();
  });

  it('blocks plaintext export when encrypted feedback is missing its decryption key', async () => {
    const user = userEvent.setup();
    const sessionLog = await mockGetSessionLog('log-1');
    mockGetSessionLog.mockResolvedValue({
      ...sessionLog,
      feedback: [
        {
          message_id: 'm-answer',
          turn_index: 1,
          rating: 'down',
          comment_ciphertext: 'encrypted-comment',
          comment_ephemeral_pubkey: null,
          created_by: 'admin',
          created_at: null,
          updated_at: null,
        },
      ],
    });

    render(<FeedbackView />);

    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    await user.click(trialButton as HTMLButtonElement);
    await screen.findByText('Yes, here is a plan.');
    // Export is now prevented rather than refused after the click: the button
    // is disabled and the reason sits behind the info control (#643).
    const exportButton = screen.getByRole('button', { name: 'Export' });
    expect(exportButton).toBeDisabled();

    await user.click(
      screen.getByRole('button', { name: 'Why is export disabled?' })
    );
    expect(
      await screen.findByText(
        'Some feedback comments could not be decrypted. Reopen the transcript and approve every decryption request before exporting.'
      )
    ).toBeInTheDocument();

    // Clicking the disabled control must still produce no file and no audit
    // record — the failure mode behind #493.
    await user.click(exportButton);
    expect(mockDecryptField).toHaveBeenCalledTimes(1);
    expect(mockRecordSessionLogPlaintextExport).not.toHaveBeenCalled();
    expect(mockAnchorClick).not.toHaveBeenCalled();
  });

  it('shows saved assistant tool trace metadata after decrypting a transcript', async () => {
    const user = userEvent.setup();
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [
          { role: 'user', content: 'Find resources', tools_used: [] },
          {
            role: 'assistant',
            content: 'I found vetted resources.',
            tools_used: [
              {
                tool_id: 'curated-resources',
                tool_name: 'Curated Resources',
                output_summary: 'Found 2 vetted resources.',
              },
            ],
            trace: {
              visibility: 'detailed',
              reasoning: {
                summary: 'Sage used enabled tools before answering.',
              },
              tools: [
                {
                  id: 'curated-resources',
                  name: 'Curated Resources',
                  status: 'succeeded',
                  output_summary: 'Found 2 vetted resources.',
                },
              ],
              retrieval: [],
            },
          },
        ],
      })
    );

    render(<FeedbackView />);

    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    expect(trialButton).toBeInstanceOf(HTMLButtonElement);
    await user.click(trialButton as HTMLButtonElement);

    expect(
      await screen.findByText('I found vetted resources.')
    ).toBeInTheDocument();
    expect(
      await screen.findByText('Sage used enabled tools before answering.')
    ).toBeInTheDocument();
    expect(await screen.findByText('Curated Resources')).toBeInTheDocument();
    expect(screen.getByText('Found 2 vetted resources.')).toBeInTheDocument();
  });

  it('ignores stale transcript loads after a newer log is selected', async () => {
    const user = userEvent.setup();
    let resolveFirstLog!: (
      value: Awaited<ReturnType<typeof getSessionLog>>
    ) => void;
    mockListSessionLogs.mockResolvedValue([
      {
        log_id: 'log-1',
        source: 'admin_test',
        title: 'First trial',
        subject_user_id: 42,
        user_type_id: 1,
        sage_session_id: 'sage-1',
        turn_count: 0,
        status: 'completed',
        created_by: 'admin',
        created_at: null,
        updated_at: null,
        completed_at: null,
        has_transcript: false,
      },
      {
        log_id: 'log-2',
        source: 'admin_test',
        title: 'Second trial',
        subject_user_id: 43,
        user_type_id: 1,
        sage_session_id: 'sage-2',
        turn_count: 0,
        status: 'completed',
        created_by: 'admin',
        created_at: null,
        updated_at: null,
        completed_at: null,
        has_transcript: false,
      },
    ]);
    mockGetSessionLog.mockImplementation((logId) => {
      if (logId === 'log-1') {
        return new Promise((resolve) => {
          resolveFirstLog = resolve;
        });
      }
      return Promise.resolve({
        log_id: 'log-2',
        source: 'admin_test',
        title: 'Loaded second trial',
        subject_user_id: 43,
        user_type_id: 1,
        sage_session_id: 'sage-2',
        turn_count: 0,
        status: 'completed',
        created_by: 'admin',
        created_at: null,
        updated_at: null,
        completed_at: null,
        has_transcript: false,
        transcript_ciphertext: null,
        transcript_ephemeral_pubkey: null,
        encrypted_to_pubkey: 'admin-pubkey',
        transcript_messages: [],
        feedback: [],
      });
    });

    render(<FeedbackView />);

    await user.click(await screen.findByText('First trial'));
    await user.click(await screen.findByText('Second trial'));
    expect(await screen.findByText('Loaded second trial')).toBeInTheDocument();

    await act(async () => {
      resolveFirstLog({
        log_id: 'log-1',
        source: 'admin_test',
        title: 'Loaded first trial',
        subject_user_id: 42,
        user_type_id: 1,
        sage_session_id: 'sage-1',
        turn_count: 0,
        status: 'completed',
        created_by: 'admin',
        created_at: null,
        updated_at: null,
        completed_at: null,
        has_transcript: false,
        transcript_ciphertext: null,
        transcript_ephemeral_pubkey: null,
        encrypted_to_pubkey: 'admin-pubkey',
        transcript_messages: [],
        feedback: [],
      });
      await Promise.resolve();
    });

    expect(screen.getByText('Loaded second trial')).toBeInTheDocument();
    expect(screen.queryByText('Loaded first trial')).not.toBeInTheDocument();
  });

  it('keeps the selected log visible when delete fails', async () => {
    const user = userEvent.setup();
    mockDeleteSessionLog.mockRejectedValue(new Error('Delete failed'));

    render(<FeedbackView />);

    const trialButton = (await screen.findByText('Student trial')).closest(
      'button'
    );
    expect(trialButton).toBeInstanceOf(HTMLButtonElement);
    await user.click(trialButton as HTMLButtonElement);
    await screen.findByText('Yes, here is a plan.');

    await user.click(screen.getByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('Delete failed')).toBeInTheDocument();
    expect(screen.getByText('Yes, here is a plan.')).toBeInTheDocument();
    expect(mockListSessionLogs).toHaveBeenCalledTimes(1);
  });

  it('saves feedback against the rated answer message ID', async () => {
    const user = userEvent.setup();
    mockSetMessageFeedback.mockResolvedValue({
      message_id: 'm-answer',
      turn_index: 1,
      rating: 'down',
      comment_ciphertext: null,
      comment_ephemeral_pubkey: null,
      created_by: 'admin',
      created_at: null,
      updated_at: null,
    });

    render(<FeedbackView />);
    await openStudentTrial();
    await screen.findByText('Yes, here is a plan.');
    await user.click(screen.getByTitle('Needs work'));

    await waitFor(() => {
      expect(mockSetMessageFeedback).toHaveBeenCalledWith(
        'log-1',
        'm-answer',
        'down',
        null
      );
    });
  });

  it('shows saved feedback on the answer it was given for after the conversation grew', async () => {
    const sessionLog = await mockGetSessionLog('log-1');
    mockGetSessionLog.mockResolvedValue({
      ...sessionLog,
      source: 'user',
      turn_count: 4,
      transcript_messages: [
        { message_id: 'm-q1', role: 'user' },
        { message_id: 'm-a1', role: 'assistant' },
        { message_id: 'm-q2', role: 'user' },
        { message_id: 'm-a2', role: 'assistant' },
      ],
      feedback: [
        {
          message_id: 'm-a1',
          turn_index: 1,
          rating: 'down',
          comment_ciphertext: null,
          comment_ephemeral_pubkey: null,
          created_by: 'admin',
          created_at: null,
          updated_at: null,
        },
        {
          message_id: 'm-removed-answer',
          turn_index: null,
          rating: 'up',
          comment_ciphertext: null,
          comment_ephemeral_pubkey: null,
          created_by: 'admin',
          created_at: null,
          updated_at: null,
        },
      ],
    });
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [
          { message_id: 'm-q1', role: 'user', content: 'First question' },
          { message_id: 'm-a1', role: 'assistant', content: 'First answer' },
          { message_id: 'm-q2', role: 'user', content: 'Second question' },
          { message_id: 'm-a2', role: 'assistant', content: 'Second answer' },
        ],
      })
    );

    render(<FeedbackView />);
    await openStudentTrial();
    await screen.findByText('Second answer');

    const [firstNeedsWork, secondNeedsWork] =
      screen.getAllByTitle('Needs work');
    expect(firstNeedsWork).toHaveAttribute('aria-pressed', 'true');
    expect(secondNeedsWork).toHaveAttribute('aria-pressed', 'false');
    for (const goodAnswer of screen.getAllByTitle('Good answer')) {
      expect(goodAnswer).toHaveAttribute('aria-pressed', 'false');
    }

    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: 'Export' }));
    await waitFor(() => {
      expect(mockCreateObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    });
    const blob = (mockCreateObjectURL.mock.calls[0] as unknown as [Blob])[0];
    const exported = JSON.parse(await blob.text()) as {
      feedback: Array<{ message_id: string; turn_index: number | null }>;
    };
    expect(exported.feedback).toEqual([
      expect.objectContaining({ message_id: 'm-a1', turn_index: 1 }),
      expect.objectContaining({
        message_id: 'm-removed-answer',
        turn_index: null,
      }),
    ]);
  });

  it('blocks review when the decrypted transcript does not match its message order', async () => {
    mockDecryptField.mockResolvedValue(
      JSON.stringify({
        turns: [
          { message_id: 'm-question', role: 'user', content: 'Can you help?' },
          { message_id: 'other-answer', role: 'assistant', content: 'Yes.' },
        ],
      })
    );

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
  });

  it('still opens a log saved before its message order was recorded', async () => {
    const sessionLog = await mockGetSessionLog('log-1');
    mockGetSessionLog.mockResolvedValue({
      ...sessionLog,
      transcript_messages: [],
    });

    render(<FeedbackView />);
    await openStudentTrial();

    expect(await screen.findByText('Yes, here is a plan.')).toBeInTheDocument();
    expect(
      screen.queryByText(
        'The transcript is unavailable or incomplete and cannot be exported.'
      )
    ).not.toBeInTheDocument();
  });

  it('blocks review when the message order disagrees with the turn count', async () => {
    const sessionLog = await mockGetSessionLog('log-1');
    mockGetSessionLog.mockResolvedValue({
      ...sessionLog,
      transcript_messages: [{ message_id: 'm-question', role: 'user' }],
    });

    render(<FeedbackView />);
    await expectTranscriptIntegrityFailure();
    expect(mockDecryptField).not.toHaveBeenCalled();
  });
});
