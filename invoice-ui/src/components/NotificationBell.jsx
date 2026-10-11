import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Bell, Loader2 } from 'lucide-react'
import {
  getNotifications,
  getUnreadCount,
  markNotificationRead,
  markAllNotificationsRead,
} from '../api/client'
import { announceNotificationsChanged } from '../utils/notificationMeta'
import NotificationItem from './NotificationItem'

const POLL_MS = 30000 // simple polling - no extra server infrastructure needed
const RECENT_LIMIT = 8

export default function NotificationBell() {
  const [unread, setUnread] = useState(0)
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const wrapRef = useRef(null)

  // The badge is a convenience: if this fails we keep the last known
  // number and stay quiet rather than disturbing the rest of the page.
  const refreshCount = useCallback(async () => {
    try {
      const data = await getUnreadCount()
      setUnread(data.unread)
    } catch {
      /* keep last known count */
    }
  }, [])

  const loadRecent = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getNotifications({ limit: RECENT_LIMIT })
      setItems(data.items)
      setUnread(data.unread)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  // Poll the unread count (skipped while the tab is hidden) and refresh
  // when the tab becomes visible again or another view changed something.
  useEffect(() => {
    refreshCount()
    const timer = setInterval(() => {
      if (!document.hidden) refreshCount()
    }, POLL_MS)
    const onVisible = () => {
      if (!document.hidden) refreshCount()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('notifications-changed', refreshCount)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('notifications-changed', refreshCount)
    }
  }, [refreshCount])

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return undefined
    const onClick = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false)
    }
    const onKey = (e) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  function toggle() {
    const next = !open
    setOpen(next)
    if (next) loadRecent()
  }

  async function handleMarkRead(n) {
    if (n.is_read) return
    try {
      const res = await markNotificationRead(n.id)
      setItems((prev) => prev.map((i) => (i.id === n.id ? { ...i, is_read: true } : i)))
      setUnread(res.unread)
      announceNotificationsChanged()
    } catch (err) {
      setError(err.message)
    }
  }

  async function handleMarkAll() {
    try {
      await markAllNotificationsRead()
      setItems((prev) => prev.map((i) => ({ ...i, is_read: true })))
      setUnread(0)
      announceNotificationsChanged()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="notif-wrap" ref={wrapRef}>
      <button
        type="button"
        className="notif-bell-btn"
        onClick={toggle}
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
        aria-haspopup="true"
        aria-expanded={open}
        title="Notifications"
      >
        <Bell size={20} />
        {unread > 0 && <span className="notif-badge">{unread > 99 ? '99+' : unread}</span>}
      </button>

      {open && (
        <div className="notif-dropdown" role="dialog" aria-label="Recent notifications">
          <div className="notif-dropdown-header">
            <strong>Notifications</strong>
            <button
              type="button"
              className="notif-link-btn"
              onClick={handleMarkAll}
              disabled={unread === 0}
            >
              Mark all as read
            </button>
          </div>

          <div className="notif-dropdown-body">
            {loading && items.length === 0 && (
              <div className="loading-state"><Loader2 size={18} className="spin" />Loading...</div>
            )}
            {error && <div className="error-state">Couldn't load: {error}</div>}
            {!loading && !error && items.length === 0 && (
              <div className="empty-state">No notifications yet.</div>
            )}
            {items.map((n) => (
              <NotificationItem key={n.id} n={n} onMarkRead={handleMarkRead} compact />
            ))}
          </div>

          <Link to="/notifications" className="notif-dropdown-footer" onClick={() => setOpen(false)}>
            View all notifications
          </Link>
        </div>
      )}
    </div>
  )
}
