"""Report generation service for invoice analytics.

Generates summary reports with executive metrics, detailed records,
and downloadable exports (PDF, Excel, CSV) based on date ranges.
"""

from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from io import BytesIO
import psycopg
from psycopg_pool import ConnectionPool
from app.config.settings import settings
from app.utils.logging_config import logger
from collections import defaultdict


class ReportService:
    """Service for generating invoice reports with date filtering and exports."""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def get_report_data(
        self,
        start_date: date,
        end_date: date,
        limit: int = 10000
    ) -> Dict[str, Any]:
        """Fetch all report data for the given date range.

        Returns:
            Dict with executive summary, detailed records, and analytics.
        """
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    # Executive summary metrics
                    summary = self._get_summary_metrics(cur, start_date, end_date)
                    
                    # Detailed invoice records
                    invoices = self._get_invoice_records(cur, start_date, end_date, limit)
                    
                    # Missing field analysis
                    missing_fields = self._get_missing_field_analysis(cur, start_date, end_date)
                    
                    # Vendor distribution
                    vendor_stats = self._get_vendor_distribution(cur, start_date, end_date)
                    
                    # Daily/weekly trends
                    trends = self._get_date_trends(cur, start_date, end_date)

            return {
                "summary": summary,
                "invoices": invoices,
                "missing_fields": missing_fields,
                "vendor_stats": vendor_stats,
                "trends": trends,
                "date_range": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat()
                }
            }
        except psycopg.Error as e:
            logger.error(f"Failed to generate report data: {str(e)}")
            raise

    def _get_summary_metrics(
        self,
        cur,
        start_date: date,
        end_date: date
    ) -> Dict[str, Any]:
        """Calculate executive summary metrics."""
        # Convert dates to datetime for proper timestamp comparison
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())

        cur.execute(
            """
            SELECT
                COUNT(*) as total_invoices,
                COALESCE(SUM(invoice_amount), 0) as total_invoice_amount,
                COALESCE(SUM(payment_done), 0) as total_payment_amount,
                COALESCE(SUM(total_net_payment), 0) as total_net_payment_amount,
                COUNT(DISTINCT vendor_name) as unique_vendors
            FROM invoices
            WHERE created_at >= %s AND created_at <= %s
            """,
            (start_datetime, end_datetime)
        )
        row = cur.fetchone()
        
        return {
            "total_invoices": row[0],
            "total_invoice_amount": float(row[1]) if row[1] else 0.0,
            "total_payment_amount": float(row[2]) if row[2] else 0.0,
            "total_net_payment_amount": float(row[3]) if row[3] else 0.0,
            "unique_vendors": row[4]
        }

    def _get_invoice_records(
        self,
        cur,
        start_date: date,
        end_date: date,
        limit: int
    ) -> List[Dict[str, Any]]:
        """Fetch detailed invoice records."""
        # Convert dates to datetime for proper timestamp comparison
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())

        cur.execute(
            """
            SELECT
                id, utr_no, invoice_number, invoice_date, invoice_amount,
                total_payment, total_net_payment, payment_done, vendor_name,
                source, created_at
            FROM invoices
            WHERE created_at >= %s AND created_at <= %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (start_datetime, end_datetime, limit)
        )
        
        columns = [
            "id", "utr_no", "invoice_number", "invoice_date", "invoice_amount",
            "total_payment", "total_net_payment", "payment_done", "vendor_name",
            "source", "created_at"
        ]
        
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        
        # Convert datetime/date objects to ISO strings for JSON serialization
        for row in rows:
            if row.get("invoice_date"):
                row["invoice_date"] = row["invoice_date"].isoformat()
            if row.get("created_at"):
                row["created_at"] = row["created_at"].isoformat()
            # Convert Decimal to float for JSON
            for key in ["invoice_amount", "total_payment", "total_net_payment", "payment_done"]:
                if row.get(key) is not None:
                    row[key] = float(row[key])
        
        return rows

    def _get_missing_field_analysis(
        self,
        cur,
        start_date: date,
        end_date: date
    ) -> Dict[str, int]:
        """Analyze missing fields in invoices."""
        # Convert dates to datetime for proper timestamp comparison
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())

        total_invoices = 0
        cur.execute(
            "SELECT COUNT(*) FROM invoices WHERE created_at >= %s AND created_at <= %s",
            (start_datetime, end_datetime)
        )
        total_invoices = cur.fetchone()[0]

        if total_invoices == 0:
            return {}

        fields = ["invoice_number", "invoice_amount", "vendor_name", "invoice_date"]
        missing = {}

        for field in fields:
            cur.execute(
                f"SELECT COUNT(*) FROM invoices WHERE {field} IS NULL AND created_at >= %s AND created_at <= %s",
                (start_datetime, end_datetime)
            )
            missing[field] = cur.fetchone()[0]
        
        return missing

    def _get_vendor_distribution(
        self,
        cur,
        start_date: date,
        end_date: date
    ) -> List[Dict[str, Any]]:
        """Get invoice count and amount by vendor (top 10)."""
        # Convert dates to datetime for proper timestamp comparison
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())

        cur.execute(
            """
            SELECT
                vendor_name,
                COUNT(*) as invoice_count,
                COALESCE(SUM(invoice_amount), 0) as total_amount
            FROM invoices
            WHERE created_at >= %s AND created_at <= %s
            GROUP BY vendor_name
            ORDER BY invoice_count DESC
            LIMIT 10
            """,
            (start_datetime, end_datetime)
        )
        
        rows = []
        for vendor_name, count, amount in cur.fetchall():
            rows.append({
                "vendor_name": vendor_name or "Unknown",
                "invoice_count": count,
                "total_amount": float(amount) if amount else 0.0
            })
        
        return rows

    def _get_date_trends(
        self,
        cur,
        start_date: date,
        end_date: date
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Get daily and weekly trends."""
        # Convert dates to datetime for proper timestamp comparison
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())

        # Daily trends
        cur.execute(
            """
            SELECT
                DATE(created_at) as date,
                COUNT(*) as invoice_count,
                COALESCE(SUM(invoice_amount), 0) as total_amount
            FROM invoices
            WHERE created_at >= %s AND created_at <= %s
            GROUP BY DATE(created_at)
            ORDER BY date ASC
            """,
            (start_datetime, end_datetime)
        )
        
        daily_trends = []
        for trend_date, count, amount in cur.fetchall():
            daily_trends.append({
                "date": trend_date.isoformat(),
                "invoice_count": count,
                "total_amount": float(amount) if amount else 0.0
            })
        
        return {
            "daily": daily_trends
        }

    def generate_pdf_report(
        self,
        report_data: Dict[str, Any]
    ) -> BytesIO:
        """Generate a detailed, professional PDF report from report data."""
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.units import inch

        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=40,
            leftMargin=40,
            topMargin=45,
            bottomMargin=35
        )
        styles = getSampleStyleSheet()

        # Custom styles - professional and detailed
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=18,
            textColor=colors.HexColor('#1a1a2e'),
            spaceAfter=6,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )

        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Heading4'],
            fontSize=9,
            textColor=colors.HexColor('#666677'),
            spaceAfter=15,
            alignment=TA_CENTER
        )

        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=11,
            textColor=colors.HexColor('#1a1a2e'),
            spaceAfter=8,
            spaceBefore=12,
            fontName='Helvetica-Bold',
            leading=14
        )

        normal_style = ParagraphStyle(
            'NormalText',
            parent=styles['BodyText'],
            fontSize=8,
            leading=11,
            textColor=colors.HexColor('#333344')
        )

        info_style = ParagraphStyle(
            'InfoText',
            parent=normal_style,
            fontSize=7,
            textColor=colors.HexColor('#888899'),
            spaceAfter=10
        )

        # Build PDF content
        story = []

        # Header section
        story.append(Paragraph("Invoice Summary Report", title_style))
        story.append(Paragraph(
            f"Period: {report_data['date_range']['start']} to {report_data['date_range']['end']} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            subtitle_style
        ))
        story.append(Spacer(1, 0.12*inch))

        # Executive Summary - more detailed 3x3 grid
        summary = report_data["summary"]
        payment_rate = (summary['total_payment_amount'] / summary['total_invoice_amount'] * 100) if summary['total_invoice_amount'] > 0 else 0
        avg_per_vendor = (summary['total_invoice_amount'] / summary['unique_vendors'] if summary['unique_vendors'] > 0 else 0)

        summary_data = [
            ["Total Invoices", f"{summary['total_invoices']:,}", "Unique Vendors", f"{summary['unique_vendors']}", "Payment Rate", f"{payment_rate:.1f}%"],
            ["Total Invoice Amount", f"₹{summary['total_invoice_amount']:,.2f}", "Total Payment", f"₹{summary['total_payment_amount']:,.2f}", "Avg Invoice", f"₹{(summary['total_invoice_amount']/summary['total_invoices'] if summary['total_invoices'] > 0 else 0):,.2f}"],
            ["Total Net Payment", f"₹{summary['total_net_payment_amount']:,.2f}", "Avg per Vendor", f"₹{avg_per_vendor:,.2f}", "Invoice Count", f"{summary['total_invoices']/summary['unique_vendors']:.1f}/vendor" if summary['unique_vendors'] > 0 else "N/A"]
        ]

        summary_table = Table(summary_data, colWidths=[1.2*inch, 1.2*inch, 1.2*inch, 1.2*inch, 1.2*inch, 1.2*inch])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
            ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#f0f4f8')),
            ('BACKGROUND', (0, 2), (-1, 2), colors.white),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('TEXTCOLOR', (0, 1), (-1, 1), colors.HexColor('#1a1a2e')),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d0d0')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
        ]))
        story.append(summary_table)
        story.append(Spacer(1, 0.15*inch))

        # Key Insights - expanded
        story.append(Paragraph("Key Insights", heading_style))

        insights = [
            f"• Payment Completion: {payment_rate:.1f}% of total invoice amount has been paid",
            f"• Average Invoice Value: ₹{(summary['total_invoice_amount']/summary['total_invoices'] if summary['total_invoices'] > 0 else 0):,.2f}",
            f"• Vendor Concentration: {summary['unique_vendors']} vendors with avg {summary['total_invoices']/summary['unique_vendors']:.1f} invoices each" if summary['unique_vendors'] > 0 else "",
            f"• Total Volume: ₹{summary['total_invoice_amount']:,.2f} across {summary['total_invoices']} invoices"
        ]

        for insight in insights:
            if insight:
                story.append(Paragraph(insight, normal_style))

        story.append(Spacer(1, 0.15*inch))

        # Vendor Distribution - top 10 with more details
        if report_data["vendor_stats"]:
            story.append(Paragraph("Vendor Distribution (Top 10)", heading_style))
            vendor_data = [["Vendor", "Invoices", "Total Amount", "% Share", "Avg/Invoice"]]

            total_amount = summary['total_invoice_amount']
            for vendor in report_data["vendor_stats"][:10]:
                percentage = (vendor['total_amount'] / total_amount * 100) if total_amount > 0 else 0
                avg_invoice = (vendor['total_amount'] / vendor['invoice_count']) if vendor['invoice_count'] > 0 else 0
                vendor_data.append([
                    vendor["vendor_name"][:18],
                    str(vendor["invoice_count"]),
                    f"₹{vendor['total_amount']:,.0f}",
                    f"{percentage:.1f}%",
                    f"₹{avg_invoice:,.0f}"
                ])

            vendor_table = Table(vendor_data, colWidths=[1.6*inch, 0.7*inch, 1.1*inch, 0.7*inch, 1.0*inch])
            vendor_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c5282')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d0d0')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')])
            ]))
            story.append(vendor_table)
            story.append(Spacer(1, 0.15*inch))

        # Data Quality Analysis
        if report_data["missing_fields"] and any(report_data["missing_fields"].values()):
            story.append(Paragraph("Data Quality Analysis", heading_style))
            missing = report_data["missing_fields"]
            total_invoices = summary['total_invoices']
            missing_data = [["Field", "Missing Count", "% of Total"]]

            for field, count in missing.items():
                if count > 0:
                    percentage = (count / total_invoices * 100) if total_invoices > 0 else 0
                    missing_data.append([
                        field.replace('_', ' ').title(),
                        str(count),
                        f"{percentage:.1f}%"
                    ])

            if len(missing_data) > 1:
                missing_table = Table(missing_data, colWidths=[1.8*inch, 1.0*inch, 1.0*inch])
                missing_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#c05621')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 7),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 5),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d0d0')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#fffaf0')])
                ]))
                story.append(missing_table)
                story.append(Spacer(1, 0.15*inch))

        # Daily Trends - compact horizontal layout
        if report_data["trends"]["daily"]:
            story.append(Paragraph("Daily Trends", heading_style))
            trend_data = [["Date", "Invoices", "Amount", "Avg/Invoice"]]

            for trend in report_data["trends"]["daily"][-7:]:  # Last 7 days
                avg_invoice = (trend['total_amount'] / trend['invoice_count']) if trend['invoice_count'] > 0 else 0
                trend_data.append([
                    trend["date"][5:],
                    str(trend["invoice_count"]),
                    f"₹{trend['total_amount']:,.0f}",
                    f"₹{avg_invoice:,.0f}"
                ])

            trend_table = Table(trend_data, colWidths=[0.9*inch, 0.7*inch, 1.1*inch, 1.0*inch])
            trend_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2f855a')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d0d0d0')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0fff4')])
            ]))
            story.append(trend_table)
            story.append(Spacer(1, 0.12*inch))

        # Footer note
        story.append(Paragraph(
            "Note: This is a summary report. Download Excel for complete detailed records with all invoice-level data. Processing failures are not tracked in the database.",
            info_style
        ))

        doc.build(story)
        buffer.seek(0)
        return buffer

    def generate_excel_report(
        self,
        report_data: Dict[str, Any]
    ) -> BytesIO:
        """Generate an Excel report from report data."""
        import xlsxwriter
        
        buffer = BytesIO()
        workbook = xlsxwriter.Workbook(buffer, {'in_memory': True})
        
        # Summary sheet
        summary_sheet = workbook.add_worksheet('Summary')
        summary_sheet.set_column('A:A', 20)
        summary_sheet.set_column('B:B', 20)
        
        # Header
        header_format = workbook.add_format({
            'bold': True,
            'font_size': 14,
            'bg_color': '#0f1a2e',
            'font_color': 'white',
            'border': 1
        })
        
        summary_sheet.write('A1', 'Invoice Summary Report', header_format)
        summary_sheet.merge_range('A1:B1', 'Invoice Summary Report', header_format)
        
        # Date range
        date_range = report_data["date_range"]
        summary_sheet.write('A2', 'Report Period:', workbook.add_format({'bold': True}))
        summary_sheet.write('B2', f"{date_range['start']} to {date_range['end']}")
        
        # Executive Summary
        summary_sheet.write('A4', 'Executive Summary', header_format)
        summary_sheet.merge_range('A4:B4', 'Executive Summary', header_format)
        
        summary = report_data["summary"]
        summary_data = [
            ["Metric", "Value"],
            ["Total Invoices", summary['total_invoices']],
            ["Total Invoice Amount", summary['total_invoice_amount']],
            ["Total Payment Amount", summary['total_payment_amount']],
            ["Total Net Payment", summary['total_net_payment_amount']],
            ["Unique Vendors", summary['unique_vendors']]
        ]
        
        for row_idx, row in enumerate(summary_data, start=5):
            for col_idx, value in enumerate(row):
                cell_format = None
                if row_idx == 5:  # Header row
                    cell_format = workbook.add_format({'bold': True, 'bg_color': '#f4f6fa'})
                elif col_idx == 1 and isinstance(value, (int, float)):
                    cell_format = workbook.add_format({'num_format': '#,##0.00'})
                summary_sheet.write(row_idx, col_idx, value, cell_format)
        
        # Missing Fields sheet
        if report_data["missing_fields"]:
            missing_sheet = workbook.add_worksheet('Missing Fields')
            missing_sheet.set_column('A:A', 15)
            missing_sheet.set_column('B:B', 10)
            
            missing_sheet.write('A1', 'Missing Fields Analysis', header_format)
            missing_sheet.merge_range('A1:B1', 'Missing Fields Analysis', header_format)
            
            missing_data = [["Field", "Count"]]
            for field, count in report_data["missing_fields"].items():
                missing_data.append([field, count])
            
            for row_idx, row in enumerate(missing_data, start=2):
                for col_idx, value in enumerate(row):
                    cell_format = workbook.add_format({'bold': True, 'bg_color': '#fdf1de'}) if row_idx == 2 else None
                    missing_sheet.write(row_idx, col_idx, value, cell_format)
        
        # Vendor Distribution sheet
        if report_data["vendor_stats"]:
            vendor_sheet = workbook.add_worksheet('Vendor Distribution')
            vendor_sheet.set_column('A:A', 20)
            vendor_sheet.set_column('B:B', 12)
            vendor_sheet.set_column('C:C', 15)
            
            vendor_sheet.write('A1', 'Top Vendors by Invoice Count', header_format)
            vendor_sheet.merge_range('A1:C1', 'Top Vendors by Invoice Count', header_format)
            
            vendor_data = [["Vendor", "Invoices", "Total Amount"]]
            for vendor in report_data["vendor_stats"]:
                vendor_data.append([
                    vendor["vendor_name"],
                    vendor["invoice_count"],
                    vendor["total_amount"]
                ])
            
            for row_idx, row in enumerate(vendor_data, start=2):
                for col_idx, value in enumerate(row):
                    cell_format = workbook.add_format({'bold': True, 'bg_color': '#e7f0fd'}) if row_idx == 2 else None
                    if col_idx == 2 and isinstance(value, (int, float)):
                        cell_format = workbook.add_format({'num_format': '#,##0.00'})
                    vendor_sheet.write(row_idx, col_idx, value, cell_format)
        
        # Daily Trends sheet
        if report_data["trends"]["daily"]:
            trends_sheet = workbook.add_worksheet('Daily Trends')
            trends_sheet.set_column('A:A', 12)
            trends_sheet.set_column('B:B', 10)
            trends_sheet.set_column('C:C', 15)
            
            trends_sheet.write('A1', 'Daily Invoice Trends', header_format)
            trends_sheet.merge_range('A1:C1', 'Daily Invoice Trends', header_format)
            
            trend_data = [["Date", "Invoices", "Total Amount"]]
            for trend in report_data["trends"]["daily"]:
                trend_data.append([
                    trend["date"],
                    trend["invoice_count"],
                    trend["total_amount"]
                ])
            
            for row_idx, row in enumerate(trend_data, start=2):
                for col_idx, value in enumerate(row):
                    cell_format = workbook.add_format({'bold': True, 'bg_color': '#eaf7ee'}) if row_idx == 2 else None
                    if col_idx == 2 and isinstance(value, (int, float)):
                        cell_format = workbook.add_format({'num_format': '#,##0.00'})
                    trends_sheet.write(row_idx, col_idx, value, cell_format)
        
        # Detailed Invoices sheet
        invoices_sheet = workbook.add_worksheet('Detailed Invoices')
        columns = [
            ("ID", 8),
            ("UTR No.", 15),
            ("Invoice No.", 18),
            ("Vendor", 20),
            ("Invoice Date", 12),
            ("Invoice Amount", 15),
            ("Total Payment", 15),
            ("Total Net Payment", 15),
            ("Payment Done", 15),
            ("Source", 12),
            ("Created At", 20)
        ]
        
        for col_idx, (col_name, width) in enumerate(columns):
            invoices_sheet.set_column(col_idx, col_idx, width)
        
        # Header
        invoice_header = [col[0] for col in columns]
        for col_idx, header in enumerate(invoice_header):
            invoices_sheet.write(0, col_idx, header, header_format)
        
        # Data
        for row_idx, inv in enumerate(report_data["invoices"], start=1):
            row_data = [
                inv.get("id"),
                inv.get("utr_no"),
                inv.get("invoice_number"),
                inv.get("vendor_name"),
                inv.get("invoice_date"),
                inv.get("invoice_amount"),
                inv.get("total_payment"),
                inv.get("total_net_payment"),
                inv.get("payment_done"),
                inv.get("source"),
                inv.get("created_at")
            ]
            
            for col_idx, value in enumerate(row_data):
                cell_format = None
                if col_idx in [5, 6, 7, 8] and isinstance(value, (int, float)):
                    cell_format = workbook.add_format({'num_format': '#,##0.00'})
                invoices_sheet.write(row_idx, col_idx, value, cell_format)
        
        workbook.close()
        buffer.seek(0)
        return buffer


