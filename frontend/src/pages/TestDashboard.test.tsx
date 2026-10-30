import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TestDashboard } from './TestDashboard'
import { ThemeProvider } from '../theme'

function renderDashboard() {
  return render(
    <MemoryRouter>
      <ThemeProvider>
        <TestDashboard />
      </ThemeProvider>
    </MemoryRouter>
  )
}

describe('TestDashboard', () => {
  beforeEach(() => {
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => store.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => {
        store.set(key, value)
      }),
      removeItem: vi.fn((key: string) => {
        store.delete(key)
      }),
      clear: vi.fn(() => {
        store.clear()
      }),
    })
    localStorage.setItem('enclave-theme', 'light')
    localStorage.setItem('enclave_admin_pubkey', 'admin-pubkey')
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    document.documentElement.classList.remove('dark')
  })

  it.each([
    ['partial_failure', 'Account removed. Conversation and memory cleanup is incomplete.'],
    ['succeeded', 'User deleted. Covered Conversation and memory cleanup is complete.'],
  ])('shows %s after User deletion', async (status, message) => {
    const user = userEvent.setup()
    let removed = false
    vi.stubGlobal('fetch', vi.fn((url: string, options?: RequestInit) => {
      if (options?.method === 'DELETE') {
        removed = true
        return Promise.resolve(Response.json({ success: true, deletion: { status } }))
      }
      if (url.includes('/admin/users')) {
        return Promise.resolve(Response.json({ users: removed ? [] : [{ id: 42, pubkey: 'synthetic-user', approved: true, fields: {} }] }))
      }
      return Promise.resolve(Response.json({ settings: {} }))
    }))
    renderDashboard()
    await user.type(screen.getByPlaceholderText('Paste admin session token...'), 'synthetic-token')
    await user.click(screen.getByRole('button', { name: /14.User Management/ }))
    await user.click(screen.getByRole('button', { name: 'Fetch All Users' }))
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(message))
    if (status === 'partial_failure') expect(screen.getByRole('status')).toHaveTextContent('Retry it in Admin Deployment')
  })

  it('displays a failed User deletion and keeps the User visible', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn((url: string, options?: RequestInit) => {
      if (options?.method === 'DELETE') return Promise.resolve(Response.json({ detail: 'Forbidden' }, { status: 403 }))
      if (url.includes('/admin/users')) return Promise.resolve(Response.json({ users: [{ id: 42, pubkey: 'synthetic-user', approved: true, fields: {} }] }))
      return Promise.resolve(Response.json({ settings: {} }))
    }))
    renderDashboard()
    await user.type(screen.getByPlaceholderText('Paste admin session token...'), 'synthetic-token')
    await user.click(screen.getByRole('button', { name: /14.User Management/ }))
    await user.click(screen.getByRole('button', { name: 'Fetch All Users' }))
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('User deletion failed.'))
    expect(screen.getByRole('cell', { name: '42' })).toBeInTheDocument()
  })

  it('checks health and renders the returned admin status output', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn((url: string) => {
      if (url.includes('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }))
      }
      return Promise.resolve(Response.json({
        neo4j: 'ok',
        qdrant: 'ok',
      }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderDashboard()

    const endpointNotes = screen.getAllByRole('note', { name: 'Endpoint guidance' })
    expect(endpointNotes[0]).toHaveTextContent('GET /health')

    await user.click(screen.getByRole('button', { name: 'Check Health' }))

    expect(fetchMock).toHaveBeenCalledWith('/api/health')
    await waitFor(() => {
      expect(screen.getByRole('region', { name: 'Health response output' })).toHaveTextContent('"neo4j": "ok"')
    })
    expect(screen.getByText(/"qdrant": "ok"/)).toBeInTheDocument()
  })

  it('renders follow-up questions as ordinary answer text without a side panel', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn((url: string) => {
      if (url.includes('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }))
      }
      if (url.includes('/query')) {
        return Promise.resolve(Response.json({
          answer: 'Which location should I search near?',
          session_id: 'session-1234',
          sources: [],
          graph_context: {},
          clarifying_questions: ['Which location should I search near?'],
          search_term: null,
          context_used: '',
          temperature: 0.2,
        }))
      }
      return Promise.resolve(Response.json({}))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderDashboard()

    await user.type(
      screen.getByPlaceholderText('Ask a question... (try: When was the UDHR adopted?)'),
      'Find nearby support',
    )
    await user.click(screen.getByRole('button', { name: 'Query' }))

    expect(await screen.findByText('Which location should I search near?')).toBeInTheDocument()
    expect(screen.queryByText('Clarifying Questions:')).not.toBeInTheDocument()
  })

  it('toggles migrated admin dashboard sections through their accessible header', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn())

    renderDashboard()

    const sectionHeader = screen.getByRole('button', { name: /10\..*Authentication Testing/ })
    const sectionContent = document.getElementById(sectionHeader.getAttribute('aria-controls') ?? '')
    expect(sectionHeader).toHaveAttribute('aria-expanded', 'false')
    expect(sectionContent).toHaveAttribute('aria-hidden', 'true')

    await user.click(sectionHeader)

    expect(sectionHeader).toHaveAttribute('aria-expanded', 'true')
    expect(sectionContent).toHaveAttribute('aria-hidden', 'false')
    expect(within(sectionContent as HTMLElement).getByText('Magic Link Authentication')).toBeInTheDocument()

    await user.click(sectionHeader)

    expect(sectionHeader).toHaveAttribute('aria-expanded', 'false')
    expect(sectionContent).toHaveAttribute('aria-hidden', 'true')
  })

  it('keeps the dashboard theme toggle reachable by accessible name', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn())

    renderDashboard()

    expect(document.documentElement.classList.contains('dark')).toBe(false)

    await user.click(screen.getByRole('button', { name: 'Toggle theme' }))

    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(screen.getByRole('combobox', { name: 'Theme preference' })).toHaveValue('dark')
  })

  it('keeps migrated dashboard selects labelled and wired to their controls', async () => {
    const user = userEvent.setup()
    let rateLimitCalls = 0
    const fetchMock = vi.fn((url: string) => {
      if (url.includes('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }))
      }
      rateLimitCalls += 1
      return Promise.resolve(new Response(null, { status: rateLimitCalls === 2 ? 429 : 200 }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderDashboard()

    await user.click(screen.getByRole('button', { name: /16\..*Rate Limiting Test/ }))

    const testType = screen.getByRole('combobox', { name: 'Rate limit test type' })
    expect(testType).toHaveValue('magic_link')
    expect(screen.getByRole('button', { name: 'Send 6 Rapid Requests' })).toBeInTheDocument()

    await user.selectOptions(testType, 'admin_auth')

    expect(testType).toHaveValue('admin_auth')
    expect(screen.getByRole('button', { name: 'Send 11 Rapid Requests' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Send 11 Rapid Requests' }))

    await waitFor(() => {
    expect(screen.getByRole('group', { name: 'Successful requests' })).toHaveTextContent('10')
    })
    expect(screen.getByRole('group', { name: 'Blocked requests' })).toHaveTextContent('1')
    expect(fetchMock.mock.calls.filter(([url]) => !url.includes('/settings/public'))).toHaveLength(11)
  })
})
