import { getSeverityMeta, getTypeLabel } from '../utils/notificationMeta'
import { formatDateTime } from '../utils/format'

// One notification row. `compact` = the clickable row inside the bell
// dropdown (clicking marks it read); otherwise the full row used on the
// Notifications page, with an explicit "Mark as read" button.
export default function NotificationItem({ n, onMarkRead, compact = false }) {
  const { icon: Icon, label } = getSeverityMeta(n.severity)
  const className = `notif-item sev-${n.severity}` + (n.is_read ? '' : ' unread') + (compact ? ' compact' : '')

  const body = (
    <>
      <span className="notif-icon" title={label}>
        <Icon size={18} />
      </span>
      <span className="notif-content">
        <span className="notif-title-row">
          <span className="notif-title">{n.title}</span>
          {!n.is_read && <span className="notif-dot" aria-label="Unread" />}
        </span>
        <span className="notif-message">{n.message}</span>
        <span className="notif-meta">
          <span className={`notif-tag sev-${n.severity}`}>{label}</span>
          {!compact && <span className="notif-type">{getTypeLabel(n.notification_type)}</span>}
          <span>{formatDateTime(n.created_at)}</span>
          {!compact && <span>{n.is_read ? 'Read' : 'Unread'}</span>}
        </span>
      </span>
    </>
  )

  if (compact) {
    return (
      <button type="button" className={className} onClick={() => onMarkRead(n)}>
        {body}
      </button>
    )
  }

  return (
    <div className={className}>
      {body}
      {!n.is_read && (
        <button type="button" className="notif-btn" onClick={() => onMarkRead(n)}>
          Mark as read
        </button>
      )}
    </div>
  )
}
