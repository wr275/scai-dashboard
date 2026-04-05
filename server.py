"""
SCAI Executive Dashboard — Backend Server
==========================================
Local:  python server.py            → http://localhost:8000
Cloud:  set env vars, platform auto-starts via Procfile

Environment variables (all optional — sensible defaults apply):
  PORT          Port to listen on (cloud platforms set this automatically)
  DATA_DIR      Where to store DB + uploads (default: ~/SCAI_Dashboard_Data)
  DASHBOARD_PWD Password to protect the dashboard (default: no password)
  DASHBOARD_USR Username for basic auth (default: scai)

Requires: pip install fastapi uvicorn openpyxl python-multipart
"""

import os
import json
import sqlite3
import shutil
import logging
import secrets
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

# Auth (optional — only active when DASHBOARD_PWD is set)
AUTH_USER = os.environ.get('DASHBOARD_USR', 'scai')
AUTH_PWD  = os.environ.get('DASHBOARD_PWD', '')   # empty = no auth
AUTH_ON   = bool(AUTH_PWD)

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


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Dependency: enforce basic auth when DASHBOARD_PWD is set."""
    if not AUTH_ON:
        return  # auth disabled
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Basic realm="SCAI Dashboard"'},
        )
    ok_user = secrets.compare_digest(credentials.username.encode(), AUTH_USER.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), AUTH_PWD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Incorrect username or password',
            headers={'WWW-Authenticate': 'Basic realm="SCAI Dashboard"'},
        )


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


@app.delete('/api/projects/{pid}', dependencies=[Depends(require_auth)])
async def api_delete_project(pid: str):
    pid = pid.upper()
    with get_conn() as conn:
        result = conn.execute('DELETE FROM projects WHERE id = ?', (pid,))
        if result.rowcount == 0:
            raise HTTPException(404, f'Project {pid} not found')
    log.info(f'Deleted project {pid}')
    return {'deleted': pid}


@app.post('/api/upload', dependencies=[Depends(require_auth)])
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


@app.get('/api/upload-log', dependencies=[Depends(require_auth)])
async def api_upload_log(limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT * FROM upload_log ORDER BY log_id DESC LIMIT ?', (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/status')   # no auth — used as health check by cloud platforms
async def api_status():
    projects = get_all_projects()
    return {
        'status':     'ok',
        'projects':   len(projects),
        'authEnabled': AUTH_ON,
        'serverTime': datetime.utcnow().isoformat(),
    }


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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import uvicorn
    uvicorn.run('server:app', host='0.0.0.0', port=PORT, reload=False, log_level='info')
