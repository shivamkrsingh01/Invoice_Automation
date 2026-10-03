// Thin wrapper around fetch(). Every call uses a relative "/api/..." path -
// see vite.config.js for why that works in both dev and production without
// any environment-specific base URL.

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    // Required so the browser sends/receives the httpOnly session cookie -
    // without this, every request would look logged-out even right after
    // a successful login.
    credentials: 'include',
    ...options,
  })

  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    // Session missing or expired - bounce to the login page instead of
    // letting every page show its own raw "401" error individually.
    // (The /api/auth/* endpoints are excluded so the login form itself
    // can show a proper "wrong password" message instead of redirecting.)
    window.location.href = '/login'
    return new Promise(() => {}) // navigation is happening; never resolve
  }

  if (!res.ok) {
    // FastAPI's HTTPException responses have a "detail" field - surface
    // that when present, since it's usually the actual useful message.
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // response wasn't JSON - fall back to statusText above
    }
    throw new Error(`${res.status}: ${detail}`)
  }

  // 204/empty responses have no body to parse
  if (res.status === 204) return null
  return res.json()
}

export function login(username, password) {
  return request('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

export function logout() {
  return request('/api/auth/logout', { method: 'POST' })
}

export function getMe() {
  return request('/api/auth/me')
}

export function getInvoices({ limit = 25, offset = 0, search = '' } = {}) {
  const params = new URLSearchParams({ limit, offset })
  if (search) params.set('search', search)
  return request(`/api/invoices?${params}`)
}

export function getInvoicesNeedingValidation({ limit = 25, offset = 0 } = {}) {
  const params = new URLSearchParams({ limit, offset })
  return request(`/api/invoices/validation?${params}`)
}

export function getSyncStatus() {
  return request('/api/sync/status')
}

export function triggerSync(limit = 10) {
  return request(`/api/process?limit=${limit}`, { method: 'POST' })
}

export function stopSync() {
  return request('/api/sync/stop', { method: 'POST' })
}

export function resumeSync() {
  return request('/api/sync/resume', { method: 'POST' })
}

export function askQuestion(question) {
  return request('/api/qa', {
    method: 'POST',
    body: JSON.stringify({ question }),
  })
}