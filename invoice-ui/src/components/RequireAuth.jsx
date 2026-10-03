import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { getMe } from '../api/client'

// Wraps the protected routes. Checks the session once on mount (does the
// httpOnly cookie, if any, still correspond to a valid, non-expired
// session on the server?) before rendering anything behind it - this is
// what stops someone from seeing the dashboard for a flash before being
// bounced to /login, and what makes deep links (e.g. sharing /invoices)
// safe.
export default function RequireAuth({ children }) {
  const [status, setStatus] = useState('checking') // 'checking' | 'authenticated' | 'unauthenticated'

  useEffect(() => {
    getMe()
      .then(() => setStatus('authenticated'))
      .catch(() => setStatus('unauthenticated'))
  }, [])

  if (status === 'checking') {
    return <div className="auth-checking">Checking session...</div>
  }

  if (status === 'unauthenticated') {
    return <Navigate to="/login" replace />
  }

  return children
}
