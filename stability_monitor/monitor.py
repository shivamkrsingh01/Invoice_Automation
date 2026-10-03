# """
# Invoice Automation - temporary stability-test watchdog.

# Runs as its OWN process, completely separate from the invoice application.
# It never imports the application, never writes to its files, never talks to
# Microsoft Graph and never calls Gemini. It only:

#   * checks the app over HTTP            (GET http://127.0.0.1:8000/)
#   * checks PostgreSQL                   (connect + SELECT 1, read-only)
#   * reads logs/app.log                  (read-only: is the background loop still
#                                          completing runs? any new ERROR lines?)
#   * checks CPU / RAM / disk             (psutil)
#   * sends Telegram messages             (one when a problem starts, one when it ends)

# Usage (from the project folder, with the project's virtual environment):

#     python stability_monitor/monitor.py                       run the monitor
#     python stability_monitor/monitor.py --check-now           one check, printed, no Telegram
#     python stability_monitor/monitor.py --simulate app,db     fake failures to test alerts
#     python stability_monitor/monitor.py --no-telegram         dry run (alerts printed, not sent)

# Settings: stability_monitor/.env (see .env.example). The app's own .env is
# only READ, to reuse the PostgreSQL connection details.
# """
# import argparse
# import json
# import logging
# import os
# import re
# import signal
# import socket
# import sys
# import time
# import traceback
# from collections import deque
# from dataclasses import dataclass
# from datetime import datetime, timedelta
# from logging.handlers import RotatingFileHandler
# from pathlib import Path
# from typing import Deque, Dict, List, Optional
# from urllib.parse import urlparse

# import psutil
# import requests
# from dotenv import dotenv_values

# HERE = Path(__file__).resolve().parent
# ROOT = HERE.parent
# sys.path.insert(0, str(HERE))
# from telegram_notifier import DEFAULT_API_BASE, TelegramNotifier  # noqa: E402

# try:  # never crash on a Windows console that cannot print a character
#     sys.stdout.reconfigure(errors="replace")
# except Exception:
#     pass

# SIMULATABLE = {"app", "db", "cpu", "ram", "disk", "stall"}
# STATE_FILE = HERE / "monitor_state.json"


# # =====================================================================
# # Configuration
# # =====================================================================
# class ConfigError(Exception):
#     pass


# class Config:
#     """Lookup order: real environment variable > stability_monitor/.env > the
#     app's .env (read-only) > default."""

#     def __init__(self):
#         self._mon = dotenv_values(HERE / ".env")
#         self._app = dotenv_values(ROOT / ".env")

#         g = self._get
#         self.telegram_token = g("TELEGRAM_BOT_TOKEN")
#         self.telegram_chat_id = g("TELEGRAM_CHAT_ID")
#         self.telegram_api_base = g("TELEGRAM_API_BASE", DEFAULT_API_BASE)
#         self.server_name = g("SERVER_NAME") or socket.gethostname()

#         self.interval = self._num("MONITOR_INTERVAL", 60, int, 1)
#         self.health_url = g("HEALTH_URL", "http://127.0.0.1:8000/")
#         self.health_timeout = self._num("HEALTH_TIMEOUT", 10, int, 1)
#         self.app_fail_after = self._num("APP_FAILURE_THRESHOLD", 2, int, 1)

#         self.db_enabled = self._bool("DB_CHECK_ENABLED", True)
#         self.db_fail_after = self._num("DB_FAILURE_THRESHOLD", 2, int, 1)
#         self.db_host = g("POSTGRES_HOST", "localhost")
#         self.db_port = self._num("POSTGRES_PORT", 5432, int, 1)
#         self.db_name = g("POSTGRES_DB")
#         self.db_user = g("POSTGRES_USER")
#         self.db_password = g("POSTGRES_PASSWORD")

#         self.cpu_pct = self._num("CPU_WARNING_PERCENT", 90, float, 1)
#         self.ram_pct = self._num("RAM_WARNING_PERCENT", 90, float, 1)
#         self.disk_pct = self._num("DISK_WARNING_PERCENT", 90, float, 1)
#         self.res_checks = self._num("RESOURCE_CONSECUTIVE_CHECKS", 5, int, 1)   # CPU + RAM
#         self.disk_checks = self._num("DISK_CONSECUTIVE_CHECKS", 2, int, 1)
#         self.disk_path = g("DISK_PATH") or (ROOT.anchor or "/")

#         self.app_log = Path(g("APP_LOG_PATH") or (ROOT / "logs" / "app.log"))
#         self.stall_check = self._bool("BACKGROUND_STALL_CHECK", True)
#         self.bg_enabled = self._bool("ENABLE_BACKGROUND_PROCESSING", True)
#         bg_interval = self._num("BACKGROUND_PROCESSING_INTERVAL_MINUTES", 5, int, 1)
#         stall_override = self._num("BACKGROUND_STALL_MINUTES", 0, int, 0)
#         # A run can legitimately take several minutes (Gemini calls), so the
#         # automatic limit is generous: two full intervals plus 10 minutes.
#         self.stall_minutes = stall_override or (bg_interval * 2 + 10)
#         self.bg_interval = bg_interval

#         self.heartbeat_hours = self._num("HEARTBEAT_HOURS", 0, float, 0)

#     def _get(self, key, default=None):
#         for source in (os.environ, self._mon, self._app):
#             value = source.get(key)
#             if value not in (None, ""):
#                 return value
#         return default

#     def _num(self, key, default, cast, minimum):
#         raw = self._get(key, default)
#         try:
#             value = cast(raw)
#         except (TypeError, ValueError):
#             raise ConfigError(f"{key}={raw!r} is not a valid number")
#         if value < minimum:
#             raise ConfigError(f"{key}={raw!r} must be at least {minimum}")
#         return value

#     def _bool(self, key, default):
#         raw = self._get(key)
#         if raw is None:
#             return default
#         return str(raw).strip().lower() in ("1", "true", "yes", "on")

#     @property
#     def db_ready(self) -> bool:
#         return bool(self.db_enabled and self.db_name and self.db_user)


# # =====================================================================
# # Small helpers
# # =====================================================================
# def now() -> datetime:
#     return datetime.now()


# def stamp(dt: Optional[datetime] = None) -> str:
#     return (dt or now()).astimezone().strftime("%Y-%m-%d %H:%M:%S (UTC%z)")


# def fmt_duration(seconds: float) -> str:
#     seconds = int(max(seconds, 0))
#     h, rem = divmod(seconds, 3600)
#     m, s = divmod(rem, 60)
#     if h:
#         return f"{h}h {m:02d}m {s:02d}s"
#     if m:
#         return f"{m}m {s:02d}s"
#     return f"{s}s"


# def gb(num_bytes: float) -> str:
#     return f"{num_bytes / (1024 ** 3):.1f} GB"


# def first_line(text: str, limit: int = 300) -> str:
#     for line in str(text).splitlines():
#         if line.strip():
#             return line.strip()[:limit]
#     return str(text)[:limit]


# def read_tail(path: Path, nbytes: int = 512 * 1024) -> List[str]:
#     try:
#         size = path.stat().st_size
#         with open(path, "rb") as f:
#             f.seek(max(size - nbytes, 0))
#             data = f.read()
#         return data.decode("utf-8", errors="replace").splitlines()
#     except OSError:
#         return []


# RUN_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - \w+ - Background processing run (complete|failed)")


# def last_background_run(path: Path) -> Optional[datetime]:
#     """Time of the newest 'Background processing run complete/failed' line in
#     the app's log (the log uses the machine's local time)."""
#     for line in reversed(read_tail(path)):
#         m = RUN_LINE.match(line)
#         if m:
#             try:
#                 return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
#             except ValueError:
#                 return None
#     return None


# def app_log_hint(path: Path) -> str:
#     """Best-effort clue whether the app was stopped normally or died."""
#     lines = [ln for ln in read_tail(path, 64 * 1024) if ln.strip()]
#     if not lines:
#         return "app.log not found or empty"
#     if any("Shutting down" in ln for ln in lines[-8:]):
#         return "app.log ends with a normal shutdown message (stopped with Ctrl+C / normal shutdown / auto-reload)"
#     return f"app.log has NO shutdown message - likely crashed or was killed. Last line: {lines[-1][:200]}"


# @dataclass
# class Result:
#     ok: bool
#     kind: str = "ok"      # ok | down | timeout | unhealthy | error
#     detail: str = ""


# class Problem:
#     """Tracks one failure condition so that we alert ONCE when it starts and
#     ONCE when it ends - never once per check."""

#     def __init__(self, fail_after: int, recover_after: int = 1):
#         self.fail_after = fail_after
#         self.recover_after = recover_after
#         self.failed = False
#         self.since: Optional[datetime] = None
#         self.bad = 0
#         self.good = 0

#     def update(self, ok: bool) -> Optional[str]:
#         if ok:
#             self.bad = 0
#             self.good += 1
#             if self.failed and self.good >= self.recover_after:
#                 self.failed = False
#                 return "recovered"
#         else:
#             self.good = 0
#             self.bad += 1
#             if not self.failed and self.bad >= self.fail_after:
#                 self.failed = True
#                 self.since = now()
#                 return "started"
#         return None

#     def down_for(self) -> str:
#         return fmt_duration((now() - self.since).total_seconds()) if self.since else "?"


# class DryRunNotifier:
#     configured = True
#     last_error = None

#     def __init__(self, log):
#         self.log = log

#     def send(self, text: str, attempts: int = 3) -> bool:
#         self.log.info("[dry-run] would send Telegram: " + text.splitlines()[0])
#         print("\n----- [dry-run] Telegram message -----\n" + text + "\n--------------------------------------\n")
#         return True


# # =====================================================================
# # The monitor
# # =====================================================================
# class Monitor:
#     def __init__(self, cfg: Config, notifier, log: logging.Logger, simulate: Dict[str, float]):
#         self.cfg = cfg
#         self.notifier = notifier
#         self.log = log
#         self.sim_until = simulate            # name -> time.monotonic() deadline
#         self.started = now()
#         self.started_mono = time.monotonic()
#         self.stop_reason: Optional[str] = None

#         self.problems = {
#             "app": Problem(cfg.app_fail_after, 1),
#             "db": Problem(cfg.db_fail_after, 1),
#             "stall": Problem(2, 1),
#             "cpu": Problem(cfg.res_checks, 2),
#             "ram": Problem(cfg.res_checks, 2),
#             "disk": Problem(cfg.disk_checks, 2),
#             "monitor": Problem(3, 1),
#         }
#         self.last_app: Result = Result(True)
#         self.last_db: Optional[Result] = None
#         self.app_up_since: Optional[datetime] = None
#         self.log_offset: Optional[int] = None
#         self.outbox: Deque[str] = deque(maxlen=100)
#         self.mem_probe_ok = True
#         self.next_heartbeat = (
#             time.monotonic() + cfg.heartbeat_hours * 3600 if cfg.heartbeat_hours > 0 else None
#         )
#         self.stats = {
#             "checks": 0, "alerts_sent": 0, "telegram_failures": 0, "app_log_errors": 0,
#             "app_failures": 0, "db_failures": 0, "stall_events": 0, "internal_errors": 0,
#             "peak_cpu": 0.0, "peak_ram": 0.0, "peak_disk": 0.0, "peak_app_mem_mb": 0.0,
#         }
#         psutil.cpu_percent(interval=None)  # prime: first reading is meaningless

#     # ---- simulation ---------------------------------------------------
#     def simulating(self, name: str) -> bool:
#         return time.monotonic() < self.sim_until.get(name, 0)

#     def sim_note(self, name: str) -> str:
#         return "\nNote: SIMULATED TEST - nothing is actually wrong." if self.simulating(name) else ""

#     # ---- Telegram -----------------------------------------------------
#     def alert(self, text: str):
#         self.log.warning("ALERT: " + text.splitlines()[0] + " | " + " | ".join(
#             ln for ln in text.splitlines()[1:] if ln.startswith(("Problem", "Disk", "CPU", "RAM"))))
#         # 2 attempts only: a failed alert is queued in the outbox and retried
#         # on later cycles, so a Telegram outage must not stall the checks.
#         if self.notifier.send(text, attempts=2):
#             self.stats["alerts_sent"] += 1
#         else:
#             self.stats["telegram_failures"] += 1
#             self.log.error(f"Telegram send FAILED ({self.notifier.last_error}) - will retry")
#             self.outbox.append(text)

#     def flush_outbox(self):
#         while self.outbox:
#             msg = "⏱ Delayed alert (Telegram was unreachable when this happened)\n\n" + self.outbox[0]
#             if not self.notifier.send(msg, attempts=1):
#                 return
#             self.outbox.popleft()
#             self.stats["alerts_sent"] += 1
#             self.log.info("Delayed Telegram alert delivered")

#     def head(self, icon_title: str, problem: str, status: str = "FAILED", extra: str = "") -> str:
#         return (f"{icon_title}\n\nServer: {self.cfg.server_name}\nProblem: {problem}\n"
#                 f"Time: {stamp()}\nStatus: {status}{extra}")

#     # ---- individual checks -------------------------------------------
#     def check_app(self) -> Result:
#         if self.simulating("app"):
#             return Result(False, "down", "SIMULATED failure")
#         session = requests.Session()
#         session.trust_env = False   # a corporate proxy must not intercept a local check
#         try:
#             r = session.get(self.cfg.health_url, timeout=self.cfg.health_timeout)
#             if r.status_code == 200:
#                 return Result(True, "ok", f"HTTP 200 in {r.elapsed.total_seconds() * 1000:.0f}ms")
#             return Result(False, "unhealthy", f"HTTP {r.status_code} from {self.cfg.health_url}")
#         except requests.exceptions.Timeout:
#             return Result(False, "timeout", f"No response within {self.cfg.health_timeout}s")
#         except requests.exceptions.ConnectionError:
#             return Result(False, "down", "Connection refused - nothing is listening on that address")
#         except Exception as e:
#             return Result(False, "error", f"{type(e).__name__}: {first_line(e)}")

#     def check_db(self) -> Optional[Result]:
#         if not self.cfg.db_ready:
#             return None
#         if self.simulating("db"):
#             return Result(False, "down", "SIMULATED failure")
#         try:
#             import psycopg
#         except ImportError:
#             return None
#         t0 = time.monotonic()
#         try:
#             with psycopg.connect(
#                 host=self.cfg.db_host, port=self.cfg.db_port, dbname=self.cfg.db_name,
#                 user=self.cfg.db_user, password=self.cfg.db_password,
#                 connect_timeout=5, options="-c statement_timeout=5000",
#             ) as conn:
#                 with conn.cursor() as cur:
#                     cur.execute("SELECT 1")
#                     cur.fetchone()
#             return Result(True, "ok", f"{(time.monotonic() - t0) * 1000:.0f}ms")
#         except Exception as e:
#             msg = first_line(e)
#             if self.cfg.db_password:
#                 msg = msg.replace(self.cfg.db_password, "<password>")
#             return Result(False, "down", f"{type(e).__name__}: {msg}")

#     def check_stall(self) -> Optional[Result]:
#         if not (self.cfg.stall_check and self.cfg.bg_enabled):
#             return None
#         if self.simulating("stall"):
#             return Result(False, "stalled", "no run for 99 minutes (SIMULATED)")
#         last = last_background_run(self.cfg.app_log)
#         # Never blame the app for time it was not up: measure from the later
#         # of "last run" and "app became healthy".
#         reference = max([t for t in (last, self.app_up_since) if t], default=self.started)
#         age_min = (now() - reference).total_seconds() / 60
#         ok = age_min <= self.cfg.stall_minutes
#         return Result(ok, "ok" if ok else "stalled",
#                       f"last run {age_min:.0f} min ago (limit {self.cfg.stall_minutes} min)")

#     def read_resources(self):
#         cpu = 99.0 if self.simulating("cpu") else psutil.cpu_percent(interval=None)
#         mem = psutil.virtual_memory()
#         ram = 99.0 if self.simulating("ram") else mem.percent
#         try:
#             disk = psutil.disk_usage(self.cfg.disk_path)
#             disk_pct, disk_free = (99.0 if self.simulating("disk") else disk.percent), disk.free
#         except OSError as e:
#             self.log.error(f"Cannot read disk usage for {self.cfg.disk_path}: {e}")
#             disk_pct, disk_free = 0.0, 0
#         return cpu, ram, mem, disk_pct, disk_free

#     def app_memory_mb(self) -> Optional[float]:
#         """Memory of the process listening on the app's port (+ children).
#         Log-only: lets you see a slow memory leak over 24 hours."""
#         if not self.mem_probe_ok:
#             return None
#         parsed = urlparse(self.cfg.health_url)
#         if parsed.hostname not in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or not parsed.port:
#             return None
#         try:
#             for c in psutil.net_connections(kind="tcp"):
#                 if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == parsed.port and c.pid:
#                     p = psutil.Process(c.pid)
#                     total = p.memory_info().rss
#                     for child in p.children(recursive=True):
#                         try:
#                             total += child.memory_info().rss
#                         except psutil.Error:
#                             pass
#                     return total / (1024 * 1024)
#         except psutil.AccessDenied:
#             self.mem_probe_ok = False
#             self.log.info("App memory probe needs more permissions on this OS - disabled (not important).")
#         except Exception:
#             pass
#         return None

#     def scan_app_log_errors(self):
#         """Count NEW ERROR lines in app.log since the previous check."""
#         try:
#             size = self.cfg.app_log.stat().st_size
#         except OSError:
#             return 0, []
#         if self.log_offset is None:      # first look: start counting from now
#             self.log_offset = size
#             return 0, []
#         if size < self.log_offset:       # file was truncated/replaced
#             self.log_offset = 0
#         if size == self.log_offset:
#             return 0, []
#         with open(self.cfg.app_log, "rb") as f:
#             f.seek(self.log_offset)
#             data = f.read(2_000_000)
#         self.log_offset += len(data)
#         errors = [ln for ln in data.decode("utf-8", errors="replace").splitlines()
#                   if " - ERROR - " in ln or " - CRITICAL - " in ln]
#         return len(errors), errors[:3]

#     # ---- one full cycle ----------------------------------------------
#     def run_cycle(self):
#         cfg = self.cfg
#         self.flush_outbox()
#         self.stats["checks"] += 1

#         app = self.check_app()
#         self.last_app = app
#         if app.ok:
#             if self.app_up_since is None:
#                 self.app_up_since = now()
#         else:
#             self.app_up_since = None
#         ev = self.problems["app"].update(app.ok)
#         if ev == "started":
#             self.stats["app_failures"] += 1
#             title = {
#                 "down": "Application is not running (connection refused)",
#                 "timeout": "Application is not responding",
#                 "unhealthy": "Application health check failing",
#             }.get(app.kind, "Application health check failing")
#             self.alert(self.head("🚨 Invoice Automation Alert", title, "FAILED",
#                                  f"\nDetails: {app.detail}\nClue: {app_log_hint(cfg.app_log)}") + self.sim_note("app"))
#         elif ev == "recovered":
#             self.alert(f"✅ Application RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
#                        f"Status: HEALTHY\nWas down for: {self.problems['app'].down_for()}")

#         db = self.check_db()
#         self.last_db = db
#         if db is not None:
#             ev = self.problems["db"].update(db.ok)
#             if ev == "started":
#                 self.stats["db_failures"] += 1
#                 self.alert(self.head("🚨 Invoice Automation Alert", "PostgreSQL connection failed", "FAILED",
#                                      f"\nDetails: {db.detail}") + self.sim_note("db"))
#             elif ev == "recovered":
#                 self.alert(f"✅ PostgreSQL RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
#                            f"Status: HEALTHY\nWas down for: {self.problems['db'].down_for()}")

#         stall = self.check_stall() if app.ok else None
#         if stall is not None:
#             ev = self.problems["stall"].update(stall.ok)
#             if ev == "started":
#                 self.stats["stall_events"] += 1
#                 self.alert(self.head("🚨 Invoice Automation Alert",
#                                      "Application is up but background invoice processing has stopped completing runs",
#                                      "FAILED", f"\nDetails: {stall.detail}\n"
#                                      f"(runs are expected every {cfg.bg_interval} min; if you paused sync "
#                                      f"in the UI this is expected)") + self.sim_note("stall"))
#             elif ev == "recovered":
#                 self.alert(f"✅ Background processing RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
#                            f"Status: HEALTHY\nWas stalled for: {self.problems['stall'].down_for()}")

#         cpu, ram, mem, disk_pct, disk_free = self.read_resources()
#         st = self.stats
#         st["peak_cpu"], st["peak_ram"], st["peak_disk"] = max(st["peak_cpu"], cpu), max(st["peak_ram"], ram), max(st["peak_disk"], disk_pct)

#         self._resource(
#             "cpu", cpu >= cfg.cpu_pct,
#             f"CPU Usage: {cpu:.0f}% (over {cfg.cpu_pct:.0f}% for {cfg.res_checks} checks in a row)",
#             f"CPU usage back to normal ({cpu:.0f}%)")
#         self._resource(
#             "ram", ram >= cfg.ram_pct,
#             f"RAM Usage: {ram:.0f}% ({gb(mem.used)} of {gb(mem.total)}, over {cfg.ram_pct:.0f}% for {cfg.res_checks} checks in a row)",
#             f"RAM usage back to normal ({ram:.0f}%)")
#         self._resource(
#             "disk", disk_pct >= cfg.disk_pct,
#             f"Disk Usage: {disk_pct:.0f}%\nFree Space: {gb(disk_free)} on {cfg.disk_path}",
#             f"Disk usage back to normal ({disk_pct:.0f}%)")

#         app_mem = self.app_memory_mb()
#         if app_mem:
#             st["peak_app_mem_mb"] = max(st["peak_app_mem_mb"], app_mem)
#         new_errors, samples = self.scan_app_log_errors()
#         st["app_log_errors"] += new_errors
#         for s in samples:
#             self.log.warning("app.log ERROR: " + s[:250])

#         bg = last_background_run(cfg.app_log)
#         bg_txt = f"{(now() - bg).total_seconds() / 60:.0f}min ago" if bg else "n/a"
#         self.log.info(
#             f"app={'OK' if app.ok else 'FAIL(' + app.kind + ')'} "
#             f"db={'skipped' if db is None else ('OK ' + db.detail if db.ok else 'FAIL')} "
#             f"cpu={cpu:.0f}% ram={ram:.0f}% disk={disk_pct:.0f}%(free {gb(disk_free)}) "
#             f"app_mem={'%.0fMB' % app_mem if app_mem else 'n/a'} last_bg_run={bg_txt} new_app_errors={new_errors}"
#         )

#         if self.next_heartbeat and time.monotonic() >= self.next_heartbeat:
#             self.next_heartbeat = time.monotonic() + cfg.heartbeat_hours * 3600
#             self.notifier.send(self.heartbeat_text(cpu, ram, disk_pct))

#         self.write_state(running=True)

#     def _resource(self, key: str, exceeded: bool, warn_text: str, ok_text: str):
#         ev = self.problems[key].update(not exceeded)
#         note = self.sim_note(key)
#         if ev == "started":
#             self.alert(f"⚠️ Server Resource Warning\n\nServer: {self.cfg.server_name}\n{warn_text}\nTime: {stamp()}{note}")
#         elif ev == "recovered":
#             self.alert(f"✅ {ok_text}\n\nServer: {self.cfg.server_name}\nTime: {stamp()}\n"
#                        f"Was high for: {self.problems[key].down_for()}")

#     # ---- messages / state --------------------------------------------
#     def heartbeat_text(self, cpu, ram, disk) -> str:
#         s = self.stats
#         return (f"💓 Invoice Automation Monitor - still running\n\nServer: {self.cfg.server_name}\nTime: {stamp()}\n"
#                 f"Monitor uptime: {fmt_duration(time.monotonic() - self.started_mono)}\n"
#                 f"App: {'OK' if self.last_app.ok else 'FAILED'} | "
#                 f"PostgreSQL: {'skipped' if self.last_db is None else ('OK' if self.last_db.ok else 'FAILED')}\n"
#                 f"CPU {cpu:.0f}% | RAM {ram:.0f}% | Disk {disk:.0f}%\n"
#                 f"App failures so far: {s['app_failures']} | DB failures: {s['db_failures']} | "
#                 f"app.log errors: {s['app_log_errors']}")

#     def summary_text(self) -> str:
#         s = self.stats
#         mem = f", peak app memory {s['peak_app_mem_mb']:.0f} MB" if s["peak_app_mem_mb"] else ""
#         return (f"Ran for: {fmt_duration(time.monotonic() - self.started_mono)}\n"
#                 f"Checks: {s['checks']} | App failures: {s['app_failures']} | DB failures: {s['db_failures']} | "
#                 f"Stalls: {s['stall_events']}\n"
#                 f"Alerts sent: {s['alerts_sent']} | app.log errors seen: {s['app_log_errors']}\n"
#                 f"Peak CPU {s['peak_cpu']:.0f}% | peak RAM {s['peak_ram']:.0f}% | peak disk {s['peak_disk']:.0f}%{mem}")

#     def write_state(self, running: bool):
#         try:
#             tmp = STATE_FILE.with_suffix(".tmp")
#             tmp.write_text(json.dumps({
#                 "running": running, "started": self.started.isoformat(timespec="seconds"),
#                 "last_beat": now().isoformat(timespec="seconds"), "pid": os.getpid(),
#             }), encoding="utf-8")
#             os.replace(tmp, STATE_FILE)
#         except OSError as e:
#             self.log.warning(f"Could not write state file: {e}")


# # =====================================================================
# # Startup / shutdown
# # =====================================================================
# def previous_session_died() -> Optional[str]:
#     """If the last monitor run never wrote its 'clean stop' marker, it was
#     killed, crashed, the terminal was closed, or the machine lost power."""
#     try:
#         data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
#     except (OSError, ValueError):
#         return None
#     return data.get("last_beat") if data.get("running") else None


# def startup_text(m: Monitor, prev_beat: Optional[str], sims: List[str], sim_minutes: float) -> str:
#     c = m.cfg
#     db = f"{c.db_host}:{c.db_port}/{c.db_name}" if c.db_ready else "not checked"
#     lines = [
#         "✅ Invoice Automation Monitor Started", "",
#         f"Server: {c.server_name}", f"Time: {stamp()}", "",
#         f"Checking every {c.interval}s",
#         f"App: {c.health_url} (alert after {c.app_fail_after} failed checks)",
#         f"PostgreSQL: {db}",
#         f"Background-run watch: " + (f"alert if no run for {c.stall_minutes} min" if c.stall_check and c.bg_enabled else "off"),
#         f"Limits: CPU {c.cpu_pct:.0f}% / RAM {c.ram_pct:.0f}% ({c.res_checks} checks in a row) / Disk {c.disk_pct:.0f}%",
#         f"Heartbeat: " + (f"every {c.heartbeat_hours:g} h" if c.heartbeat_hours else "off"),
#     ]
#     if prev_beat:
#         lines += ["", f"⚠️ The previous monitor session did NOT stop cleanly (last seen {prev_beat}). "
#                       "It was killed, crashed, or the machine/terminal went down."]
#     if sims:
#         lines += ["", f"🧪 SIMULATING failures for {sim_minutes:g} min: {', '.join(sims)} - alerts that follow are TESTS."]
#     return "\n".join(lines)


# def setup_logger() -> logging.Logger:
#     (ROOT / "logs").mkdir(exist_ok=True)
#     log = logging.getLogger("stability_monitor")
#     log.setLevel(logging.INFO)
#     log.propagate = False   # do not touch the application's logging setup
#     fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
#     fh = RotatingFileHandler(ROOT / "logs" / "monitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
#     ch = logging.StreamHandler(sys.stdout)
#     for h in (fh, ch):
#         h.setFormatter(fmt)
#         log.addHandler(h)
#     return log


# def check_now(cfg: Config):
#     """One pass of every check, printed. No Telegram, no state changes."""
#     log = logging.getLogger("check_now")
#     m = Monitor(cfg, DryRunNotifier(log), log, {})
#     print(f"\nServer name : {cfg.server_name}")
#     app = m.check_app()
#     print(f"Application: {'OK' if app.ok else 'FAILED (' + app.kind + ')'} - {app.detail}   [{cfg.health_url}]")
#     if not app.ok:
#         print(f"             clue: {app_log_hint(cfg.app_log)}")
#     db = m.check_db()
#     if db is None:
#         print("PostgreSQL : skipped (disabled, not configured, or psycopg missing)")
#     else:
#         print(f"PostgreSQL : {'OK' if db.ok else 'FAILED'} - {db.detail}   [{cfg.db_host}:{cfg.db_port}/{cfg.db_name}]")
#     bg = last_background_run(cfg.app_log)
#     print("Last background run in app.log: " + (bg.strftime("%Y-%m-%d %H:%M:%S") if bg else "none found")
#           + f"   (stall limit {cfg.stall_minutes} min, watch {'on' if cfg.stall_check and cfg.bg_enabled else 'off'})")
#     cpu = psutil.cpu_percent(interval=1)
#     mem = psutil.virtual_memory()
#     du = psutil.disk_usage(cfg.disk_path)
#     print(f"CPU        : {cpu:.0f}%  (limit {cfg.cpu_pct:.0f}%)")
#     print(f"RAM        : {mem.percent:.0f}%  {gb(mem.used)} of {gb(mem.total)}  (limit {cfg.ram_pct:.0f}%)")
#     print(f"Disk       : {du.percent:.0f}%  free {gb(du.free)} on {cfg.disk_path}  (limit {cfg.disk_pct:.0f}%)")
#     mb = m.app_memory_mb()
#     print(f"App memory : {'%.0f MB' % mb if mb else 'n/a'}")
#     print(f"Telegram   : {'token + chat id present' if cfg.telegram_token and cfg.telegram_chat_id else 'NOT configured (stability_monitor/.env)'}\n")


# def main() -> int:
#     ap = argparse.ArgumentParser(description="Invoice Automation stability-test watchdog")
#     ap.add_argument("--check-now", action="store_true", help="run every check once, print the result, exit")
#     ap.add_argument("--simulate", default="", help=f"comma list of fake failures to inject: {','.join(sorted(SIMULATABLE))}")
#     ap.add_argument("--simulate-minutes", type=float, default=8, help="how long simulated failures last (default 8)")
#     ap.add_argument("--interval", type=int, help="override MONITOR_INTERVAL (seconds) - handy for quick tests")
#     ap.add_argument("--no-telegram", action="store_true", help="dry run: print alerts instead of sending them")
#     args = ap.parse_args()

#     try:
#         cfg = Config()
#         if args.interval:
#             cfg.interval = max(args.interval, 1)
#     except ConfigError as e:
#         print(f"CONFIG ERROR: {e}")
#         return 2

#     if args.check_now:
#         check_now(cfg)
#         return 0

#     sims = [s.strip() for s in args.simulate.split(",") if s.strip()]
#     bad = [s for s in sims if s not in SIMULATABLE]
#     if bad:
#         print(f"Unknown --simulate value(s): {', '.join(bad)}. Choose from: {', '.join(sorted(SIMULATABLE))}")
#         return 2

#     log = setup_logger()
#     notifier = DryRunNotifier(log) if args.no_telegram else TelegramNotifier(
#         cfg.telegram_token, cfg.telegram_chat_id, cfg.telegram_api_base)
#     if not notifier.configured:
#         print("CONFIG ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set.\n"
#               "Fill them in stability_monitor/.env (copy .env.example), or use --no-telegram for a dry run.")
#         return 2

#     sim_until = {s: time.monotonic() + args.simulate_minutes * 60 for s in sims}
#     m = Monitor(cfg, notifier, log, sim_until)

#     def _terminate(signum, _frame):
#         m.stop_reason = f"stopped by signal {signum}"
#     for name in ("SIGTERM", "SIGBREAK"):          # SIGBREAK = Ctrl+Break on Windows
#         if hasattr(signal, name):
#             signal.signal(getattr(signal, name), _terminate)

#     prev = previous_session_died()
#     if not notifier.send(startup_text(m, prev, sims, args.simulate_minutes)):
#         print(f"\nERROR: the startup message could not be sent to Telegram: {notifier.last_error}\n"
#               "Fix the token/chat id/internet access first (python stability_monitor/telegram_notifier.py test).")
#         return 2
#     log.info(f"Monitor started on '{cfg.server_name}', interval {cfg.interval}s, "
#              f"health={cfg.health_url}, simulate={sims or 'none'}")
#     if prev:
#         log.warning(f"Previous monitor session ended unexpectedly (last seen {prev})")
#     m.write_state(running=True)

#     exit_code = 0
#     try:
#         next_at = time.monotonic()
#         while not m.stop_reason:
#             try:
#                 m.run_cycle()
#                 if m.problems["monitor"].update(True) == "recovered":
#                     m.alert(f"✅ Monitor internal error RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}")
#             except Exception as e:      # a bug in the monitor must not silently kill it
#                 m.stats["internal_errors"] += 1
#                 log.error("Internal error in check cycle:\n" + traceback.format_exc())
#                 if m.problems["monitor"].update(False) == "started":
#                     m.alert(m.head("🚨 Invoice Automation Alert", "The monitor itself is failing its checks",
#                                    "FAILED", f"\nDetails: {type(e).__name__}: {first_line(e)}\nSee logs/monitor.log"))
#             next_at += cfg.interval
#             while not m.stop_reason and time.monotonic() < next_at:
#                 time.sleep(min(1.0, max(next_at - time.monotonic(), 0)))
#             if next_at < time.monotonic() - cfg.interval:   # laptop slept / process was frozen - do not burst
#                 next_at = time.monotonic()
#         reason = m.stop_reason
#     except KeyboardInterrupt:
#         reason = "stopped manually (Ctrl+C)"
#     except Exception as e:
#         log.error("MONITOR CRASHED:\n" + traceback.format_exc())
#         notifier.send(f"💥 Invoice Automation Monitor CRASHED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
#                       f"Error: {type(e).__name__}: {first_line(e)}\n\n"
#                       "The monitor is NO LONGER watching your application.")
#         return 1

#     log.info(f"Monitor stopping: {reason}")
#     m.write_state(running=False)
#     notifier.send(f"🛑 Invoice Automation Monitor Stopped\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
#                   f"Reason: {reason}\n\n{m.summary_text()}")
#     log.info("Session summary: " + m.summary_text().replace("\n", " | "))
#     return exit_code


# if __name__ == "__main__":
#     sys.exit(main())




"""
Invoice Automation - temporary stability-test watchdog.

Runs as its OWN process, completely separate from the invoice application.
It never imports the application, never writes to its files, never talks to
Microsoft Graph and never calls Gemini. It only:

  * checks the app over HTTP            (GET http://127.0.0.1:8000/)
  * checks PostgreSQL                   (connect + SELECT 1, read-only)
  * reads logs/app.log                  (read-only: is the background loop still
                                         completing runs? any new ERROR lines?)
  * checks CPU / RAM / disk             (psutil)
  * sends Telegram messages             (one when a problem starts, one when it ends)

Usage (from the project folder, with the project's virtual environment):

    python stability_monitor/monitor.py                       run the monitor
    python stability_monitor/monitor.py --check-now           one check, printed, no Telegram
    python stability_monitor/monitor.py --simulate app,db     fake failures to test alerts
    python stability_monitor/monitor.py --no-telegram         dry run (alerts printed, not sent)

Settings: stability_monitor/.env (see .env.example). The app's own .env is
only READ, to reuse the PostgreSQL connection details.
"""
import argparse
import ast
import json
import logging
import os
import re
import signal
import socket
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, List, Optional
from urllib.parse import urlparse

import psutil
import requests
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from telegram_notifier import DEFAULT_API_BASE, TelegramNotifier  # noqa: E402

try:  # never crash on a Windows console that cannot print a character
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

SIMULATABLE = {"app", "db", "cpu", "ram", "disk", "stall", "runs"}
STATE_FILE = HERE / "monitor_state.json"


# =====================================================================
# Configuration
# =====================================================================
class ConfigError(Exception):
    pass


class Config:
    """Lookup order: real environment variable > stability_monitor/.env > the
    app's .env (read-only) > default."""

    def __init__(self):
        self._mon = dotenv_values(HERE / ".env")
        self._app = dotenv_values(ROOT / ".env")

        g = self._get
        self.telegram_token = g("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = g("TELEGRAM_CHAT_ID")
        self.telegram_api_base = g("TELEGRAM_API_BASE", DEFAULT_API_BASE)
        self.server_name = g("SERVER_NAME") or socket.gethostname()

        self.interval = self._num("MONITOR_INTERVAL", 60, int, 1)
        self.health_url = g("HEALTH_URL", "http://127.0.0.1:8000/")
        self.health_timeout = self._num("HEALTH_TIMEOUT", 10, int, 1)
        self.app_fail_after = self._num("APP_FAILURE_THRESHOLD", 2, int, 1)

        self.db_enabled = self._bool("DB_CHECK_ENABLED", True)
        self.db_fail_after = self._num("DB_FAILURE_THRESHOLD", 2, int, 1)
        self.db_host = g("POSTGRES_HOST", "localhost")
        self.db_port = self._num("POSTGRES_PORT", 5432, int, 1)
        self.db_name = g("POSTGRES_DB")
        self.db_user = g("POSTGRES_USER")
        self.db_password = g("POSTGRES_PASSWORD")
        self.db_sslmode = g("POSTGRES_SSLMODE")

        self.cpu_pct = self._num("CPU_WARNING_PERCENT", 90, float, 1)
        self.ram_pct = self._num("RAM_WARNING_PERCENT", 90, float, 1)
        self.disk_pct = self._num("DISK_WARNING_PERCENT", 90, float, 1)
        self.res_checks = self._num("RESOURCE_CONSECUTIVE_CHECKS", 5, int, 1)   # CPU + RAM
        self.disk_checks = self._num("DISK_CONSECUTIVE_CHECKS", 2, int, 1)
        self.disk_path = g("DISK_PATH") or (ROOT.anchor or "/")

        self.app_log = Path(g("APP_LOG_PATH") or (ROOT / "logs" / "app.log"))
        self.stall_check = self._bool("BACKGROUND_STALL_CHECK", True)
        self.bg_enabled = self._bool("ENABLE_BACKGROUND_PROCESSING", True)
        bg_interval = self._num("BACKGROUND_PROCESSING_INTERVAL_MINUTES", 5, int, 1)
        stall_override = self._num("BACKGROUND_STALL_MINUTES", 0, int, 0)
        # A run can legitimately take several minutes (Gemini calls), so the
        # automatic limit is generous: two full intervals plus 10 minutes.
        self.stall_minutes = stall_override or (bg_interval * 2 + 10)
        self.bg_interval = bg_interval

        self.heartbeat_hours = self._num("HEARTBEAT_HOURS", 0, float, 0)

        # Simple "email processed" messages, built from the app's own log lines.
        self.notify_runs = self._bool("NOTIFY_PROCESSED_EMAILS", True)
        self.notify_empty = self._bool("NOTIFY_NO_INVOICE_EMAILS", True)

    def _get(self, key, default=None):
        for source in (os.environ, self._mon, self._app):
            value = source.get(key)
            if value not in (None, ""):
                return value
        return default

    def _num(self, key, default, cast, minimum):
        raw = self._get(key, default)
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"{key}={raw!r} is not a valid number")
        if value < minimum:
            raise ConfigError(f"{key}={raw!r} must be at least {minimum}")
        return value

    def _bool(self, key, default):
        raw = self._get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    @property
    def db_ready(self) -> bool:
        return bool(self.db_enabled and self.db_name and self.db_user)


# =====================================================================
# Small helpers
# =====================================================================
def now() -> datetime:
    return datetime.now()


def stamp(dt: Optional[datetime] = None) -> str:
    return (dt or now()).astimezone().strftime("%Y-%m-%d %H:%M:%S (UTC%z)")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def gb(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GB"


def first_line(text: str, limit: int = 300) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()[:limit]
    return str(text)[:limit]


def read_tail(path: Path, nbytes: int = 512 * 1024) -> List[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(size - nbytes, 0))
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


RUN_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - \w+ - Background processing run (complete|failed)")


def last_background_run(path: Path) -> Optional[datetime]:
    """Time of the newest 'Background processing run complete/failed' line in
    the app's log (the log uses the machine's local time)."""
    for line in reversed(read_tail(path)):
        m = RUN_LINE.match(line)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    return None


def app_log_hint(path: Path) -> str:
    """Best-effort clue whether the app was stopped normally or died."""
    lines = [ln for ln in read_tail(path, 64 * 1024) if ln.strip()]
    if not lines:
        return "app.log not found or empty"
    if any("Shutting down" in ln for ln in lines[-8:]):
        return "app.log ends with a normal shutdown message (stopped with Ctrl+C / normal shutdown / auto-reload)"
    return f"app.log has NO shutdown message - likely crashed or was killed. Last line: {lines[-1][:200]}"


RUN_SUMMARY = re.compile(r"Background processing run complete: (\{.*\})\s*$")
RUN_FAILED = re.compile(r" - ERROR - Background processing run failed: (.*)$")


def parse_run_line(line: str) -> Optional[dict]:
    """Turn one app.log line into a run summary dict, or None if it is not one.
    Complete run -> the app's own counters. Failed run -> {"failed": "<reason>"}."""
    m = RUN_FAILED.search(line)
    if m:
        return {"failed": m.group(1).strip()[:300]}
    m = RUN_SUMMARY.search(line)
    if not m:
        return None
    try:
        data = ast.literal_eval(m.group(1))
        return data if isinstance(data, dict) else None
    except (ValueError, SyntaxError):
        out = {}
        for key in ("emails_processed", "attachments_processed", "invoices_extracted",
                    "invoices_stored", "documents_skipped_duplicate"):
            k = re.search(rf"'{key}': (\d+)", m.group(1))
            out[key] = int(k.group(1)) if k else 0
        out["errors"] = [] if "'errors': []" in m.group(1) else ["(see logs/app.log)"]
        return out


@dataclass
class Result:
    ok: bool
    kind: str = "ok"      # ok | down | timeout | unhealthy | error
    detail: str = ""


class Problem:
    """Tracks one failure condition so that we alert ONCE when it starts and
    ONCE when it ends - never once per check."""

    def __init__(self, fail_after: int, recover_after: int = 1):
        self.fail_after = fail_after
        self.recover_after = recover_after
        self.failed = False
        self.since: Optional[datetime] = None
        self.bad = 0
        self.good = 0

    def update(self, ok: bool) -> Optional[str]:
        if ok:
            self.bad = 0
            self.good += 1
            if self.failed and self.good >= self.recover_after:
                self.failed = False
                return "recovered"
        else:
            self.good = 0
            self.bad += 1
            if not self.failed and self.bad >= self.fail_after:
                self.failed = True
                self.since = now()
                return "started"
        return None

    def down_for(self) -> str:
        return fmt_duration((now() - self.since).total_seconds()) if self.since else "?"


class DryRunNotifier:
    configured = True
    last_error = None

    def __init__(self, log):
        self.log = log

    def send(self, text: str, attempts: int = 3) -> bool:
        self.log.info("[dry-run] would send Telegram: " + text.splitlines()[0])
        print("\n----- [dry-run] Telegram message -----\n" + text + "\n--------------------------------------\n")
        return True


# =====================================================================
# The monitor
# =====================================================================
class Monitor:
    def __init__(self, cfg: Config, notifier, log: logging.Logger, simulate: Dict[str, float]):
        self.cfg = cfg
        self.notifier = notifier
        self.log = log
        self.sim_until = simulate            # name -> time.monotonic() deadline
        self.started = now()
        self.started_mono = time.monotonic()
        self.stop_reason: Optional[str] = None

        self.problems = {
            "app": Problem(cfg.app_fail_after, 1),
            "db": Problem(cfg.db_fail_after, 1),
            "stall": Problem(2, 1),
            "cpu": Problem(cfg.res_checks, 2),
            "ram": Problem(cfg.res_checks, 2),
            "disk": Problem(cfg.disk_checks, 2),
            "monitor": Problem(3, 1),
        }
        self.last_app: Result = Result(True)
        self.last_db: Optional[Result] = None
        self.app_up_since: Optional[datetime] = None
        self.log_offset: Optional[int] = None
        self.outbox: Deque[str] = deque(maxlen=100)
        self.mem_probe_ok = True
        self.next_heartbeat = (
            time.monotonic() + cfg.heartbeat_hours * 3600 if cfg.heartbeat_hours > 0 else None
        )
        self.stats = {
            "checks": 0, "alerts_sent": 0, "telegram_failures": 0, "app_log_errors": 0,
            "app_failures": 0, "db_failures": 0, "stall_events": 0, "internal_errors": 0,
            "peak_cpu": 0.0, "peak_ram": 0.0, "peak_disk": 0.0, "peak_app_mem_mb": 0.0,
            "emails": 0, "invoices_found": 0, "invoices_stored": 0, "run_reports": 0, "failed_runs": 0,
        }
        self._sim_run_sent = False
        psutil.cpu_percent(interval=None)  # prime: first reading is meaningless

    # ---- simulation ---------------------------------------------------
    def simulating(self, name: str) -> bool:
        return time.monotonic() < self.sim_until.get(name, 0)

    def sim_note(self, name: str) -> str:
        return "\nNote: SIMULATED TEST - nothing is actually wrong." if self.simulating(name) else ""

    # ---- Telegram -----------------------------------------------------
    def alert(self, text: str, kind: str = "ALERT"):
        self.log.warning(f"{kind}: " + text.splitlines()[0] + " | " + " | ".join(
            ln for ln in text.splitlines()[1:] if ln.startswith(("Problem", "Disk", "CPU", "RAM", "Stored", "Result"))))
        # 2 attempts only: a failed alert is queued in the outbox and retried
        # on later cycles, so a Telegram outage must not stall the checks.
        if self.notifier.send(text, attempts=2):
            self.stats["alerts_sent"] += 1
        else:
            self.stats["telegram_failures"] += 1
            self.log.error(f"Telegram send FAILED ({self.notifier.last_error}) - will retry")
            self.outbox.append(text)

    def flush_outbox(self):
        while self.outbox:
            msg = "⏱ Delayed alert (Telegram was unreachable when this happened)\n\n" + self.outbox[0]
            if not self.notifier.send(msg, attempts=1):
                return
            self.outbox.popleft()
            self.stats["alerts_sent"] += 1
            self.log.info("Delayed Telegram alert delivered")

    def head(self, icon_title: str, problem: str, status: str = "FAILED", extra: str = "") -> str:
        return (f"{icon_title}\n\nServer: {self.cfg.server_name}\nProblem: {problem}\n"
                f"Time: {stamp()}\nStatus: {status}{extra}")

    # ---- individual checks -------------------------------------------
    def check_app(self) -> Result:
        if self.simulating("app"):
            return Result(False, "down", "SIMULATED failure")
        session = requests.Session()
        session.trust_env = False   # a corporate proxy must not intercept a local check
        try:
            r = session.get(self.cfg.health_url, timeout=self.cfg.health_timeout)
            if r.status_code == 200:
                return Result(True, "ok", f"HTTP 200 in {r.elapsed.total_seconds() * 1000:.0f}ms")
            return Result(False, "unhealthy", f"HTTP {r.status_code} from {self.cfg.health_url}")
        except requests.exceptions.Timeout:
            return Result(False, "timeout", f"No response within {self.cfg.health_timeout}s")
        except requests.exceptions.ConnectionError:
            return Result(False, "down", "Connection refused - nothing is listening on that address")
        except Exception as e:
            return Result(False, "error", f"{type(e).__name__}: {first_line(e)}")

    def check_db(self) -> Optional[Result]:
        if not self.cfg.db_ready:
            return None
        if self.simulating("db"):
            return Result(False, "down", "SIMULATED failure")
        try:
            import psycopg
        except ImportError:
            return None
        t0 = time.monotonic()
        try:
            # NOTE: no `options=` startup parameter - Neon's pooled (-pooler)
            # endpoint rejects it. The timeout is set with a normal SET below.
            kwargs = dict(
                host=self.cfg.db_host, port=self.cfg.db_port, dbname=self.cfg.db_name,
                user=self.cfg.db_user, password=self.cfg.db_password,
                connect_timeout=5,
            )
            if self.cfg.db_sslmode:
                kwargs["sslmode"] = self.cfg.db_sslmode
            with psycopg.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 5000")
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return Result(True, "ok", f"{(time.monotonic() - t0) * 1000:.0f}ms")
        except Exception as e:
            msg = first_line(e)
            if self.cfg.db_password:
                msg = msg.replace(self.cfg.db_password, "<password>")
            return Result(False, "down", f"{type(e).__name__}: {msg}")

    def check_stall(self) -> Optional[Result]:
        if not (self.cfg.stall_check and self.cfg.bg_enabled):
            return None
        if self.simulating("stall"):
            return Result(False, "stalled", "no run for 99 minutes (SIMULATED)")
        last = last_background_run(self.cfg.app_log)
        # Never blame the app for time it was not up: measure from the later
        # of "last run" and "app became healthy".
        reference = max([t for t in (last, self.app_up_since) if t], default=self.started)
        age_min = (now() - reference).total_seconds() / 60
        ok = age_min <= self.cfg.stall_minutes
        return Result(ok, "ok" if ok else "stalled",
                      f"last run {age_min:.0f} min ago (limit {self.cfg.stall_minutes} min)")

    def read_resources(self):
        cpu = 99.0 if self.simulating("cpu") else psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        ram = 99.0 if self.simulating("ram") else mem.percent
        try:
            disk = psutil.disk_usage(self.cfg.disk_path)
            disk_pct, disk_free = (99.0 if self.simulating("disk") else disk.percent), disk.free
        except OSError as e:
            self.log.error(f"Cannot read disk usage for {self.cfg.disk_path}: {e}")
            disk_pct, disk_free = 0.0, 0
        return cpu, ram, mem, disk_pct, disk_free

    def app_memory_mb(self) -> Optional[float]:
        """Memory of the process listening on the app's port (+ children).
        Log-only: lets you see a slow memory leak over 24 hours."""
        if not self.mem_probe_ok:
            return None
        parsed = urlparse(self.cfg.health_url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or not parsed.port:
            return None
        try:
            for c in psutil.net_connections(kind="tcp"):
                if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == parsed.port and c.pid:
                    p = psutil.Process(c.pid)
                    total = p.memory_info().rss
                    for child in p.children(recursive=True):
                        try:
                            total += child.memory_info().rss
                        except psutil.Error:
                            pass
                    return total / (1024 * 1024)
        except psutil.AccessDenied:
            self.mem_probe_ok = False
            self.log.info("App memory probe needs more permissions on this OS - disabled (not important).")
        except Exception:
            pass
        return None

    def scan_app_log(self):
        """Read NEW app.log lines since the previous check.
        Returns (error_count, sample_error_lines, run_summary_dicts)."""
        try:
            size = self.cfg.app_log.stat().st_size
        except OSError:
            return 0, [], []
        if self.log_offset is None:      # first look: start counting from now
            self.log_offset = size
            return 0, [], []
        if size < self.log_offset:       # file was truncated/replaced
            self.log_offset = 0
        if size == self.log_offset:
            return 0, [], []
        with open(self.cfg.app_log, "rb") as f:
            f.seek(self.log_offset)
            data = f.read(2_000_000)
        cut = data.rfind(b"\n")
        if cut < 0:                      # a line is still being written - wait for the rest
            return 0, [], []
        data = data[:cut + 1]
        self.log_offset += len(data)
        lines = data.decode("utf-8", errors="replace").splitlines()
        errors = [ln for ln in lines if " - ERROR - " in ln or " - CRITICAL - " in ln]
        runs = [r for r in (parse_run_line(ln) for ln in lines) if r]
        return len(errors), errors[:3], runs

    def report_run(self, run: dict, simulated: bool = False):
        """One short Telegram message per finished processing run."""
        st, cfg = self.stats, self.cfg
        note = "\nNote: SIMULATED TEST - not a real email." if simulated else ""
        head = f"Server: {cfg.server_name}\nTime: {stamp()}"

        if run.get("failed"):
            st["failed_runs"] += 1
            self.alert(f"❌ Invoice processing FAILED\n\n{head}\nResult: the run crashed before finishing\n"
                       f"Error: {run['failed']}\nSee logs/app.log{note}", kind="RUN")
            return

        emails = int(run.get("emails_processed", 0) or 0)
        atts = int(run.get("attachments_processed", 0) or 0)
        found = int(run.get("invoices_extracted", 0) or 0)
        stored = int(run.get("invoices_stored", 0) or 0)
        dup_docs = int(run.get("documents_skipped_duplicate", 0) or 0)
        errors = run.get("errors") or []
        st["emails"] += emails
        st["invoices_found"] += found
        st["invoices_stored"] += stored

        if not (emails or atts or found or dup_docs or errors):
            return   # an empty check - nothing arrived, stay silent
        if not cfg.notify_runs:
            return

        counts = (f"Emails: {emails} | Attachments: {atts}\n"
                  f"Invoices found: {found}\nStored in database: {stored}")
        already = max(found - stored, 0)
        if errors:
            shown = "\n".join("  - " + (str(e.get("error", e)) if isinstance(e, dict) else str(e))[:160] for e in errors[:3])
            title, result = "❌ Invoice processing had ERRORS", f"{len(errors)} error(s):\n{shown}"
        elif found and stored == found:
            title, result = "✅ Invoice processed", "SUCCESS - all invoices stored"
        elif found and stored:
            title, result = "✅ Invoice processed", f"stored {stored}; {already} already in database (duplicates skipped)"
        elif found and not stored:
            title, result = "ℹ️ Invoices already in database", f"nothing new stored ({already} duplicate invoice(s))"
        elif dup_docs:
            title, result = "ℹ️ Duplicate document skipped", "this exact email/attachment was already processed"
        else:
            if not cfg.notify_empty:
                return
            title, result = "⚠️ Mail checked - no invoices found", "the email had no invoice data (or Gemini found none)"
        if dup_docs and found:
            counts += f"\nDuplicate documents skipped: {dup_docs}"
        st["run_reports"] += 1
        self.alert(f"{title}\n\n{head}\n{counts}\nResult: {result}{note}", kind="RUN")

    # ---- one full cycle ----------------------------------------------
    def run_cycle(self):
        cfg = self.cfg
        self.flush_outbox()
        self.stats["checks"] += 1

        app = self.check_app()
        self.last_app = app
        if app.ok:
            if self.app_up_since is None:
                self.app_up_since = now()
        else:
            self.app_up_since = None
        ev = self.problems["app"].update(app.ok)
        if ev == "started":
            self.stats["app_failures"] += 1
            title = {
                "down": "Application is not running (connection refused)",
                "timeout": "Application is not responding",
                "unhealthy": "Application health check failing",
            }.get(app.kind, "Application health check failing")
            self.alert(self.head("🚨 Invoice Automation Alert", title, "FAILED",
                                 f"\nDetails: {app.detail}\nClue: {app_log_hint(cfg.app_log)}") + self.sim_note("app"))
        elif ev == "recovered":
            self.alert(f"✅ Application RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
                       f"Status: HEALTHY\nWas down for: {self.problems['app'].down_for()}")

        db = self.check_db()
        self.last_db = db
        if db is not None:
            ev = self.problems["db"].update(db.ok)
            if ev == "started":
                self.stats["db_failures"] += 1
                self.alert(self.head("🚨 Invoice Automation Alert", "PostgreSQL connection failed", "FAILED",
                                     f"\nDetails: {db.detail}") + self.sim_note("db"))
            elif ev == "recovered":
                self.alert(f"✅ PostgreSQL RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
                           f"Status: HEALTHY\nWas down for: {self.problems['db'].down_for()}")

        stall = self.check_stall() if app.ok else None
        if stall is not None:
            ev = self.problems["stall"].update(stall.ok)
            if ev == "started":
                self.stats["stall_events"] += 1
                self.alert(self.head("🚨 Invoice Automation Alert",
                                     "Application is up but background invoice processing has stopped completing runs",
                                     "FAILED", f"\nDetails: {stall.detail}\n"
                                     f"(runs are expected every {cfg.bg_interval} min; if you paused sync "
                                     f"in the UI this is expected)") + self.sim_note("stall"))
            elif ev == "recovered":
                self.alert(f"✅ Background processing RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
                           f"Status: HEALTHY\nWas stalled for: {self.problems['stall'].down_for()}")

        cpu, ram, mem, disk_pct, disk_free = self.read_resources()
        st = self.stats
        st["peak_cpu"], st["peak_ram"], st["peak_disk"] = max(st["peak_cpu"], cpu), max(st["peak_ram"], ram), max(st["peak_disk"], disk_pct)

        self._resource(
            "cpu", cpu >= cfg.cpu_pct,
            f"CPU Usage: {cpu:.0f}% (over {cfg.cpu_pct:.0f}% for {cfg.res_checks} checks in a row)",
            f"CPU usage back to normal ({cpu:.0f}%)")
        self._resource(
            "ram", ram >= cfg.ram_pct,
            f"RAM Usage: {ram:.0f}% ({gb(mem.used)} of {gb(mem.total)}, over {cfg.ram_pct:.0f}% for {cfg.res_checks} checks in a row)",
            f"RAM usage back to normal ({ram:.0f}%)")
        self._resource(
            "disk", disk_pct >= cfg.disk_pct,
            f"Disk Usage: {disk_pct:.0f}%\nFree Space: {gb(disk_free)} on {cfg.disk_path}",
            f"Disk usage back to normal ({disk_pct:.0f}%)")

        app_mem = self.app_memory_mb()
        if app_mem:
            st["peak_app_mem_mb"] = max(st["peak_app_mem_mb"], app_mem)
        new_errors, samples, runs = self.scan_app_log()
        st["app_log_errors"] += new_errors
        for sample in samples:
            self.log.warning("app.log ERROR: " + sample[:250])
        if self.simulating("runs") and not self._sim_run_sent:
            self._sim_run_sent = True
            runs.append({"emails_processed": 1, "attachments_processed": 1, "invoices_extracted": 3,
                         "invoices_stored": 3, "documents_skipped_duplicate": 0, "errors": []})
            self.report_run(runs.pop(), simulated=True)
        for run in runs:
            self.report_run(run)

        bg = last_background_run(cfg.app_log)
        bg_txt = f"{(now() - bg).total_seconds() / 60:.0f}min ago" if bg else "n/a"
        self.log.info(
            f"app={'OK' if app.ok else 'FAIL(' + app.kind + ')'} "
            f"db={'skipped' if db is None else ('OK ' + db.detail if db.ok else 'FAIL')} "
            f"cpu={cpu:.0f}% ram={ram:.0f}% disk={disk_pct:.0f}%(free {gb(disk_free)}) "
            f"app_mem={'%.0fMB' % app_mem if app_mem else 'n/a'} last_bg_run={bg_txt} new_app_errors={new_errors}"
        )

        if self.next_heartbeat and time.monotonic() >= self.next_heartbeat:
            self.next_heartbeat = time.monotonic() + cfg.heartbeat_hours * 3600
            self.notifier.send(self.heartbeat_text(cpu, ram, disk_pct))

        self.write_state(running=True)

    def _resource(self, key: str, exceeded: bool, warn_text: str, ok_text: str):
        ev = self.problems[key].update(not exceeded)
        note = self.sim_note(key)
        if ev == "started":
            self.alert(f"⚠️ Server Resource Warning\n\nServer: {self.cfg.server_name}\n{warn_text}\nTime: {stamp()}{note}")
        elif ev == "recovered":
            self.alert(f"✅ {ok_text}\n\nServer: {self.cfg.server_name}\nTime: {stamp()}\n"
                       f"Was high for: {self.problems[key].down_for()}")

    # ---- messages / state --------------------------------------------
    def heartbeat_text(self, cpu, ram, disk) -> str:
        s = self.stats
        return (f"💓 Invoice Automation Monitor - still running\n\nServer: {self.cfg.server_name}\nTime: {stamp()}\n"
                f"Monitor uptime: {fmt_duration(time.monotonic() - self.started_mono)}\n"
                f"App: {'OK' if self.last_app.ok else 'FAILED'} | "
                f"PostgreSQL: {'skipped' if self.last_db is None else ('OK' if self.last_db.ok else 'FAILED')}\n"
                f"CPU {cpu:.0f}% | RAM {ram:.0f}% | Disk {disk:.0f}%\n"
                f"App failures so far: {s['app_failures']} | DB failures: {s['db_failures']} | "
                f"app.log errors: {s['app_log_errors']}")

    def summary_text(self) -> str:
        s = self.stats
        mem = f", peak app memory {s['peak_app_mem_mb']:.0f} MB" if s["peak_app_mem_mb"] else ""
        return (f"Ran for: {fmt_duration(time.monotonic() - self.started_mono)}\n"
                f"Checks: {s['checks']} | App failures: {s['app_failures']} | DB failures: {s['db_failures']} | "
                f"Stalls: {s['stall_events']}\n"
                f"Alerts sent: {s['alerts_sent']} | app.log errors seen: {s['app_log_errors']}\n"
                f"Mail processed: {s['emails']} email(s) | invoices found {s['invoices_found']}, "
                f"stored {s['invoices_stored']} | failed runs: {s['failed_runs']}\n"
                f"Peak CPU {s['peak_cpu']:.0f}% | peak RAM {s['peak_ram']:.0f}% | peak disk {s['peak_disk']:.0f}%{mem}")

    def write_state(self, running: bool):
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "running": running, "started": self.started.isoformat(timespec="seconds"),
                "last_beat": now().isoformat(timespec="seconds"), "pid": os.getpid(),
            }), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            self.log.warning(f"Could not write state file: {e}")


# =====================================================================
# Startup / shutdown
# =====================================================================
def previous_session_died() -> Optional[str]:
    """If the last monitor run never wrote its 'clean stop' marker, it was
    killed, crashed, the terminal was closed, or the machine lost power."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("last_beat") if data.get("running") else None


def startup_text(m: Monitor, prev_beat: Optional[str], sims: List[str], sim_minutes: float) -> str:
    c = m.cfg
    db = f"{c.db_host}:{c.db_port}/{c.db_name}" if c.db_ready else "not checked"
    lines = [
        "✅ Invoice Automation Monitor Started", "",
        f"Server: {c.server_name}", f"Time: {stamp()}", "",
        f"Checking every {c.interval}s",
        f"App: {c.health_url} (alert after {c.app_fail_after} failed checks)",
        f"PostgreSQL: {db}",
        f"Background-run watch: " + (f"alert if no run for {c.stall_minutes} min" if c.stall_check and c.bg_enabled else "off"),
        f"Limits: CPU {c.cpu_pct:.0f}% / RAM {c.ram_pct:.0f}% ({c.res_checks} checks in a row) / Disk {c.disk_pct:.0f}%",
        f"Heartbeat: " + (f"every {c.heartbeat_hours:g} h" if c.heartbeat_hours else "off"),
        f"Invoice-processed messages: " + ("on (a short message after each mail is processed)" if c.notify_runs else "off"),
    ]
    if prev_beat:
        lines += ["", f"⚠️ The previous monitor session did NOT stop cleanly (last seen {prev_beat}). "
                      "It was killed, crashed, or the machine/terminal went down."]
    if sims:
        lines += ["", f"🧪 SIMULATING failures for {sim_minutes:g} min: {', '.join(sims)} - alerts that follow are TESTS."]
    return "\n".join(lines)


def setup_logger() -> logging.Logger:
    (ROOT / "logs").mkdir(exist_ok=True)
    log = logging.getLogger("stability_monitor")
    log.setLevel(logging.INFO)
    log.propagate = False   # do not touch the application's logging setup
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(ROOT / "logs" / "monitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ch = logging.StreamHandler(sys.stdout)
    for h in (fh, ch):
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


def check_now(cfg: Config):
    """One pass of every check, printed. No Telegram, no state changes."""
    log = logging.getLogger("check_now")
    m = Monitor(cfg, DryRunNotifier(log), log, {})
    print(f"\nServer name : {cfg.server_name}")
    app = m.check_app()
    print(f"Application: {'OK' if app.ok else 'FAILED (' + app.kind + ')'} - {app.detail}   [{cfg.health_url}]")
    if not app.ok:
        print(f"             clue: {app_log_hint(cfg.app_log)}")
    db = m.check_db()
    if db is None:
        print("PostgreSQL : skipped (disabled, not configured, or psycopg missing)")
    else:
        print(f"PostgreSQL : {'OK' if db.ok else 'FAILED'} - {db.detail}   [{cfg.db_host}:{cfg.db_port}/{cfg.db_name}]")
    bg = last_background_run(cfg.app_log)
    print("Last background run in app.log: " + (bg.strftime("%Y-%m-%d %H:%M:%S") if bg else "none found")
          + f"   (stall limit {cfg.stall_minutes} min, watch {'on' if cfg.stall_check and cfg.bg_enabled else 'off'})")
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    du = psutil.disk_usage(cfg.disk_path)
    print(f"CPU        : {cpu:.0f}%  (limit {cfg.cpu_pct:.0f}%)")
    print(f"RAM        : {mem.percent:.0f}%  {gb(mem.used)} of {gb(mem.total)}  (limit {cfg.ram_pct:.0f}%)")
    print(f"Disk       : {du.percent:.0f}%  free {gb(du.free)} on {cfg.disk_path}  (limit {cfg.disk_pct:.0f}%)")
    mb = m.app_memory_mb()
    print(f"App memory : {'%.0f MB' % mb if mb else 'n/a'}")
    print(f"Telegram   : {'token + chat id present' if cfg.telegram_token and cfg.telegram_chat_id else 'NOT configured (stability_monitor/.env)'}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Invoice Automation stability-test watchdog")
    ap.add_argument("--check-now", action="store_true", help="run every check once, print the result, exit")
    ap.add_argument("--simulate", default="", help=f"comma list of fake failures to inject: {','.join(sorted(SIMULATABLE))}")
    ap.add_argument("--simulate-minutes", type=float, default=8, help="how long simulated failures last (default 8)")
    ap.add_argument("--interval", type=int, help="override MONITOR_INTERVAL (seconds) - handy for quick tests")
    ap.add_argument("--no-telegram", action="store_true", help="dry run: print alerts instead of sending them")
    args = ap.parse_args()

    try:
        cfg = Config()
        if args.interval:
            cfg.interval = max(args.interval, 1)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        return 2

    if args.check_now:
        check_now(cfg)
        return 0

    sims = [s.strip() for s in args.simulate.split(",") if s.strip()]
    bad = [s for s in sims if s not in SIMULATABLE]
    if bad:
        print(f"Unknown --simulate value(s): {', '.join(bad)}. Choose from: {', '.join(sorted(SIMULATABLE))}")
        return 2

    log = setup_logger()
    notifier = DryRunNotifier(log) if args.no_telegram else TelegramNotifier(
        cfg.telegram_token, cfg.telegram_chat_id, cfg.telegram_api_base)
    if not notifier.configured:
        print("CONFIG ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set.\n"
              "Fill them in stability_monitor/.env (copy .env.example), or use --no-telegram for a dry run.")
        return 2

    sim_until = {s: time.monotonic() + args.simulate_minutes * 60 for s in sims}
    m = Monitor(cfg, notifier, log, sim_until)

    def _terminate(signum, _frame):
        m.stop_reason = f"stopped by signal {signum}"
    for name in ("SIGTERM", "SIGBREAK"):          # SIGBREAK = Ctrl+Break on Windows
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _terminate)

    prev = previous_session_died()
    if not notifier.send(startup_text(m, prev, sims, args.simulate_minutes)):
        print(f"\nERROR: the startup message could not be sent to Telegram: {notifier.last_error}\n"
              "Fix the token/chat id/internet access first (python stability_monitor/telegram_notifier.py test).")
        return 2
    log.info(f"Monitor started on '{cfg.server_name}', interval {cfg.interval}s, "
             f"health={cfg.health_url}, simulate={sims or 'none'}")
    if prev:
        log.warning(f"Previous monitor session ended unexpectedly (last seen {prev})")
    m.write_state(running=True)

    exit_code = 0
    try:
        next_at = time.monotonic()
        while not m.stop_reason:
            try:
                m.run_cycle()
                if m.problems["monitor"].update(True) == "recovered":
                    m.alert(f"✅ Monitor internal error RECOVERED\n\nServer: {cfg.server_name}\nTime: {stamp()}")
            except Exception as e:      # a bug in the monitor must not silently kill it
                m.stats["internal_errors"] += 1
                log.error("Internal error in check cycle:\n" + traceback.format_exc())
                if m.problems["monitor"].update(False) == "started":
                    m.alert(m.head("🚨 Invoice Automation Alert", "The monitor itself is failing its checks",
                                   "FAILED", f"\nDetails: {type(e).__name__}: {first_line(e)}\nSee logs/monitor.log"))
            next_at += cfg.interval
            while not m.stop_reason and time.monotonic() < next_at:
                time.sleep(min(1.0, max(next_at - time.monotonic(), 0)))
            if next_at < time.monotonic() - cfg.interval:   # laptop slept / process was frozen - do not burst
                next_at = time.monotonic()
        reason = m.stop_reason
    except KeyboardInterrupt:
        reason = "stopped manually (Ctrl+C)"
    except Exception as e:
        log.error("MONITOR CRASHED:\n" + traceback.format_exc())
        notifier.send(f"💥 Invoice Automation Monitor CRASHED\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
                      f"Error: {type(e).__name__}: {first_line(e)}\n\n"
                      "The monitor is NO LONGER watching your application.")
        return 1

    log.info(f"Monitor stopping: {reason}")
    m.write_state(running=False)
    notifier.send(f"🛑 Invoice Automation Monitor Stopped\n\nServer: {cfg.server_name}\nTime: {stamp()}\n"
                  f"Reason: {reason}\n\n{m.summary_text()}")
    log.info("Session summary: " + m.summary_text().replace("\n", " | "))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())