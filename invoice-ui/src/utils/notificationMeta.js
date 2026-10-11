// Shared look-up tables for notifications, used by the navbar bell and the
// Notifications page so both always label and style them the same way.
import { CheckCircle2, AlertTriangle, XCircle, Info } from 'lucide-react'

export const SEVERITY_META = {
  success: { label: 'Success', icon: CheckCircle2 },
  warning: { label: 'Warning', icon: AlertTriangle },
  error: { label: 'Error', icon: XCircle },
  system: { label: 'System', icon: Info },
}

// Must match NOTIFICATION_TYPES in app/services/notification_service.py
export const TYPE_LABELS = {
  invoice_processed: 'Invoice processed',
  invoice_not_stored: 'Invoice not stored',
  duplicate_document: 'Duplicate document',
  processing_error: 'Processing error',
  run_failed: 'Run failed',
  run_recovered: 'Run recovered',
}

// Unknown severities fall back to "system" so a new backend value can
// never crash the UI.
export function getSeverityMeta(severity) {
  return SEVERITY_META[severity] || SEVERITY_META.system
}

export function getTypeLabel(type) {
  return TYPE_LABELS[type] || type
}

// Tells every mounted notification view (bell, history page) to reload.
export function announceNotificationsChanged() {
  window.dispatchEvent(new Event('notifications-changed'))
}
