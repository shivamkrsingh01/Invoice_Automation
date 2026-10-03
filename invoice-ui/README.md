# Invoice Automation UI

React frontend for the AI Invoice Automation backend. Built with Vite.

## Part 2 status

- App shell + sidebar navigation (Dashboard, Invoices, Validation, Q&A)
- **Dashboard** - fully wired to the real backend: recent invoices table,
  sync status banner, Sync/Stop Sync/View Invoices/Ask a Question actions
- Invoices, Validation, Q&A - placeholder pages (nav works, content comes
  in later parts)

## Running it during development

Requires Node.js and your FastAPI backend already running on port 8000.

```bash
npm install
npm run dev
```

Opens on http://localhost:5173. API calls to `/api/...` are automatically
proxied to `http://localhost:8000` (see `vite.config.js`) - no extra
configuration needed, and no CORS issues in dev even though it's a
different port.

## Building for production

```bash
npm run build
```

This produces a `dist/` folder of static HTML/JS/CSS. The intended setup
is for your FastAPI app to serve this folder directly (single deployable
unit, no separate frontend host) - see the backend changes for how that's
wired up. Once that's in place, the exact same `/api/...` calls just work,
since frontend and backend are on the same origin with no proxy needed.

## Project structure

```
src/
  api/client.js       All backend calls live here - one function per endpoint
  App.jsx             Layout shell: sidebar + top bar + page outlet
  main.jsx            Router setup
  index.css           All styling (design tokens at the top)
  pages/
    Dashboard.jsx      Built - real data
    Invoices.jsx       Placeholder
    Validation.jsx     Placeholder
    QA.jsx             Placeholder
```
