import { FileText } from 'lucide-react'
import { formatCurrency, formatDate } from '../utils/format'

function Missing() {
  return <span className="missing-value">Not extracted</span>
}

// Used by both the Invoices page (all invoices) and the Validation page
// (only invoices missing a field) - same columns either way. A missing
// field naturally shows as "Not extracted" in both places, which is what
// makes the Validation page's filtering visible at a glance.
export default function InvoiceTable({ invoices }) {
  return (
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
            <td className="invoice-number-cell">{inv.invoice_number || <Missing />}</td>
            <td>{inv.vendor_name || <Missing />}</td>
            <td>{formatDate(inv.invoice_date) ?? <Missing />}</td>
            <td>{formatCurrency(inv.invoice_amount) ?? <Missing />}</td>
            <td>{formatCurrency(inv.payment_done) ?? <Missing />}</td>
            <td>{formatCurrency(inv.total_net_payment) ?? <Missing />}</td>
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
  )
}
