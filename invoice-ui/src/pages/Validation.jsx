import { useCallback, useEffect, useState } from 'react'
import { ShieldAlert, Loader2 } from 'lucide-react'
import { getInvoicesNeedingValidation } from '../api/client'
import InvoiceTable from '../components/InvoiceTable'
import Pagination from '../components/Pagination'

const PAGE_SIZE = 15

export default function Validation() {
  const [invoices, setInvoices] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getInvoicesNeedingValidation({ limit: PAGE_SIZE, offset })
      setInvoices(data.invoices)
      setTotal(data.total)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [offset])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="page-fade-in">
      <div className="page-header">
        <h1>Validation</h1>
        <p>Invoices missing a key field - review and correct these manually</p>
      </div>

      {!loading && !error && total > 0 && (
        <div className="status-banner paused">
          <div className="status-banner-left">
            <ShieldAlert size={20} color="#b06a06" />
            <div>
              <p className="status-banner-title">{total} invoice{total === 1 ? '' : 's'} need review</p>
              <p className="status-banner-sub">
                Missing invoice number, vendor name, date, or amount - shown as "Not extracted" below
              </p>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        {loading && <div className="loading-state"><Loader2 size={18} className="spin" />Loading...</div>}
        {error && <div className="error-state">Couldn't load: {error}</div>}
        {!loading && !error && invoices.length === 0 && (
          <div className="empty-state">Nothing needs review - every stored invoice has all its key fields.</div>
        )}
        {!loading && !error && invoices.length > 0 && <InvoiceTable invoices={invoices} />}

        <Pagination total={total} limit={PAGE_SIZE} offset={offset} onOffsetChange={setOffset} />
      </div>
    </div>
  )
}
