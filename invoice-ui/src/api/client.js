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

// ---------- Notifications ----------

export function getNotifications({ limit = 20, offset = 0, severity = '', type = '', unreadOnly = false } = {}) {
  const params = new URLSearchParams({ limit, offset })
  if (severity) params.set('severity', severity)
  if (type) params.set('type', type)
  if (unreadOnly) params.set('unread_only', 'true')
  return request(`/api/notifications?${params}`)
}

export function getUnreadCount() {
  return request('/api/notifications/unread-count')
}

export function markNotificationRead(id) {
  return request(`/api/notifications/${id}/read`, { method: 'POST' })
}

export function markAllNotificationsRead() {
  return request('/api/notifications/read-all', { method: 'POST' })
}

// ---------- Reports ----------

export function generateReportData({ period, start_date, end_date }) {
  return request('/api/reports/data', {
    method: 'POST',
    body: JSON.stringify({ period, start_date, end_date }),
  })
}

export function downloadPdf({ period, start_date, end_date }) {
  return fetch('/api/reports/pdf', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ period, start_date, end_date }),
  }).then(res => {
    if (!res.ok) throw new Error(`PDF download failed: ${res.statusText}`)
    return res.blob()
  }).then(blob => {
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `invoice_report_${period}.pdf`
    document.body.appendChild(a)
    a.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(a)
  })
}

export function downloadExcel({ period, start_date, end_date }) {
  return fetch('/api/reports/excel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ period, start_date, end_date }),
  }).then(res => {
    if (!res.ok) throw new Error(`Excel download failed: ${res.statusText}`)
    return res.blob()
  }).then(blob => {
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `invoice_report_${period}.xlsx`
    document.body.appendChild(a)
    a.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(a)
  })
}
