import { useState } from 'react'
import { FileText, Download, Calendar, Loader2, BarChart3 } from 'lucide-react'
import { generateReportData, downloadPdf, downloadExcel } from '../api/client'

export default function Reports() {
  const [period, setPeriod] = useState('weekly')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [loading, setLoading] = useState(false)
  const [downloading, setDownloading] = useState(null) // 'pdf' | 'excel' | null
  const [reportData, setReportData] = useState(null)
  const [error, setError] = useState(null)

  const handleGenerate = async () => {
    setLoading(true)
    setError(null)
    setReportData(null)

    try {
      const data = await generateReportData({
        period,
        start_date: startDate || undefined,
        end_date: endDate || undefined
      })
      setReportData(data)
    } catch (err) {
      setError(err.message || 'Failed to generate report')
    } finally {
      setLoading(false)
    }
  }

  const handleDownload = async (format) => {
    setDownloading(format)
    try {
      if (format === 'pdf') {
        await downloadPdf({
          period,
          start_date: startDate || undefined,
          end_date: endDate || undefined
        })
      } else if (format === 'excel') {
        await downloadExcel({
          period,
          start_date: startDate || undefined,
          end_date: endDate || undefined
        })
      }
    } catch (err) {
      setError(`Failed to download ${format}: ${err.message}`)
    } finally {
      setDownloading(null)
    }
  }

  const formatDate = (dateStr) => {
    if (!dateStr) return '-'
    return new Date(dateStr).toLocaleDateString('en-IN', {
      day: '2-digit',
      month: 'short',
      year: 'numeric'
    })
  }

  const formatCurrency = (value) => {
    if (value === null || value === undefined) return '₹0.00'
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 2
    }).format(value)
  }

  return (
    <div className="page-fade-in">
      <div className="page-header">
        <div>
          <h1>Summary Reports</h1>
          <p>Generate and download invoice summary reports</p>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h2>Report Filters</h2>
        </div>

        <div className="report-filters">
          <div className="filter-group">
            <label>Report Period</label>
            <select
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
              className="filter-select"
            >
              <option value="weekly">Weekly (Last 7 days)</option>
              <option value="monthly">Monthly (Last 30 days)</option>
              <option value="custom">Custom Date Range</option>
            </select>
          </div>

          {period === 'custom' && (
            <>
              <div className="filter-group">
                <label>Start Date</label>
                <input
                  type="date"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  className="filter-input"
                />
              </div>
              <div className="filter-group">
                <label>End Date</label>
                <input
                  type="date"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  className="filter-input"
                />
              </div>
            </>
          )}

          <div className="filter-actions">
            <button
              className="btn-primary"
              onClick={handleGenerate}
              disabled={loading}
            >
              {loading ? <Loader2 size={16} className="spin" /> : <BarChart3 size={16} />}
              {loading ? 'Generating...' : 'Generate Report'}
            </button>
          </div>
        </div>

        {error && <div className="error-state">{error}</div>}

        {reportData && (
          <>
            <div className="report-actions">
              <button
                className="btn-download"
                onClick={() => handleDownload('pdf')}
                disabled={downloading !== null}
              >
                {downloading === 'pdf' ? <Loader2 size={16} className="spin" /> : <FileText size={16} />}
                {downloading === 'pdf' ? 'Generating...' : 'Download PDF'}
              </button>
              <button
                className="btn-download"
                onClick={() => handleDownload('excel')}
                disabled={downloading !== null}
              >
                {downloading === 'excel' ? <Loader2 size={16} className="spin" /> : <FileText size={16} />}
                {downloading === 'excel' ? 'Generating...' : 'Download Excel'}
              </button>
            </div>

            <div className="report-summary">
              <h3>Executive Summary</h3>
              <div className="summary-grid">
                <div className="summary-card">
                  <span className="summary-label">Total Invoices</span>
                  <span className="summary-value">{reportData.summary.total_invoices.toLocaleString()}</span>
                </div>
                <div className="summary-card">
                  <span className="summary-label">Total Invoice Amount</span>
                  <span className="summary-value">{formatCurrency(reportData.summary.total_invoice_amount)}</span>
                </div>
                <div className="summary-card">
                  <span className="summary-label">Total Payment Amount</span>
                  <span className="summary-value">{formatCurrency(reportData.summary.total_payment_amount)}</span>
                </div>
                <div className="summary-card">
                  <span className="summary-label">Total Net Payment</span>
                  <span className="summary-value">{formatCurrency(reportData.summary.total_net_payment_amount)}</span>
                </div>
                <div className="summary-card">
                  <span className="summary-label">Unique Vendors</span>
                  <span className="summary-value">{reportData.summary.unique_vendors}</span>
                </div>
              </div>
            </div>

            {reportData.missing_fields && Object.keys(reportData.missing_fields).length > 0 && (
              <div className="report-section">
                <h3>Missing Fields Analysis</h3>
                <table className="report-table">
                  <thead>
                    <tr>
                      <th>Field</th>
                      <th>Count</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(reportData.missing_fields).map(([field, count]) => (
                      <tr key={field}>
                        <td>{field.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}</td>
                        <td>{count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {reportData.vendor_stats && reportData.vendor_stats.length > 0 && (
              <div className="report-section">
                <h3>Top Vendors by Invoice Count</h3>
                <table className="report-table">
                  <thead>
                    <tr>
                      <th>Vendor</th>
                      <th>Invoices</th>
                      <th>Total Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reportData.vendor_stats.map((vendor, idx) => (
                      <tr key={idx}>
                        <td>{vendor.vendor_name}</td>
                        <td>{vendor.invoice_count}</td>
                        <td>{formatCurrency(vendor.total_amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {reportData.trends && reportData.trends.daily && reportData.trends.daily.length > 0 && (
              <div className="report-section">
                <h3>Daily Invoice Trends</h3>
                <table className="report-table">
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th>Invoices</th>
                      <th>Total Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reportData.trends.daily.map((trend, idx) => (
                      <tr key={idx}>
                        <td>{formatDate(trend.date)}</td>
                        <td>{trend.invoice_count}</td>
                        <td>{formatCurrency(trend.total_amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <div className="report-section">
              <h3>Detailed Invoice Records ({reportData.invoices.length.toLocaleString()} invoices)</h3>
              {reportData.invoices.length === 0 ? (
                <div className="empty-state">No invoices found in this period.</div>
              ) : (
                <div className="invoice-records-note">
                  Note: Showing summary above. Download Excel for complete detailed records.
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
