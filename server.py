"""
SCAI Executive Dashboard — Backend Server
==========================================
Local:  python server.py            → http://localhost:8000
Cloud:  set env vars, platform auto-starts via Procfile

Environment variables (all optional — sensible defaults apply):
  PORT             Port to listen on (cloud platforms set this automatically)
  DATA_DIR         Where to store DB + uploads (default: ~/SCAI_Dashboard_Data)
  DASHBOARD_PWD    Password to protect the dashboard (default: no password)
  DASHBOARD_USR    Username for basic auth (default: scai)
  EMAIL_FROM       Gmail address to send from (e.g. you@gmail.com)
  EMAIL_APP_PWD    Gmail App Password (16-char, spaces removed)
  SCAI_HEAD_EMAIL  Recipient for weekly summary email
  OWNER_EMAILS     JSON mapping vertical→email e.g. {"ICT":"m@nmdc.sa","AI & Data":"a@nmdc.sa"}
  DASHBOARD_URL    Public URL of this dashboard (for email links)

Requires: pip install fastapi uvicorn openpyxl python-multipart anthropic
"""

import os
import json
import sqlite3
import shutil
import logging
import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from scai_parser import parse_any_excel, extract_pid_from_filename

# ── Config (all from environment, with safe defaults) ─────────────────────────
BASE_DIR = Path(__file__).parent
DASHBOARD = BASE_DIR / 'SCAI_Visual_Dashboard.html'
PORT = int(os.environ.get('PORT', 8000))

# Data directory: env var → fallback to home folder (handles FUSE/network mounts)
_data_env = os.environ.get('DATA_DIR', '')
if _data_env:
    DATA_DIR = Path(_data_env)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
else:
    def _writable_dir(preferred: Path) -> Path:
        try:
            test = preferred / '.write_test'
            test.write_text('ok')
            test.unlink()
            return preferred
        except OSError:
            fallback = Path.home() / 'SCAI_Dashboard_Data'
            fallback.mkdir(exist_ok=True)
            return fallback
    DATA_DIR = _writable_dir(BASE_DIR)

DB_PATH    = DATA_DIR / 'scai_data.db'
UPLOAD_DIR = DATA_DIR / 'uploaded_files'
UPLOAD_DIR.mkdir(exist_ok=True)

# Auth — admin credentials (full access)
AUTH_USER = os.environ.get('DASHBOARD_USR', 'scai')
AUTH_PWD  = os.environ.get('DASHBOARD_PWD', '')   # empty = no auth
AUTH_ON   = bool(AUTH_PWD)

# Auth — owner credentials (read dashboard + submit weekly updates only)
OWNER_USR = os.environ.get('OWNER_USR', 'owner')
OWNER_PWD = os.environ.get('OWNER_PWD', '')       # empty = owner login disabled
OWNER_ON  = bool(OWNER_PWD)

# Email config (optional — only active when EMAIL_FROM + EMAIL_APP_PWD are set)
EMAIL_FROM      = os.environ.get('EMAIL_FROM', '')
EMAIL_APP_PWD   = os.environ.get('EMAIL_APP_PWD', '').replace(' ', '')
SCAI_HEAD_EMAIL = os.environ.get('SCAI_HEAD_EMAIL', EMAIL_FROM)
DASHBOARD_URL   = os.environ.get('DASHBOARD_URL', 'https://web-production-228d9.up.railway.app')
_owner_emails_raw = os.environ.get('OWNER_EMAILS', '{}')
try:
    OWNER_EMAILS = json.loads(_owner_emails_raw)
except Exception:
    OWNER_EMAILS = {}
EMAIL_ON = bool(EMAIL_FROM and EMAIL_APP_PWD)

# AI config (optional — enables AI summary and risk prediction)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
AI_ON = bool(ANTHROPIC_API_KEY)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('scai')

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title='SCAI Dashboard API', version='1.0', docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

security = HTTPBasic(auto_error=False)


def _creds_match(creds: HTTPBasicCredentials, usr: str, pwd: str) -> bool:
    return (secrets.compare_digest(creds.username.encode(), usr.encode()) and
            secrets.compare_digest(creds.password.encode(), pwd.encode()))


def get_role(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Returns 'admin' or 'owner'. Raises 401/403 on bad/missing credentials."""
    if not AUTH_ON and not OWNER_ON:
        return 'admin'   # no auth configured → open access
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Basic realm="SCAI Dashboard"'},
        )
    if AUTH_ON and _creds_match(credentials, AUTH_USER, AUTH_PWD):
        return 'admin'
    if OWNER_ON and _creds_match(credentials, OWNER_USR, OWNER_PWD):
        return 'owner'
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail='Incorrect username or password',
        headers={'WWW-Authenticate': 'Basic realm="SCAI Dashboard"'},
    )


def require_auth(role: str = Depends(get_role)) -> str:
    """Allows admin or owner."""
    return role


def require_admin(role: str = Depends(get_role)) -> str:
    """Admin-only routes (upload, delete)."""
    if role != 'admin':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='Admin access required')
    return role


# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS projects (
        id          TEXT PRIMARY KEY,
        data        TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS upload_log (
        log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        filename    TEXT NOT NULL,
        project_id  TEXT,
        file_type   TEXT,
        uploaded_at TEXT NOT NULL,
        status      TEXT NOT NULL,
        message     TEXT
    )""")
    conn.commit()
    conn.close()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_all_projects():
    with get_conn() as conn:
        rows = conn.execute('SELECT data FROM projects ORDER BY id').fetchall()
    return [json.loads(r['data']) for r in rows]


def get_project(pid):
    with get_conn() as conn:
        row = conn.execute('SELECT data FROM projects WHERE id = ?', (pid,)).fetchone()
    return json.loads(row['data']) if row else None


def upsert_project(proj):
    with get_conn() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO projects (id, data, updated_at) VALUES (?, ?, ?)',
            (proj['id'], json.dumps(proj), datetime.utcnow().isoformat())
        )


def log_upload(filename, project_id, file_type, status_str, message=''):
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO upload_log (filename, project_id, file_type, uploaded_at, status, message) VALUES (?,?,?,?,?,?)',
            (filename, project_id, file_type, datetime.utcnow().isoformat(), status_str, message)
        )


def merge_project_data(existing, baseline=None, weekly=None):
    proj = existing.copy() if existing else {}

    if baseline:
        for key in ['id', 'name', 'vertical', 'owner', 'startDate', 'targetEnd',
                    'desc', 'budget', 'phase', 'milestones', 'plannedProgress']:
            if key in baseline:
                proj[key] = baseline[key]
        if 'weeklyUpdates' not in proj:
            proj['weeklyUpdates'] = []
        if 'milestoneTracker' not in proj:
            proj['milestoneTracker'] = []

    if weekly:
        existing_tracker = {t['milestoneId']: t for t in proj.get('milestoneTracker', [])}
        for t in weekly.get('milestoneTracker', []):
            existing_tracker[t['milestoneId']] = t
        proj['milestoneTracker'] = list(existing_tracker.values())

        existing_weeks = {u['weekDate'] for u in proj.get('weeklyUpdates', [])}
        new_updates = [u for u in weekly.get('weeklyUpdates', []) if u['weekDate'] not in existing_weeks]
        proj['weeklyUpdates'] = proj.get('weeklyUpdates', []) + new_updates
        proj['weeklyUpdates'].sort(key=lambda u: u.get('weekDate', ''))

    return proj


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def serve_dashboard(_: None = Depends(require_auth)):
    if not DASHBOARD.exists():
        raise HTTPException(404, 'Dashboard file not found')
    return HTMLResponse(DASHBOARD.read_text(encoding='utf-8'))


@app.get('/api/projects', dependencies=[Depends(require_auth)])
async def api_get_projects():
    return get_all_projects()


@app.get('/api/projects/{pid}', dependencies=[Depends(require_auth)])
async def api_get_project(pid: str):
    proj = get_project(pid.upper())
    if not proj:
        raise HTTPException(404, f'Project {pid} not found')
    return proj


@app.delete('/api/projects/{pid}', dependencies=[Depends(require_admin)])
async def api_delete_project(pid: str):
    pid = pid.upper()
    with get_conn() as conn:
        result = conn.execute('DELETE FROM projects WHERE id = ?', (pid,))
        if result.rowcount == 0:
            raise HTTPException(404, f'Project {pid} not found')
    log.info(f'Deleted project {pid}')
    return {'deleted': pid}


@app.post('/api/upload', dependencies=[Depends(require_admin)])
async def api_upload(files: list[UploadFile] = File(...)):
    results, errors = [], []

    for upload in files:
        fname = upload.filename or 'unknown.xlsx'
        pid_hint = extract_pid_from_filename(fname)

        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        saved_path = UPLOAD_DIR / f'{ts}_{fname}'
        try:
            with saved_path.open('wb') as f:
                shutil.copyfileobj(upload.file, f)
        except Exception as e:
            errors.append({'file': fname, 'error': f'Could not save file: {e}'})
            continue

        try:
            file_type, parsed = parse_any_excel(str(saved_path), pid_hint)
        except Exception as e:
            log.warning(f'Parse error {fname}: {e}')
            log_upload(fname, pid_hint, 'unknown', 'error', str(e))
            errors.append({'file': fname, 'error': f'Parse failed: {e}'})
            continue

        if file_type == 'unknown' or parsed is None:
            log_upload(fname, pid_hint, 'unknown', 'error', 'Could not identify file type')
            errors.append({'file': fname, 'error': 'Could not identify as Baseline or WeeklyReport'})
            continue

        if file_type == 'baseline':
            pid = (parsed.get('id') or pid_hint or '').upper()
            if not pid:
                errors.append({'file': fname, 'error': 'No project ID found in file'})
                continue
            parsed['id'] = pid
            existing = get_project(pid)
            merged = merge_project_data(existing, baseline=parsed)
            upsert_project(merged)
            log_upload(fname, pid, 'baseline', 'ok', f'Saved project {pid}')
            results.append({'file': fname, 'type': 'baseline', 'projectId': pid, 'name': parsed.get('name', '')})
            log.info(f'Baseline uploaded: {pid} ({fname})')

        elif file_type == 'weekly':
            pid = pid_hint.upper()
            if not pid:
                errors.append({'file': fname, 'error': 'Name file like P001_WeeklyReport.xlsx so the project ID can be detected'})
                continue
            existing = get_project(pid)
            if not existing:
                existing = {'id': pid, 'name': pid, 'vertical': '', 'owner': '',
                            'milestones': [], 'weeklyUpdates': [], 'milestoneTracker': [],
                            'plannedProgress': []}
            existing_dates = {u['weekDate'] for u in existing.get('weeklyUpdates', [])}
            merged = merge_project_data(existing, weekly=parsed)
            upsert_project(merged)
            n_new   = len([u for u in parsed.get('weeklyUpdates', []) if u['weekDate'] not in existing_dates])
            n_total = len(parsed.get('weeklyUpdates', []))
            log_upload(fname, pid, 'weekly', 'ok', f'{n_new} new / {n_total} total week updates')
            results.append({'file': fname, 'type': 'weekly', 'projectId': pid,
                            'weeksAdded': n_new, 'weeksInFile': n_total})
            log.info(f'WeeklyReport uploaded: {pid} — {n_new} new updates ({fname})')

    return {'saved': len(results), 'errors': len(errors), 'results': results, 'errorDetails': errors}


@app.get('/api/upload-log', dependencies=[Depends(require_admin)])
async def api_upload_log(limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT * FROM upload_log ORDER BY log_id DESC LIMIT ?', (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/role')
async def api_role(role: str = Depends(get_role)):
    """Returns the current user's role so the frontend can adjust the UI."""
    return {'role': role}


@app.post('/api/weekly-update')
async def api_weekly_update(payload: dict, role: str = Depends(require_auth)):
    """Owner or admin: submit / overwrite this week's project update."""
    pid = (payload.get('projectId') or '').upper()
    if not pid:
        raise HTTPException(400, 'projectId is required')

    proj = get_project(pid)
    if not proj:
        raise HTTPException(404, f'Project {pid} not found')

    week_date = payload.get('weekDate', '')
    if not week_date:
        raise HTTPException(400, 'weekDate is required (YYYY-MM-DD)')

    has_blocker = bool(payload.get('hasBlocker', False))
    new_update = {
        'weekDate':     week_date,
        'progress':     int(payload.get('progress', 0)),
        'comment':      payload.get('comment', ''),
        'thisWeek':     payload.get('comment', ''),   # shown in "This Week" block
        'nextWeek':     payload.get('nextWeek', ''),
        'hasBlocker':   has_blocker,
        'blockerDetail': payload.get('blockerDetail', '') if has_blocker else '',
    }

    # Upsert into weeklyUpdates (replace entry for same weekDate if exists)
    updates = proj.get('weeklyUpdates', [])
    idx = next((i for i, u in enumerate(updates) if u.get('weekDate') == week_date), None)
    if idx is not None:
        updates[idx] = new_update
    else:
        updates.append(new_update)
    updates.sort(key=lambda u: u.get('weekDate', ''))
    proj['weeklyUpdates'] = updates

    # Upsert milestone tracker entries
    tracker = {t['milestoneId']: t for t in proj.get('milestoneTracker', [])}
    for m in payload.get('milestones', []):
        mid = m.get('milestoneId')
        if not mid:
            continue
        entry = {'milestoneId': mid, 'status': m.get('status', 'Not Started')}
        if m.get('status') == 'Complete':
            entry['completedDate'] = week_date
        elif mid in tracker and tracker[mid].get('status') == 'Complete':
            entry['completedDate'] = tracker[mid].get('completedDate', '')
        tracker[mid] = entry
    proj['milestoneTracker'] = list(tracker.values())

    upsert_project(proj)
    log.info(f'Weekly update submitted: {pid} week={week_date} role={role}')
    return {'ok': True, 'projectId': pid, 'weekDate': week_date}


@app.get('/api/status')   # no auth — used as health check by cloud platforms
async def api_status():
    projects = get_all_projects()
    return {
        'status':     'ok',
        'projects':   len(projects),
        'authEnabled': AUTH_ON,
        'serverTime': datetime.utcnow().isoformat(),
    }


# ── Email helpers ─────────────────────────────────────────────────────────────
def send_email(to: str, subject: str, html_body: str):
    """Send an HTML email via Gmail SMTP. Raises if email not configured."""
    if not EMAIL_ON:
        raise RuntimeError('Email not configured. Set EMAIL_FROM and EMAIL_APP_PWD env vars.')
    msg = MIMEMultipart('alternative')
    msg['From']    = EMAIL_FROM
    msg['To']      = to
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(EMAIL_FROM, EMAIL_APP_PWD)
        server.sendmail(EMAIL_FROM, to, msg.as_string())


def build_weekly_summary_html(projects: list) -> str:
    """Generate a clean HTML email body for the weekly portfolio summary."""
    total = len(projects)
    by_rag = {'On Track': 0, 'At Risk': 0, 'Blocked': 0, 'Complete': 0}
    blockers = []
    achievements = []
    vertical_summaries = {}

    for p in projects:
        updates = sorted(p.get('weeklyUpdates', []), key=lambda u: u.get('weekDate',''))
        latest  = updates[-1] if updates else None
        rag     = latest.get('ragOverride') or ('Blocked' if latest and latest.get('hasBlocker') else 'On Track') if latest else 'Not Started'
        by_rag[rag] = by_rag.get(rag, 0) + 1

        v = p.get('vertical', 'Unknown')
        if v not in vertical_summaries:
            vertical_summaries[v] = {'total': 0, 'blocked': 0, 'at_risk': 0, 'on_track': 0}
        vertical_summaries[v]['total'] += 1
        if rag == 'Blocked':  vertical_summaries[v]['blocked'] += 1
        elif rag == 'At Risk': vertical_summaries[v]['at_risk'] += 1
        else:                  vertical_summaries[v]['on_track'] += 1

        if latest and latest.get('hasBlocker') and latest.get('blockerDetail'):
            blockers.append({'project': p.get('name',''), 'vertical': v,
                             'owner': p.get('owner',''), 'detail': latest['blockerDetail']})
        if latest and latest.get('thisWeek'):
            achievements.append({'project': p.get('name',''), 'text': latest['thisWeek']})

    week_str = datetime.utcnow().strftime('%d %b %Y')
    rag_color = {'On Track':'#29BA74','At Risk':'#F89862','Blocked':'#E71C57','Complete':'#30C1D7'}

    rows_vert = ''.join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:600">{v}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{d["total"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#E71C57;text-align:center">{d["blocked"] or "—"}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#F89862;text-align:center">{d["at_risk"] or "—"}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#29BA74;text-align:center">{d["on_track"] or "—"}</td>'
        f'</tr>'
        for v, d in vertical_summaries.items()
    )
    rows_blockers = ''.join(
        f'<li style="margin-bottom:8px"><strong>{b["project"]}</strong> ({b["vertical"]})<br>'
        f'<span style="color:#555">{b["detail"]}</span><br>'
        f'<span style="color:#999;font-size:12px">Owner: {b["owner"]}</span></li>'
        for b in blockers
    ) or '<li style="color:#29BA74">No active blockers this week ✓</li>'

    rows_achieve = ''.join(
        f'<li style="margin-bottom:6px"><strong>{a["project"]}</strong>: {a["text"]}</li>'
        for a in achievements[:8]
    ) or '<li>No updates submitted yet</li>'

    _fallback_color = '#9A9A9A'
    kpi_cells = ''.join(
        f'<td style="text-align:center;padding:12px 16px">'
        f'<div style="font-size:28px;font-weight:900;color:{rag_color.get(k, _fallback_color)}">{v}</div>'
        f'<div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#999;margin-top:3px">{k}</div>'
        f'</td>'
        for k, v in by_rag.items()
    )

    return f"""
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;background:#f5f5f5">
<table width="100%" cellpadding="0" cellspacing="0">
  <tr><td style="background:#1A1A2E;padding:24px 28px">
    <div style="color:#C9A84C;font-size:20px;font-weight:800">SCAI Weekly Portfolio Report</div>
    <div style="color:#C9A84C;opacity:.6;font-size:12px;margin-top:4px">Week of {week_str} &nbsp;·&nbsp; New Murabba Development Company</div>
  </td></tr>
  <tr><td style="background:#fff;padding:20px 28px">
    <table width="100%" style="border-collapse:collapse;margin-bottom:20px">
      <tr style="background:#f9f9f9">{kpi_cells}
        <td style="text-align:center;padding:12px 16px">
          <div style="font-size:28px;font-weight:900;color:#C9A84C">{total}</div>
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#999;margin-top:3px">Total</div>
        </td>
      </tr>
    </table>
    <h3 style="color:#1A1A2E;border-bottom:2px solid #C9A84C;padding-bottom:6px">Vertical Summary</h3>
    <table width="100%" style="border-collapse:collapse;font-size:13px">
      <tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left">Vertical</th>
        <th style="padding:8px 12px">Projects</th>
        <th style="padding:8px 12px;color:#E71C57">Blocked</th>
        <th style="padding:8px 12px;color:#F89862">At Risk</th>
        <th style="padding:8px 12px;color:#29BA74">On Track</th>
      </tr>{rows_vert}
    </table>
    <h3 style="color:#1A1A2E;border-bottom:2px solid #E71C57;padding-bottom:6px;margin-top:20px">⚠ Active Blockers</h3>
    <ul style="padding-left:18px;font-size:13px">{rows_blockers}</ul>
    <h3 style="color:#1A1A2E;border-bottom:2px solid #29BA74;padding-bottom:6px;margin-top:20px">✅ This Week's Achievements</h3>
    <ul style="padding-left:18px;font-size:13px">{rows_achieve}</ul>
    <div style="margin-top:24px;text-align:center">
      <a href="{DASHBOARD_URL}" style="background:#C9A84C;color:#1A1A2E;padding:10px 24px;border-radius:6px;text-decoration:none;font-weight:700;font-size:13px">View Full Dashboard →</a>
    </div>
  </td></tr>
  <tr><td style="background:#1A1A2E;padding:12px 28px;text-align:center">
    <div style="color:#C9A84C;opacity:.5;font-size:10px">SCAI Visual Analytics · Confidential · {datetime.utcnow().strftime("%d %b %Y")}</div>
  </td></tr>
</table></body></html>"""


@app.post('/api/send-weekly-report', dependencies=[Depends(require_auth)])
async def api_send_weekly_report():
    """Send the weekly portfolio summary email to the SCAI head."""
    if not EMAIL_ON:
        raise HTTPException(400, 'Email not configured. Set EMAIL_FROM and EMAIL_APP_PWD in Railway env vars.')
    if not SCAI_HEAD_EMAIL:
        raise HTTPException(400, 'SCAI_HEAD_EMAIL not configured.')
    projects = get_all_projects()
    html = build_weekly_summary_html(projects)
    week_str = datetime.utcnow().strftime('%d %b %Y')
    subject = f'SCAI Weekly Portfolio Report — Week of {week_str}'
    send_email(SCAI_HEAD_EMAIL, subject, html)
    log.info(f'Weekly report sent to {SCAI_HEAD_EMAIL}')
    return {'sent': True, 'to': SCAI_HEAD_EMAIL, 'subject': subject}


@app.post('/api/send-reminders', dependencies=[Depends(require_auth)])
async def api_send_reminders():
    """Send reminder emails to project owners to submit their weekly update."""
    if not EMAIL_ON:
        raise HTTPException(400, 'Email not configured. Set EMAIL_FROM and EMAIL_APP_PWD in Railway env vars.')
    if not OWNER_EMAILS:
        raise HTTPException(400, 'OWNER_EMAILS not configured. Set as JSON in Railway env vars.')

    week_str = datetime.utcnow().strftime('%d %b %Y')
    sent, errors = [], []
    for vertical, email in OWNER_EMAILS.items():
        projects = [p for p in get_all_projects() if p.get('vertical') == vertical]
        proj_list = ''.join(f'<li>{p.get("name","")}</li>' for p in projects)
        html = f"""
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
<table width="100%"><tr><td style="background:#1A1A2E;padding:20px 24px">
  <div style="color:#C9A84C;font-size:18px;font-weight:800">Weekly Update Reminder</div>
  <div style="color:#C9A84C;opacity:.6;font-size:12px">SCAI Portfolio Dashboard · {week_str}</div>
</td></tr>
<tr><td style="padding:24px;background:#fff">
  <p style="font-size:14px;color:#333">Hi,</p>
  <p style="font-size:14px;color:#333">This is your weekly reminder to submit progress updates for your <strong>{vertical}</strong> projects before end of day today.</p>
  <p style="font-size:13px;color:#555">Your projects:</p>
  <ul style="font-size:13px;color:#333">{proj_list}</ul>
  <p style="font-size:13px;color:#555">Please upload your <strong>WeeklyReport.xlsx</strong> file via the dashboard:</p>
  <div style="text-align:center;margin:20px 0">
    <a href="{DASHBOARD_URL}" style="background:#C9A84C;color:#1A1A2E;padding:10px 24px;border-radius:6px;text-decoration:none;font-weight:700;font-size:13px">Open Dashboard →</a>
  </div>
  <p style="font-size:11px;color:#999">If you have already submitted your update this week, please disregard this message.</p>
</td></tr>
<tr><td style="background:#1A1A2E;padding:10px 24px;text-align:center">
  <div style="color:#C9A84C;opacity:.4;font-size:10px">SCAI Visual Analytics · New Murabba Development Company</div>
</td></tr></table></body></html>"""
        try:
            send_email(email, f'[Action Required] Weekly Project Update — {week_str}', html)
            sent.append({'vertical': vertical, 'email': email})
            log.info(f'Reminder sent to {email} ({vertical})')
        except Exception as e:
            errors.append({'vertical': vertical, 'email': email, 'error': str(e)})
            log.warning(f'Failed to send reminder to {email}: {e}')

    return {'sent': len(sent), 'errors': len(errors), 'details': sent, 'errorDetails': errors}


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event('startup')
async def startup():
    init_db()
    log.info('─' * 50)
    log.info('SCAI Dashboard Server started')
    log.info(f'  Dashboard : {DASHBOARD}')
    log.info(f'  Database  : {DB_PATH}')
    log.info(f'  Uploads   : {UPLOAD_DIR}')
    log.info(f'  Auth      : {"ON (user=" + AUTH_USER + ")" if AUTH_ON else "OFF — set DASHBOARD_PWD to enable"}')
    log.info(f'  Port      : {PORT}')
    log.info('─' * 50)


# ── AI Endpoints ─────────────────────────────────────────────────────────────

@app.post('/api/ai/executive-summary', dependencies=[Depends(require_auth)])
async def ai_executive_summary():
    """Generate AI-powered executive briefing from latest weekly updates."""
    if not AI_ON:
        raise HTTPException(503, 'AI features disabled — set ANTHROPIC_API_KEY')

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Gather all projects with their latest updates
    with get_db() as conn:
        rows = conn.execute('SELECT id, data FROM projects').fetchall()

    if not rows:
        return JSONResponse({'summary': 'No project data available for analysis.'})

    projects_context = []
    for pid, data_json in rows:
        p = json.loads(data_json)
        updates = sorted(p.get('weeklyUpdates', []), key=lambda u: u.get('weekDate', ''), reverse=True)
        latest = updates[0] if updates else None
        prev = updates[1] if len(updates) > 1 else None
        ms_tracker = p.get('milestoneTracker', [])
        done_ms = sum(1 for m in ms_tracker if m.get('status') == 'Complete')
        total_ms = len(ms_tracker)
        projects_context.append({
            'name': p.get('name'),
            'vertical': p.get('vertical'),
            'owner': p.get('owner'),
            'startDate': p.get('startDate'),
            'targetEnd': p.get('targetEnd'),
            'milestoneProgress': f'{done_ms}/{total_ms}',
            'latestUpdate': latest,
            'previousUpdate': prev,
        })

    prompt = f"""You are an executive PMO analyst for New Murabba Development Company (NMDc), Smart City & AI department.

Analyse the following project portfolio data and produce a concise executive briefing.

PROJECTS:
{json.dumps(projects_context, indent=2)}

Write a briefing with these sections (use exactly these headings):
**Portfolio Pulse** — 2-3 sentence overall health summary. Mention total projects, how many on track vs at risk vs blocked vs complete.
**Key Wins This Week** — bullet list of 2-3 notable accomplishments across the portfolio.
**Watch List** — bullet list of 2-3 projects or risks that need leadership attention, with specific reasons.
**Recommended Actions** — 2-3 specific actionable recommendations for the SCAI Head.

Keep it under 250 words. Be specific — cite project names, numbers, and dates. Write in professional executive tone. Do not use markdown headers (no #), just bold the section titles with **."""

    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=600,
            messages=[{'role': 'user', 'content': prompt}]
        )
        summary = response.content[0].text
        return JSONResponse({'summary': summary})
    except Exception as e:
        log.error(f'AI summary failed: {e}')
        raise HTTPException(500, f'AI generation failed: {str(e)}')


@app.post('/api/ai/risk-prediction', dependencies=[Depends(require_auth)])
async def ai_risk_prediction():
    """AI-powered risk prediction for each project based on update patterns."""
    if not AI_ON:
        raise HTTPException(503, 'AI features disabled — set ANTHROPIC_API_KEY')

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    with get_db() as conn:
        rows = conn.execute('SELECT id, data FROM projects').fetchall()

    if not rows:
        return JSONResponse({'predictions': []})

    projects_context = []
    for pid, data_json in rows:
        p = json.loads(data_json)
        updates = sorted(p.get('weeklyUpdates', []), key=lambda u: u.get('weekDate', ''), reverse=True)
        ms_tracker = p.get('milestoneTracker', [])
        milestones = p.get('milestones', [])

        # Compute progress velocity
        progress_values = []
        for u in updates[:3]:
            progress_values.append(u.get('progress', 0))

        # Check overdue milestones
        today = datetime.now().strftime('%Y-%m-%d')
        overdue = []
        for ms in milestones:
            tracker = next((t for t in ms_tracker if t.get('milestoneId') == ms.get('id')), None)
            if tracker and tracker.get('status') != 'Complete' and ms.get('targetDate', '9999') < today:
                overdue.append(ms.get('name'))

        blocker_streak = 0
        for u in updates:
            if u.get('hasBlocker'):
                blocker_streak += 1
            else:
                break

        projects_context.append({
            'id': pid,
            'name': p.get('name'),
            'vertical': p.get('vertical'),
            'owner': p.get('owner'),
            'targetEnd': p.get('targetEnd'),
            'recentUpdates': updates[:3],
            'overdueMilestones': overdue,
            'blockerStreak': blocker_streak,
            'milestoneDone': sum(1 for m in ms_tracker if m.get('status') == 'Complete'),
            'milestoneTotal': len(ms_tracker),
        })

    prompt = f"""You are a PMO risk analyst for New Murabba Development Company. Analyse each project and predict risk levels.

PROJECTS:
{json.dumps(projects_context, indent=2)}

For each project, return a JSON array of objects with:
- "id": project ID
- "risk": "high", "medium", or "low"
- "score": 0-100 (100 = highest risk)
- "signal": one-line reason (max 15 words)
- "trend": "rising", "stable", or "falling" (is risk increasing or decreasing?)

Consider: blocker streaks (consecutive weeks with blockers), overdue milestones, slowing progress velocity, vague or repetitive update language, approaching deadlines with low completion.
Projects that are already Complete should be "low" risk with score 0-5.

Return ONLY the JSON array, no explanation or markdown."""

    try:
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=800,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = response.content[0].text.strip()
        # Parse JSON from response (handle markdown code blocks if present)
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        predictions = json.loads(text)
        return JSONResponse({'predictions': predictions})
    except json.JSONDecodeError:
        log.error(f'AI risk prediction returned non-JSON: {text[:200]}')
        return JSONResponse({'predictions': [], 'error': 'AI returned invalid format'})
    except Exception as e:
        log.error(f'AI risk prediction failed: {e}')
        raise HTTPException(500, f'AI generation failed: {str(e)}')


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import uvicorn
    uvicorn.run('server:app', host='0.0.0.0', port=PORT, reload=False, log_level='info')
