import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { FileText, CheckCircle2, PauseCircle, ShieldAlert, ListChecks, Loader2 } from 'lucide-react'
import { getInvoices, getInvoicesNeedingValidation, getSyncStatus } from '../api/client'

const STATUS_POLL_MS = 15000

function formatCurrency(value) {
  if (value === null || value === undefined) return <span className="missing-value">Not extracted</span>
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(value)
}

function formatDate(value) {
  if (!value) return <span className="missing-value">Not extracted</span>
  return new Date(value).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

function formatSyncTime(iso) {
  if (!iso) return 'never'
  return new Date(iso).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

export default function Dashboard() {
  const navigate = useNavigate()

  const [invoices, setInvoices] = useState([])
  const [invoicesLoading, setInvoicesLoading] = useState(true)
  const [invoicesError, setInvoicesError] = useState(null)

  const [totalCount, setTotalCount] = useState(null)
  const [needsValidationCount, setNeedsValidationCount] = useState(null)
  const [statsLoading, setStatsLoading] = useState(true)

  const [status, setStatus] = useState(null)
  const [statusError, setStatusError] = useState(null)

  const loadInvoices = useCallback(async () => {
    setInvoicesLoading(true)
    setInvoicesError(null)
    try {
      const data = await getInvoices({ limit: 6, offset: 0 })
      setInvoices(data.invoices)
      setTotalCount(data.total)
    } catch (err) {
      setInvoicesError(err.message)
    } finally {
      setInvoicesLoading(false)
    }
  }, [])

  const loadValidationCount = useCallback(async () => {
    setStatsLoading(true)
    try {
      const data = await getInvoicesNeedingValidation({ limit: 1, offset: 0 })
      setNeedsValidationCount(data.total)
    } catch {
      setNeedsValidationCount(null)
    } finally {
      setStatsLoading(false)
    }
  }, [])

  const loadStatus = useCallback(async () => {
    try {
      const data = await getSyncStatus()
      setStatus(data)
      setStatusError(null)
    } catch (err) {
      setStatusError(err.message)
    }
  }, [])

  useEffect(() => {
    loadInvoices()
    loadValidationCount()
    loadStatus()
    const interval = setInterval(loadStatus, STATUS_POLL_MS)
    return () => clearInterval(interval)
  }, [loadInvoices, loadValidationCount, loadStatus])

  const isPaused = status?.paused === true
  const extractedCleanCount =
    totalCount !== null && needsValidationCount !== null ? totalCount - needsValidationCount : null

  return (
    <div className="page-fade-in">
      <div className="page-header">
        <h1>Dashboard</h1>
        <p>Welcome to Invoice Automation</p>
      </div>

      <div className={'status-banner' + (isPaused ? ' paused' : '')}>
        <div className="status-banner-left">
          {isPaused ? <PauseCircle size={20} color="#b06a06" /> : <CheckCircle2 size={20} color="#1c7a3e" />}
          <div>
            <p className="status-banner-title">
              {statusError ? 'Sync status unavailable' : isPaused ? 'Sync paused' : 'Background sync running'}
            </p>
            <p className="status-banner-sub">
              {statusError
                ? statusError
                : `Last sync: ${formatSyncTime(status?.last_sync)} · every ${status?.interval_minutes ?? '-'} min`}
            </p>
          </div>
        </div>
        {!statusError && !isPaused && (
          <div className="status-banner-note">
            <span className="dot-pulse" aria-hidden="true" />
            System is processing emails in background...
          </div>
        )}
      </div>

      <div className="dashboard-grid">
        <div className="card" style={{ flex: 1 }}>
          <div className="card-header">
            <h2>Recent Invoices</h2>
            <a className="link-muted" href="/invoices" onClick={(e) => { e.preventDefault(); navigate('/invoices') }}>
              View all &rsaquo;
            </a>
          </div>

          {invoicesLoading && <div className="loading-state"><Loader2 size={18} className="spin" />Loading invoices...</div>}
          {invoicesError && <div className="error-state">Couldn't load invoices: {invoicesError}</div>}
          {!invoicesLoading && !invoicesError && invoices.length === 0 && (
            <div className="empty-state">No invoices stored yet. Trigger a sync, or wait for the next background run.</div>
          )}

          {!invoicesLoading && !invoicesError && invoices.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Invoice No.</th>
                  <th>Vendor</th>
                  <th>Date</th>
                  <th>Amount</th>
                  <th>Payment</th>
                  <th>Total Net Payment</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {invoices.map((inv) => (
                  <tr key={inv.id}>
                    <td className="invoice-number-cell">{inv.invoice_number || <span className="missing-value">Not extracted</span>}</td>
                    <td>{inv.vendor_name || <span className="missing-value">Not extracted</span>}</td>
                    <td>{formatDate(inv.invoice_date)}</td>
                    <td>{formatCurrency(inv.invoice_amount)}</td>
                    <td>{formatCurrency(inv.payment_done)}</td>
                    <td>{formatCurrency(inv.total_net_payment)}</td>
                    <td>
                      <span className="source-pill">
                        <FileText size={14} />
                        {inv.source || '-'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card quick-actions">
          <div className="card-header">
            <h2>Overview</h2>
          </div>

          {statsLoading ? (
            <div className="loading-state"><Loader2 size={18} className="spin" />Loading stats...</div>
          ) : (
            <div className="stats-list">
              <div className="stat-row" onClick={() => navigate('/invoices')}>
                <span className="stat-icon icon-blue"><ListChecks size={16} /></span>
                <span>
                  <span className="stat-value">{totalCount ?? '-'}</span>
                  <span className="stat-label">Total invoices</span>
                </span>
              </div>

              <div className="stat-row" onClick={() => navigate('/validation')}>
                <span className="stat-icon icon-red"><ShieldAlert size={16} /></span>
                <span>
                  <span className="stat-value">{needsValidationCount ?? '-'}</span>
                  <span className="stat-label">Need validation</span>
                </span>
              </div>

              <div className="stat-row">
                <span className="stat-icon icon-green"><CheckCircle2 size={16} /></span>
                <span>
                  <span className="stat-value">{extractedCleanCount ?? '-'}</span>
                  <span className="stat-label">Fully extracted</span>
                </span>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}