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
            (start_date, end_date)
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
            (start_date, end_date, limit)
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
        total_invoices = 0
        cur.execute(
            "SELECT COUNT(*) FROM invoices WHERE created_at >= %s AND created_at <= %s",
            (start_date, end_date)
        )
        total_invoices = cur.fetchone()[0]
        
        if total_invoices == 0:
            return {}
        
        fields = ["invoice_number", "invoice_amount", "vendor_name", "invoice_date"]
        missing = {}
        
        for field in fields:
            cur.execute(
                f"SELECT COUNT(*) FROM invoices WHERE {field} IS NULL AND created_at >= %s AND created_at <= %s",
                (start_date, end_date)
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
            (start_date, end_date)
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
            (start_date, end_date)
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
        """Generate a professional, compact PDF report from report data."""
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
            rightMargin=50,
            leftMargin=50,
            topMargin=50,
            bottomMargin=40
        )
        styles = getSampleStyleSheet()
        
        # Custom styles - compact and professional
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=22,
            textColor=colors.HexColor('#1a1a2e'),
            spaceAfter=8,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )
        
        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Heading4'],
            fontSize=10,
            textColor=colors.HexColor('#666677'),
            spaceAfter=20,
            alignment=TA_CENTER
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=13,
            textColor=colors.HexColor('#1a1a2e'),
            spaceAfter=10,
            spaceBefore=15,
            fontName='Helvetica-Bold',
            leading=16
        )
        
        normal_style = ParagraphStyle(
            'NormalText',
            parent=styles['BodyText'],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor('#333344')
        )
        
        info_style = ParagraphStyle(
            'InfoText',
            parent=normal_style,
            fontSize=8,
            textColor=colors.HexColor('#888899'),
            spaceAfter=15
        )
        
        # Build PDF content
        story = []
        
        # Header section - compact
        story.append(Paragraph("Invoice Summary Report", title_style))
        story.append(Paragraph(
            f"Period: {report_data['date_range']['start']} to {report_data['date_range']['end']}",
            subtitle_style
        ))
        story.append(Spacer(1, 0.15*inch))
        
        # Executive Summary - compact table
        summary = report_data["summary"]
        summary_data = [
            ["Total Invoices", f"{summary['total_invoices']:,}", "Total Invoice Amount", f"₹{summary['total_invoice_amount']:,.2f}"],
            ["Total Payment", f"₹{summary['total_payment_amount']:,.2f}", "Total Net Payment", f"₹{summary['total_net_payment_amount']:,.2f}"],
            ["Unique Vendors", f"{summary['unique_vendors']}", "Avg Invoice Value", f"₹{(summary['total_invoice_amount']/summary['total_invoices'] if summary['total_invoices'] > 0 else 0):,.2f}"]
        ]
        
        summary_table = Table(summary_data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
            ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#f8f9fa')),
            ('BACKGROUND', (0, 2), (-1, 2), colors.white),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('TEXTCOLOR', (0, 1), (-1, 1), colors.HexColor('#1a1a2e')),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
        ]))
        story.append(summary_table)
        story.append(Spacer(1, 0.2*inch))
        
        # Key Insights - compact bullet points
        story.append(Paragraph("Key Insights", heading_style))
        
        payment_rate = (summary['total_payment_amount'] / summary['total_invoice_amount'] * 100) if summary['total_invoice_amount'] > 0 else 0
        insights = [
            f"• Payment Rate: {payment_rate:.1f}% of total invoices paid",
            f"• Average Invoice: ₹{(summary['total_invoice_amount']/summary['total_invoices'] if summary['total_invoices'] > 0 else 0):,.2f}",
            f"• Vendor Activity: {summary['total_invoices']/summary['unique_vendors']:.1f} invoices/vendor" if summary['unique_vendors'] > 0 else ""
        ]
        
        for insight in insights:
            if insight:
                story.append(Paragraph(insight, normal_style))
        
        story.append(Spacer(1, 0.2*inch))
        
        # Vendor Distribution - compact table
        if report_data["vendor_stats"]:
            story.append(Paragraph("Top Vendors", heading_style))
            vendor_data = [["Vendor", "Invoices", "Amount", "%"]]
            
            total_amount = summary['total_invoice_amount']
            for vendor in report_data["vendor_stats"][:8]:  # Top 8 only
                percentage = (vendor['total_amount'] / total_amount * 100) if total_amount > 0 else 0
                vendor_data.append([
                    vendor["vendor_name"][:20],  # Truncate long names
                    str(vendor["invoice_count"]),
                    f"₹{vendor['total_amount']:,.0f}",
                    f"{percentage:.1f}%"
                ])
            
            vendor_table = Table(vendor_data, colWidths=[2*inch, 0.8*inch, 1.2*inch, 0.8*inch])
            vendor_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4a90e2')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')])
            ]))
            story.append(vendor_table)
            story.append(Spacer(1, 0.2*inch))
        
        # Missing Fields - compact if exists
        if report_data["missing_fields"] and any(report_data["missing_fields"].values()):
            story.append(Paragraph("Data Quality", heading_style))
            missing = report_data["missing_fields"]
            missing_data = [[field.replace('_', ' ').title(), str(count)] for field, count in missing.items() if count > 0]
            
            if missing_data:
                missing_table = Table([["Field", "Missing"]] + missing_data, colWidths=[2*inch, 1*inch])
                missing_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f5a623')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 6),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0'))
                ]))
                story.append(missing_table)
                story.append(Spacer(1, 0.2*inch))
        
        # Daily Trends - compact, last 10 days only
        if report_data["trends"]["daily"]:
            story.append(Paragraph("Daily Trends (Last 10 Days)", heading_style))
            trend_data = [["Date", "Invoices", "Amount"]]
            for trend in report_data["trends"]["daily"][-10:]:  # Last 10 only
                trend_data.append([
                    trend["date"][5:],  # Short date (MM-DD)
                    str(trend["invoice_count"]),
                    f"₹{trend['total_amount']:,.0f}"
                ])
            
            trend_table = Table(trend_data, colWidths=[1*inch, 0.8*inch, 1.2*inch])
            trend_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#27ae60')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')])
            ]))
            story.append(trend_table)
            story.append(Spacer(1, 0.2*inch))
        
        # Footer note
        story.append(Paragraph(
            "Download Excel for complete detailed records. Failures not tracked in database.",
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


