// Shared formatting helpers. Both return `null` for a missing value rather
// than a placeholder string - callers decide how to display "missing"
// (see the <Missing /> element in InvoiceTable.jsx), so this file stays
// pure formatting logic with no UI opinions.

export function formatCurrency(value) {
  if (value === null || value === undefined) return null
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 2,
  }).format(value)
}

export function formatDate(value) {
  if (!value) return null
  return new Date(value).toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  })
}

export function formatDateTime(value) {
  if (!value) return null
  return new Date(value).toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
