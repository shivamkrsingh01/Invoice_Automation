import { useCallback, useEffect, useState } from 'react'
import { Search, Loader2 } from 'lucide-react'
import { getInvoices } from '../api/client'
import InvoiceTable from '../components/InvoiceTable'
import Pagination from '../components/Pagination'

const PAGE_SIZE = 15
const SEARCH_DEBOUNCE_MS = 400

export default function Invoices() {
  const [invoices, setInvoices] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')

  // Debounce: wait until the user pauses typing before actually
  // re-querying, so every keystroke doesn't fire a request.
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(searchInput)
      setOffset(0) // a new search always starts back at page 1
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [searchInput])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getInvoices({ limit: PAGE_SIZE, offset, search })
      setInvoices(data.invoices)
      setTotal(data.total)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [offset, search])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="page-fade-in">
      <div className="page-header">
        <h1>Invoices</h1>
        <p>All stored invoices</p>
      </div>

      <div className="card">
        <div className="toolbar">
          <div className="search-box">
            <Search size={16} />
            <input
              type="text"
              placeholder="Search by invoice number or vendor..."
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
          </div>
        </div>

        {loading && <div className="loading-state"><Loader2 size={18} className="spin" />Loading invoices...</div>}
        {error && <div className="error-state">Couldn't load invoices: {error}</div>}
        {!loading && !error && invoices.length === 0 && (
          <div className="empty-state">
            {search ? `No invoices match "${search}".` : 'No invoices stored yet.'}
          </div>
        )}
        {!loading && !error && invoices.length > 0 && <InvoiceTable invoices={invoices} />}

        <Pagination total={total} limit={PAGE_SIZE} offset={offset} onOffsetChange={setOffset} />
      </div>
    </div>
  )
}
