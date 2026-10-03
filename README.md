# AI Invoice Automation

Watches an Outlook mailbox for invoice emails, extracts the key details
from each attachment (PDF, DOCX, XLSX) or email body, and stores the
result in PostgreSQL. A React dashboard sits on top for browsing the
data. Runs continuously in the background - no manual step needed once
it's started.

## How it works

1. A background loop checks the mailbox every few minutes (interval set
   in `.env`).
2. New emails since the last check are fetched, oldest first.
3. Each attachment is routed by file type to the matching extractor.
4. Extracted fields (invoice number, vendor, date, amount, payment,
   UTR number) are validated and normalized - dates and currency
   amounts are cleaned up, missing values become real database NULLs.
5. The invoice is checked against what's already stored (by invoice
   number + vendor name) so the same invoice never gets stored twice,
   even if it arrives again in a different email or file format.
6. New invoices are inserted into PostgreSQL.
7. A "last processed" marker advances only as far as what was actually
   handled this run, so nothing gets silently skipped even if a lot of
   emails arrive at once.

## Tech stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI + Uvicorn |
| Email source | Microsoft Graph API (Outlook) |
| Extraction | PyMuPDF, pdfplumber, Tesseract OCR, python-docx, pandas/openpyxl |
| Database | PostgreSQL |
| Frontend | React (Vite) |

## Project structure

```
Email_Invoice_Automation/
├── app/
│   ├── main.py                     FastAPI app + background sync loop
│   ├── api/routes.py                All API endpoints
│   ├── services/
│   │   ├── invoice_processor.py     Orchestrates the whole pipeline
│   │   ├── graph_service.py         Talks to Outlook (Microsoft Graph)
│   │   ├── document_processor.py    Reads DOCX/XLSX attachments
│   │   ├── free_extraction_service.py   Extracts invoice fields
│   │   └── postgres_storage_service.py  All database access
│   ├── utils/                       Validation, date/amount parsing, dedup
│   ├── models/invoice.py            The invoice data shape
│   └── config/settings.py           Reads all settings from .env
├── requirements.txt
├── run.py                          Starts the backend
├── .env                            Your credentials and settings (not in git)
├── last_processed.json             Tracks how far the sync has gotten
├── logs/app.log                    Run history and errors
└── invoice-ui/                      Frontend (React)
    ├── src/
    │   ├── api/client.js            All calls to the backend live here
    │   ├── App.jsx                  Sidebar + page layout
    │   └── pages/                   Dashboard, Invoices, Validation, Q&A
    ├── vite.config.js
    └── package.json
```

## Requirements

- Python 3.10+
- Node.js (for the frontend)
- PostgreSQL, running, with the database from `.env` already created
- A Microsoft Entra app registration with Mail.Read permission for the
  mailbox you're monitoring
- A `.env` file (see Configuration below)

## Setup

**1. Backend**
```bash
pip install -r requirements.txt
```
Fill in `.env` with your Outlook and PostgreSQL details (see
Configuration below), then:
```bash
python run.py
```
Runs on `http://localhost:8000`. On first run it creates the database
tables automatically if they don't exist yet.

**2. Frontend**
```bash
cd invoice-ui
npm install
npm run dev
```
Runs on `http://localhost:5173`. Requires the backend to already be
running - it has no data of its own, it only displays what the backend
gives it.

Both need to be running at the same time during development. (Later,
once the frontend is built for production, the backend will serve it
directly and you'll only run one thing - not set up yet.)

## Configuration (.env)

| Setting | What it's for |
|---|---|
| `MICROSOFT_CLIENT_ID` / `CLIENT_SECRET` / `TENANT_ID` | Your app registration's credentials for Microsoft Graph |
| `OUTLOOK_MAILBOX` | The mailbox address being monitored |
| `POSTGRES_HOST` / `PORT` / `DB` / `USER` / `PASSWORD` | Database connection |
| `ENABLE_BACKGROUND_PROCESSING` | `true` to auto-poll the mailbox; `false` to only process when triggered manually |
| `BACKGROUND_PROCESSING_INTERVAL_MINUTES` | How often the mailbox is checked |
| `MARK_EMAILS_AS_READ` | Whether processed emails get marked read in Outlook |

## API endpoints

| Endpoint | Method | What it does |
|---|---|---|
| `/api/health` | GET | Basic health check |
| `/api/process` | POST | Manually trigger one processing run |
| `/api/invoices` | GET | Paginated invoice list (`?limit=&offset=&search=`) |
| `/api/invoices/validation` | GET | Invoices missing a key field (number, amount, vendor, or date) |
| `/api/sync/status` | GET | Whether sync is running/paused, and last sync time |
| `/api/sync/stop` | POST | Pause the background sync loop |
| `/api/sync/resume` | POST | Resume it |

Full interactive docs (auto-generated): `http://localhost:8000/docs`

## Verifying it's working

A quick way to sanity-check the data in pgAdmin:

```sql
SELECT COUNT(*) FROM invoices;                                    -- total stored
SELECT * FROM invoices WHERE invoice_number IS NULL                -- missing-field check
   OR invoice_amount IS NULL OR vendor_name IS NULL OR invoice_date IS NULL;
```
Compare the count against what `logs/app.log` reports for the same run.

**One thing to know:** always open the Query Tool from the correct
database in the tree (matching `POSTGRES_DB` in `.env`) - opening it
from the default `postgres` database will show a different, unrelated
table even if it's also called `invoices`.

## Current status

- **Backend** - working: fetches emails, extracts invoices, stores
  them in PostgreSQL, runs continuously in the background.
- **Frontend** - Dashboard page is complete and shows real data
  (recent invoices, sync status, Sync/Stop Sync controls). Invoices,
  Validation, and Q&A pages are placeholders - not built yet.
