# Stability test guide (temporary monitor)

Everything here is **new and separate** from your application. No existing
file is changed. When the test is over, delete the `stability_monitor` folder
and the project is exactly as before.

**Your environment (found by inspecting the project):** Windows + Python 3.10 virtual
environment (`.venv\Scripts`). All commands below are **PowerShell**, run **from the
project folder** (the folder that contains `run.py`). If your company server is Linux, see
section 9.

**Where things run.** The monitor must run on **the same machine as the invoice
application** (it checks `127.0.0.1` and that machine's CPU/RAM/disk). Below, that machine
is called *"the app machine"* (your company server, or your PC if that is where you run it).

Shortcut used in every command: `.\.venv\Scripts\python.exe` is the project's virtual-environment
Python. Using it directly means you never need to "activate" anything.

---

## 1. One-time setup  (app machine, PowerShell, project folder)

**1.1 Copy the folder.** Put the `stability_monitor` folder next to `run.py`.

**1.2 Install the one missing package (psutil).**
```powershell
.\.venv\Scripts\python.exe -m pip install -r stability_monitor\requirements-monitor.txt
```
Expected: `Successfully installed psutil-5.9.8` (or "Requirement already satisfied").
Everything else the monitor needs (`requests`, `python-dotenv`, `psycopg`) is already in your venv.

**1.3 Create the monitor settings file.**
```powershell
copy stability_monitor\.env.example stability_monitor\.env
notepad stability_monitor\.env
```
Fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; leave the rest as is. Save.

> ⚠️ Do **not** add these lines to the application's own `.env`. Your app's settings loader
> rejects unknown entries and the app would refuse to start.

**1.3 (continued) Get the bot token.** In Telegram open **@BotFather** → `/mybots` → your bot →
**API Token**. Paste it after `TELEGRAM_BOT_TOKEN=` (no quotes, no spaces).

**1.4 Get your chat ID.**
1. In Telegram, open your bot and send it any message, e.g. `hello`.
   (For a group: add the bot to the group and send a message there.)
2. Run:
```powershell
.\.venv\Scripts\python.exe stability_monitor\telegram_notifier.py find-chat-id
```
Expected:
```
Chats that have contacted this bot:

  TELEGRAM_CHAT_ID=123456789    (private: Your Name)
```
Copy that number into `stability_monitor\.env`. (Group IDs are negative, e.g. `-100123…` - keep the minus.)

**1.5 Prove Telegram works.**
```powershell
.\.venv\Scripts\python.exe stability_monitor\telegram_notifier.py test
```
Expected: `Sent.` and a Telegram message within seconds.
If it says `FAILED ... ConnectionError`, the machine cannot reach Telegram (company firewall/proxy).
Check with `Test-NetConnection api.telegram.org -Port 443` (want `TcpTestSucceeded : True`) and ask IT to allow `api.telegram.org`.

**1.6 Dry-run all checks (nothing is sent).** With the app running or not:
```powershell
.\.venv\Scripts\python.exe stability_monitor\monitor.py --check-now
```
Expected (app running):
```
Application: OK - HTTP 200 in 12ms   [http://127.0.0.1:8000/]
PostgreSQL : OK - 5ms   [localhost:5432/your_db]
Last background run in app.log: 2026-09-28 10:15:04   (stall limit 20 min, watch on)
CPU        : 6%  (limit 90%)
RAM        : 48%  ...
Disk       : 61%  free 120.4 GB on C:\  (limit 90%)
Telegram   : token + chat id present
```
If the app is stopped you will see `Application: FAILED (down)` - that is correct and proves detection.

---

## 2. Start everything  (app machine)

Use **two separate PowerShell windows**. Do not close them for the whole test.

**Window 1 - the invoice application**
```powershell
cd <your project folder>
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Why not `python run.py`? It starts uvicorn with `reload=True` (a file-watcher for development that
restarts the server whenever a `.py` file changes). It is not what you want for a 24-hour stability test.
This command starts the *same* application without that; `run.py` is untouched.

Expected: `Uvicorn running on http://0.0.0.0:8000`, and in `logs\app.log`:
`Starting AI Invoice Automation …` then `Background processing started - checking for new invoices every 5 minute(s)`.

**Window 2 - the monitor**
```powershell
cd <your project folder>
.\.venv\Scripts\python.exe stability_monitor\monitor.py
```
Expected: a Telegram message **"✅ Invoice Automation Monitor Started"** and one line per minute:
```
2026-09-29 10:01:00 | INFO    | app=OK db=OK 4ms cpu=6% ram=48% disk=61%(free 120.4 GB) app_mem=210MB last_bg_run=2min ago new_app_errors=0
```
No Telegram message is sent for healthy checks.

**Check both are running**
```powershell
Invoke-RestMethod http://127.0.0.1:8000/          # expect: status  running
Get-Content logs\monitor.log -Tail 5              # expect: fresh lines, timestamp within the last minute
Get-Content logs\app.log -Tail 5                  # expect: "Background processing run complete" every ~5 min
```

**Keep the machine from stopping the test (Windows)**
- Settings → System → Power → set *Screen and sleep* to **Never** (a sleeping PC freezes everything).
- **Do not click inside the console windows.** Windows "QuickEdit" pauses a program while text is
  selected - it looks like a hang. If a window looks frozen, click it and press Enter.
- Postpone Windows Update restarts for the test period.

---

## 3. Stop  (app machine)

1. **Monitor first:** click its window → `Ctrl+C`. Telegram: *"🛑 Monitor Stopped - Reason: stopped manually"* plus a summary.
2. **Then the application:** click its window → `Ctrl+C`.

(If you stop the app first the monitor will - correctly - tell you the app is down.)

---

## 4. Test that alerts really reach you  (do this BEFORE the long test)

None of these harm your data. Add `--interval 10` to make things happen 6x faster.

| # | What it proves | How | You should get |
|---|---|---|---|
| T1 | Telegram works | `... telegram_notifier.py test` | one test message |
| T2 | All alert types & recovery, with **zero risk** - failures are faked inside the monitor only | `.\.venv\Scripts\python.exe stability_monitor\monitor.py --interval 10 --simulate db,cpu,ram,disk,stall --simulate-minutes 2` | Started msg (mentions SIMULATING) → within ~1 min: 🚨 PostgreSQL, ⚠️ CPU, ⚠️ RAM, ⚠️ Disk, 🚨 background-processing alerts, each labelled "SIMULATED TEST" → after 2 min: a ✅ recovered for each. Then `Ctrl+C`. Only **one** message per problem, not one per check. |
| T3 | Real app-stop detection | With app + monitor running: click app window → `Ctrl+C`. Wait ~2 min (2 failed checks). | 🚨 "Application is not running", with a clue ("app.log ends with a normal shutdown message" = you stopped it; "NO shutdown message" = it crashed). Restart the app → ✅ "Application RECOVERED - was down for …". |
| T4 | "Not healthy" detection without touching the app | Stop monitor, then: `$env:HEALTH_URL="http://127.0.0.1:8000/nope"` and start the monitor. Afterwards `Remove-Item Env:HEALTH_URL`. | 🚨 "Application health check failing - HTTP 404" after 2 checks |
| T5 | Manual stop vs crash | `Ctrl+C` the monitor → 🛑 "stopped manually". Then start it again and close its window with the ✕ (not Ctrl+C) and start again. | second start says "⚠️ previous monitor session did NOT stop cleanly" |
| T6 *(optional)* | Real PostgreSQL outage | Only when no invoice is being processed: PowerShell **as Administrator** `Stop-Service <your postgres service name>`, wait 2 min, then `Start-Service …`. (Find the name with `Get-Service *postgres*`.) | 🚨 PostgreSQL connection failed → ✅ RECOVERED. T2 already covers the alert path; this only adds a real connection failure. Never kill Postgres or touch its data folder. |

Timing to expect: an alert appears after `threshold × interval` - app/DB: 2 checks (~2 min at the default 60 s);
CPU/RAM: 5 checks in a row (~5 min); disk: 2 checks.

---

## 5. Testing the real invoice workflow (the monitor does NOT do this)

The monitor never calls Gemini or reads the mailbox. You test the real chain by hand:

`Email → Microsoft Graph → Gemini → Validation → PostgreSQL`

**Schedule (adjust to your day):** T+0 start both windows · then send **one test email** at T+1h, T+3h, T+6h, T+12h, T+24h
(48-hour run: also T+36h, T+48h).

**Rules for each test email**
- Send a **new, different invoice** each time (different invoice numbers). An identical PDF/body is skipped
  on purpose as a duplicate, which would look like a failure but isn't.
- Write down the **expected number of invoices** in that email before sending.

**After each send (wait one polling interval - 5 min by default):**
```powershell
# (a) Did the background run pick it up, and how many were stored?
Select-String -Path logs\app.log -Pattern "Background processing run complete" | Select-Object -Last 2
#     look for  'invoices_extracted': N, 'invoices_stored': N, 'errors': []
# (b) Any errors?
Select-String -Path logs\app.log -Pattern " - ERROR - " | Select-Object -Last 5
```
**(c) PostgreSQL (pgAdmin → Query Tool, read-only SELECTs):**
```sql
-- newest rows
SELECT id, invoice_number, invoice_amount, utr_no, total_net_payment, source, created_at
FROM invoices ORDER BY created_at DESC LIMIT 30;

-- how many were stored since a given time (put your test start time here)
SELECT count(*) FROM invoices WHERE created_at >= '2026-09-29 10:00:00+05:30';

-- stored per hour over the last 24 h
SELECT date_trunc('hour', created_at) AS hour, count(*)
FROM invoices WHERE created_at >= now() - interval '24 hours' GROUP BY 1 ORDER BY 1;
```
Compare "expected" vs "stored" for each test email.

---

## 6. 24-hour (and optional 48-hour) test procedure

**Before starting**
- [ ] Sections 1 and 4 done (Telegram test message received, T2 passed)
- [ ] Sleep disabled, windows will not be touched
- [ ] `logs\monitor.log` and `logs\app.log` noted (or archived) so you can tell new lines from old

**Start:** both windows (section 2). Write down the start time.
**During:** send test emails per section 5; glance at Telegram - silence means healthy.
**Optional:** in `stability_monitor\.env` set `HEARTBEAT_HOURS=3` to receive "💓 still running" every 3 hours.
If those stop arriving, the monitor or the whole machine/network is down (the monitor cannot alert about its own machine dying).

**End (T+24h / T+48h):** stop monitor (Ctrl+C) → the Telegram "Stopped" message contains a summary. Then stop the app.
Collect:
```powershell
Select-String -Path logs\monitor.log -Pattern "ALERT" | Out-File test_alerts.txt      # every alert raised
(Select-String -Path logs\app.log -Pattern " - ERROR - ").Count                        # app errors
Select-String -Path logs\monitor.log -Pattern "app_mem" | Select-Object -First 1       # memory at start
Select-String -Path logs\monitor.log -Pattern "app_mem" | Select-Object -Last 1        # memory at end
```
A steadily rising `app_mem` from the first to the last line suggests a memory leak.

**Result sheet - copy this**

| Item | Value |
|---|---|
| Test length (24h / 48h) | |
| Start time / End time | |
| Test emails sent | |
| Expected invoices in total | |
| Processed successfully (app.log) | |
| Stored in PostgreSQL (SQL count) | |
| Application crashes (🚨 "not running" alerts, and were they crashes?) | |
| Background-loop stalls | |
| PostgreSQL failures | |
| Telegram alerts received (list) | |
| Peak CPU / RAM / Disk (from the Stopped summary) | |
| App memory: start → end | |
| ERROR lines in app.log (and what they were) | |
| Verdict | Pass / Fail |

**Suggested "pass":** app never went down unexpectedly, every test email's invoices were stored (expected = stored),
no unexplained ERROR lines, memory did not climb steadily, disk/CPU/RAM stayed below the limits.

---

## 7. What the monitor can and cannot tell you

- Connection refused = nothing listening = app stopped **or** crashed. The message adds a clue from `app.log`
  (normal shutdown line present or not) but cannot be 100% certain.
- "Background processing stopped" reads `logs\app.log`. If you **pause sync** in the UI you will get this alert
  (expected). Set `BACKGROUND_STALL_CHECK=false` to switch it off.
- CPU is the average over each check interval, so a 2-second spike never alerts.
- If Telegram itself is unreachable, alerts are queued and delivered later, marked "⏱ Delayed alert".
- New `ERROR` lines in `app.log` are written to `monitor.log` and counted in the summary, but are **not** sent
  to Telegram (to avoid noise).
- No monitor can warn you that the machine itself lost power or network - use the heartbeat and notice the silence.

## 8. Settings reference (`stability_monitor\.env`)

| Setting | Default | Meaning |
|---|---|---|
| `MONITOR_INTERVAL` | 60 | seconds between checks |
| `HEALTH_URL` | `http://127.0.0.1:8000/` | app address to check |
| `APP_FAILURE_THRESHOLD` / `DB_FAILURE_THRESHOLD` | 2 / 2 | failed checks in a row before alerting |
| `CPU_WARNING_PERCENT` / `RAM_WARNING_PERCENT` / `DISK_WARNING_PERCENT` | 90 | limits |
| `RESOURCE_CONSECUTIVE_CHECKS` | 5 | CPU/RAM must stay over the limit this many checks |
| `DISK_PATH` | project's drive | which drive to watch |
| `BACKGROUND_STALL_CHECK` / `BACKGROUND_STALL_MINUTES` | true / 0 (auto = 2×interval+10 min) | background-loop watch |
| `HEARTBEAT_HOURS` | 0 (off) | "still running" message every N hours |
| `DB_CHECK_ENABLED` | true | set false to skip PostgreSQL checks |

## 9. If the server is Linux instead

Same files, same settings. Only these commands differ:

| Windows | Linux |
|---|---|
| `.\.venv\Scripts\python.exe` | `./.venv/bin/python` |
| `copy a b` | `cp a b` |
| `Get-Content f -Tail 5` | `tail -n 5 f` |
| `Select-String -Path f -Pattern x` | `grep x f` |
| `Invoke-RestMethod url` | `curl url` |
| `Stop-Service` / `Start-Service` | `sudo systemctl stop/start postgresql` |
| two PowerShell windows | two terminals (or `tmux`); do not use a terminal you will close |

## 10. Removing everything afterwards

Delete the `stability_monitor` folder (and optionally `logs\monitor.log*`). Nothing else was installed
except `psutil` in the virtual environment (`.\.venv\Scripts\python.exe -m pip uninstall psutil`).
