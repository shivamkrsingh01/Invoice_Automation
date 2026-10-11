import { useCallback, useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import {
  getNotifications,
  markNotificationRead,
  markAllNotificationsRead,
} from '../api/client'
import NotificationItem from '../components/NotificationItem'
import Pagination from '../components/Pagination'
import { SEVERITY_META, TYPE_LABELS, announceNotificationsChanged } from '../utils/notificationMeta'

const PAGE_SIZE = 20

export default function Notifications() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [unread, setUnread] = useState(0)
  const [offset, setOffset] = useState(0)
  const [severity, setSeverity] = useState('')
  const [type, setType] = useState('')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [actionError, setActionError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getNotifications({
        limit: PAGE_SIZE, offset, severity, type, unreadOnly,
      })
      // E.g. the last item on the last page was just removed by a filter.
      if (data.total > 0 && offset >= data.total) {
        setOffset(Math.floor((data.total - 1) / PAGE_SIZE) * PAGE_SIZE)
        return
      }
      setItems(data.items)
      setTotal(data.total)
      setUnread(data.unread)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [offset, severity, type, unreadOnly])

  useEffect(() => {
    load()
  }, [load])

  // The bell dropdown can change things too - stay in sync with it.
  useEffect(() => {
    window.addEventListener('notifications-changed', load)
    return () => window.removeEventListener('notifications-changed', load)
  }, [load])

  function changeFilter(setter) {
    return (value) => {
      setter(value)
      setOffset(0)
    }
  }

  async function handleMarkRead(n) {
    setActionError(null)
    try {
      await markNotificationRead(n.id)
      announceNotificationsChanged() // reloads this page and updates the bell
    } catch (err) {
      setActionError(err.message)
    }
  }

  async function handleMarkAll() {
    setActionError(null)
    try {
      await markAllNotificationsRead()
      announceNotificationsChanged()
    } catch (err) {
      setActionError(err.message)
    }
  }

  const filtered = Boolean(severity || type || unreadOnly)

  return (
    <div className="page-fade-in">
      <div className="page-header">
        <h1>Notifications</h1>
        <p>Recent invoice-processing events - newest first. Cleared automatically at the start of each month.</p>
      </div>

      <div className="card">
        <div className="notif-toolbar">
          <select
            className="notif-select"
            value={severity}
            onChange={(e) => changeFilter(setSeverity)(e.target.value)}
            aria-label="Filter by severity"
          >
            <option value="">All severities</option>
            {Object.entries(SEVERITY_META).map(([value, { label }]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>

          <select
            className="notif-select"
            value={type}
            onChange={(e) => changeFilter(setType)(e.target.value)}
            aria-label="Filter by type"
          >
            <option value="">All types</option>
            {Object.entries(TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>

          <label className="notif-check">
            <input
              type="checkbox"
              checked={unreadOnly}
              onChange={(e) => changeFilter(setUnreadOnly)(e.target.checked)}
            />
            Unread only
          </label>

          <span className="notif-toolbar-spacer" />

          <button type="button" className="notif-btn" onClick={load} disabled={loading} title="Reload">
            <RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh
          </button>
          <button type="button" className="notif-btn primary" onClick={handleMarkAll} disabled={unread === 0}>
            Mark all as read{unread > 0 ? ` (${unread})` : ''}
          </button>
        </div>

        {actionError && <div className="error-state">Action failed: {actionError}</div>}
        {loading && items.length === 0 && (
          <div className="loading-state"><Loader2 size={18} className="spin" />Loading...</div>
        )}
        {error && (
          <div className="error-state">
            Couldn't load: {error}
            <button type="button" className="notif-btn" onClick={load}>Try again</button>
          </div>
        )}
        {!loading && !error && items.length === 0 && (
          <div className="empty-state">
            {filtered ? 'No notifications match these filters.' : 'No notifications yet - they appear here as invoices are processed.'}
          </div>
        )}

        {!error && items.length > 0 && (
          <div className="notif-list">
            {items.map((n) => (
              <NotificationItem key={n.id} n={n} onMarkRead={handleMarkRead} />
            ))}
          </div>
        )}

        {!error && (
          <Pagination total={total} limit={PAGE_SIZE} offset={offset} onOffsetChange={setOffset} />
        )}
      </div>
    </div>
  )
}
