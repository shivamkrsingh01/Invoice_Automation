import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import App from './App.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Invoices from './pages/Invoices.jsx'
import Validation from './pages/Validation.jsx'
import QA from './pages/QA.jsx'
import Reports from './pages/Reports.jsx'
import Notifications from './pages/Notifications.jsx'
import Login from './pages/Login.jsx'
import RequireAuth from './components/RequireAuth.jsx'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <App />
            </RequireAuth>
          }
        >
          <Route index element={<Dashboard />} />
          <Route path="invoices" element={<Invoices />} />
          <Route path="validation" element={<Validation />} />
          <Route path="qa" element={<QA />} />
          <Route path="reports" element={<Reports />} />
          <Route path="notifications" element={<Notifications />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
)
