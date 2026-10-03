import { ChevronLeft, ChevronRight } from 'lucide-react'

// `offset`/`limit` match the backend's pagination params directly, so the
// parent page can pass the result straight to the API client with no
// translation needed.
export default function Pagination({ total, limit, offset, onOffsetChange }) {
  if (total === 0) return null

  const start = offset + 1
  const end = Math.min(offset + limit, total)
  const canGoPrev = offset > 0
  const canGoNext = end < total

  return (
    <div className="pagination">
      <span className="pagination-summary">
        Showing {start}-{end} of {total}
      </span>
      <div className="pagination-buttons">
        <button
          className="pagination-btn"
          disabled={!canGoPrev}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          <ChevronLeft size={16} />
        </button>
        <button
          className="pagination-btn"
          disabled={!canGoNext}
          onClick={() => onOffsetChange(offset + limit)}
        >
          <ChevronRight size={16} />
        </button>
      </div>
    </div>
  )
}
