import { useCallback, useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { FileText, Home, FileCheck2, MessageCircle, User, Play, Square, LogOut } from 'lucide-react'
import { getSyncStatus, stopSync, resumeSync, logout } from './api/client'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: Home, end: true },
  { to: '/invoices', label: 'Invoices', icon: FileText },
  { to: '/validation', label: 'Validation', icon: FileCheck2 },
  { to: '/qa', label: 'Q&A', icon: MessageCircle },
]

export default function App() {
  const navigate = useNavigate()
  const [paused, setPaused] = useState(null) // null = not loaded yet
  const [toggling, setToggling] = useState(false)

  const loadStatus = useCallback(async () => {
    try {
      const data = await getSyncStatus()
      setPaused(data.paused)
    } catch {
      // Sidebar toggle just stays in its last known state if this fails -
      // the Dashboard's own banner already surfaces the error clearly.
    }
  }, [])

  useEffect(() => {
    loadStatus()
  }, [loadStatus])

  async function handleToggleSync() {
    setToggling(true)
    try {
      if (paused) {
        await resumeSync()
      } else {
        await stopSync()
      }
      await loadStatus()
    } finally {
      setToggling(false)
    }
  }

  async function handleLogout() {
    try {
      await logout()
    } finally {
      // Navigate regardless of whether the request succeeded - the cookie
      // is either already gone (server confirmed) or about to be treated
      // as invalid anyway, so there's nothing gained by staying on this
      // page if the logout call happened to fail on a flaky connection.
      navigate('/login', { replace: true })
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <FileText size={22} className="brand-icon" />
          <span>Invoice Automation</span>
        </div>
        <nav className="sidebar-nav">
          {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}
            >
              <Icon size={18} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        {/* margin-top: auto (see CSS) pins this to the bottom of the
            sidebar regardless of how much nav content is above it. */}
        <div className="sidebar-footer">
          <div className="sidebar-divider" />

          <div className="sidebar-user-row">
            <User size={16} />
            <span>Admin</span>
          </div>

          <button
            className={'sidebar-sync-btn' + (paused ? ' is-paused' : ' is-running')}
            onClick={handleToggleSync}
            disabled={toggling || paused === null}
            title={paused ? 'Resume background sync' : 'Pause background sync'}
          >
            {paused ? <Play size={15} /> : <Square size={15} />}
            <span>{paused ? 'Start Sync' : 'Stop Sync'}</span>
          </button>

          <button className="sidebar-logout-btn" onClick={handleLogout} title="Log out">
            <LogOut size={15} />
            <span>Logout</span>
          </button>
        </div>
      </aside>

      <div className="main-column">
        <main className="page-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
