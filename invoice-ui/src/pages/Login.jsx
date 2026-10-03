import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { FileText, LogIn, Loader2 } from 'lucide-react'
import { login } from '../api/client'

export default function Login() {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      await login(username, password)
      navigate('/', { replace: true })
    } catch (err) {
      // client.js's request() throws "401: Incorrect username or
      // password" - strip the leading status code for a cleaner message.
      setError(err.message.replace(/^\d+:\s*/, ''))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-brand">
          <FileText size={26} className="brand-icon" />
          <span>Invoice Automation</span>
        </div>
        <h1>Admin Login</h1>

        {error && <div className="login-error">{error}</div>}

        <label className="login-field">
          Username
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            required
            disabled={loading}
          />
        </label>

        <label className="login-field">
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            disabled={loading}
          />
        </label>

        <button type="submit" className="login-submit-btn" disabled={loading}>
          {loading ? <Loader2 size={16} className="spin" /> : <LogIn size={16} />}
          {loading ? 'Signing in...' : 'Sign In'}
        </button>
      </form>
    </div>
  )
}
