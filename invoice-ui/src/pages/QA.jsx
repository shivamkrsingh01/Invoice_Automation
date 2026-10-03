import { useState } from 'react'
import { Send, MessageCircle, Loader2 } from 'lucide-react'
import { askQuestion } from '../api/client'

const EXAMPLE_QUESTIONS = [
  'How many invoices are there in total?',
  'Which invoices are missing an amount?',
  'What is the total invoice amount?',
]

export default function QA() {
  const [input, setInput] = useState('')
  const [history, setHistory] = useState([]) // [{ question, answer, error }]
  const [loading, setLoading] = useState(false)

  async function handleAsk(question) {
    const q = question.trim()
    if (!q || loading) return

    setInput('')
    setLoading(true)
    // Show the question immediately, fill in the answer once it arrives -
    // so it's clear the app registered the question even while waiting.
    setHistory((h) => [...h, { question: q, answer: null, error: null }])

    try {
      const data = await askQuestion(q)
      setHistory((h) =>
        h.map((item, i) => (i === h.length - 1 ? { ...item, answer: data.answer } : item))
      )
    } catch (err) {
      setHistory((h) =>
        h.map((item, i) => (i === h.length - 1 ? { ...item, error: err.message } : item))
      )
    } finally {
      setLoading(false)
    }
  }

  function handleSubmit(e) {
    e.preventDefault()
    handleAsk(input)
  }

  return (
    <div className="page-fade-in">
      <div className="page-header">
        <h1>Q&amp;A</h1>
        <p>Ask questions about your invoice data</p>
      </div>

      <div className="card qa-card">
        {history.length === 0 && (
          <div className="qa-empty">
            <MessageCircle size={28} color="var(--text-secondary)" />
            <p>Ask a question about your stored invoices - try one of these:</p>
            <div className="qa-examples">
              {EXAMPLE_QUESTIONS.map((q) => (
                <button key={q} className="qa-example-btn" onClick={() => handleAsk(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        {history.length > 0 && (
          <div className="qa-history">
            {history.map((item, i) => (
              <div key={i} className="qa-exchange">
                <div className="qa-question">{item.question}</div>
                {item.error && <div className="qa-answer qa-answer-error">Couldn't get an answer: {item.error}</div>}
                {!item.error && item.answer === null && (
                  <div className="qa-answer qa-answer-loading">
                    <Loader2 size={14} className="spin" /> Thinking...
                  </div>
                )}
                {!item.error && item.answer !== null && <div className="qa-answer">{item.answer}</div>}
              </div>
            ))}
          </div>
        )}

        <form className="qa-input-row" onSubmit={handleSubmit}>
          <input
            type="text"
            placeholder="Ask a question about your invoices..."
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={loading}
          />
          <button type="submit" className="qa-send-btn" disabled={loading || !input.trim()}>
            <Send size={16} />
          </button>
        </form>
      </div>
    </div>
  )
}
