"""Shared persistence layer for the job matcher and email sorter.

The existing ``jobs_history`` table remains the scrape/search index.  This module
adds a canonical application record, an append-only event ledger, review queues,
approval-based follow-ups, and generated artifacts.  Both services point at the
same SQLite file so status changes no longer depend on fuzzy Notion matching.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone


DEFAULT_DB_PATH = os.environ.get("JOB_PIPELINE_DB", "/job-data/processed_jobs.db")
APPLICATION_STATUSES = {
    "discovered", "shortlisted", "applied", "mid_convo", "interviewing",
    "offer", "rejected", "withdrawn", "closed",
}
FOLLOWUP_STATES = {"due", "draft_ready", "approved", "sent", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str | None) -> str:
    value = (value or "").lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    ignored = {"inc", "llc", "ltd", "corp", "corporation", "co", "company"}
    return " ".join(word for word in value.split() if word not in ignored).strip()


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def application_id(job_url: str = "", candidate: str = "", fallback: str = "") -> str:
    identity = f"{job_url.strip()}|{candidate.strip().lower()}" if job_url else fallback
    return stable_hash(identity or utc_now())[:24]


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS applications (
            application_id TEXT PRIMARY KEY,
            job_url TEXT,
            job_url_hash TEXT,
            ats_requisition_id TEXT,
            candidate TEXT NOT NULL DEFAULT 'candidate_a',
            notion_page_id TEXT,
            email_thread_id TEXT,
            email_message_id TEXT,
            title TEXT,
            company TEXT,
            company_normalized TEXT,
            source TEXT,
            location TEXT,
            match_score INTEGER,
            status TEXT NOT NULL DEFAULT 'discovered',
            applied_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_app_job_candidate
            ON applications(job_url_hash, candidate) WHERE job_url_hash IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_app_requisition ON applications(ats_requisition_id);
        CREATE INDEX IF NOT EXISTS idx_app_thread ON applications(email_thread_id);
        CREATE INDEX IF NOT EXISTS idx_app_company ON applications(company_normalized);
        CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);

        CREATE TABLE IF NOT EXISTS application_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id TEXT,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            source TEXT NOT NULL,
            status_from TEXT,
            status_to TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            requires_approval INTEGER NOT NULL DEFAULT 0,
            approved_at TEXT,
            unique_key TEXT UNIQUE,
            FOREIGN KEY(application_id) REFERENCES applications(application_id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_time ON application_events(event_at);
        CREATE INDEX IF NOT EXISTS idx_events_app ON application_events(application_id);

        CREATE TABLE IF NOT EXISTS email_messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT,
            application_id TEXT,
            subject TEXT,
            sender TEXT,
            snippet TEXT,
            classification TEXT,
            confidence REAL,
            classification_reason TEXT,
            review_status TEXT NOT NULL DEFAULT 'none',
            processed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(application_id) REFERENCES applications(application_id)
        );

        CREATE TABLE IF NOT EXISTS classification_reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            proposed_category TEXT,
            confidence REAL NOT NULL DEFAULT 0,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_category TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY(message_id) REFERENCES email_messages(message_id)
        );

        CREATE TABLE IF NOT EXISTS followups (
            followup_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id TEXT NOT NULL,
            due_at TEXT NOT NULL,
            draft_text TEXT,
            state TEXT NOT NULL DEFAULT 'due',
            drafted_at TEXT,
            approved_at TEXT,
            sent_at TEXT,
            recipient TEXT,
            subject TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(application_id, due_at),
            FOREIGN KEY(application_id) REFERENCES applications(application_id)
        );
        CREATE INDEX IF NOT EXISTS idx_followup_state_due ON followups(state, due_at);

        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            content_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'draft',
            model TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            FOREIGN KEY(application_id) REFERENCES applications(application_id)
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_app_type
            ON artifacts(application_id, artifact_type, created_at);

        CREATE TABLE IF NOT EXISTS accomplishments (
            accomplishment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate TEXT NOT NULL,
            source_resume TEXT,
            original_text TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(candidate, original_text)
        );

        CREATE TABLE IF NOT EXISTS weekly_reports (
            week_start TEXT PRIMARY KEY,
            week_end TEXT NOT NULL,
            report_json TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in conn.execute('PRAGMA table_info(applications)')}
    for name, definition in [('applied_match_score', 'INTEGER'), ('score_snapshot_origin', 'TEXT')]:
        if name not in columns:
            conn.execute(f'ALTER TABLE applications ADD COLUMN {name} {definition}')
    conn.commit()
    conn.close()


def row_dict(row):
    return dict(row) if row else None


def ensure_application(
    *, db_path: str = DEFAULT_DB_PATH, job_url: str = "", candidate: str = "candidate_a",
    title: str = "", company: str = "", source: str = "", location: str = "",
    match_score=None, notion_page_id: str = "", ats_requisition_id: str = "",
    email_thread_id: str = "", email_message_id: str = "", status: str = "discovered",
    applied_at: str | None = None, historical_import: bool = False,
):
    init_schema(db_path)
    candidate = (candidate or "candidate_a").strip().lower()
    job_hash = stable_hash(job_url.strip()) if job_url else None
    fallback = email_message_id or email_thread_id or ats_requisition_id or f"{company}|{title}|{candidate}"
    app_id = application_id(job_url, candidate, fallback)
    now = utc_now()
    if status not in APPLICATION_STATUSES:
        status = "discovered"
    conn = connect(db_path)
    conn.execute('BEGIN IMMEDIATE')
    prior = conn.execute('SELECT applied_at FROM applications WHERE application_id=?', (app_id,)).fetchone()
    conn.execute(
        """INSERT INTO applications
           (application_id, job_url, job_url_hash, ats_requisition_id, candidate,
            notion_page_id, email_thread_id, email_message_id, title, company,
            company_normalized, source, location, match_score, status, applied_at,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(application_id) DO UPDATE SET
             job_url=COALESCE(NULLIF(excluded.job_url,''), applications.job_url),
             job_url_hash=COALESCE(excluded.job_url_hash, applications.job_url_hash),
             ats_requisition_id=COALESCE(NULLIF(excluded.ats_requisition_id,''), applications.ats_requisition_id),
             notion_page_id=COALESCE(NULLIF(excluded.notion_page_id,''), applications.notion_page_id),
             email_thread_id=COALESCE(NULLIF(excluded.email_thread_id,''), applications.email_thread_id),
             email_message_id=COALESCE(NULLIF(excluded.email_message_id,''), applications.email_message_id),
             title=COALESCE(NULLIF(excluded.title,''), applications.title),
             company=COALESCE(NULLIF(excluded.company,''), applications.company),
             company_normalized=COALESCE(NULLIF(excluded.company_normalized,''), applications.company_normalized),
             source=COALESCE(NULLIF(excluded.source,''), applications.source),
             location=COALESCE(NULLIF(excluded.location,''), applications.location),
             match_score=COALESCE(excluded.match_score, applications.match_score),
             applied_at=COALESCE(excluded.applied_at, applications.applied_at),
             updated_at=excluded.updated_at""",
        (app_id, job_url, job_hash, ats_requisition_id, candidate, notion_page_id,
         email_thread_id, email_message_id, title, company, normalize_text(company),
         source, location, match_score, status, applied_at, now, now),
    )
    if not historical_import and (status == 'applied' or applied_at) and (not prior or not prior['applied_at']):
        # Preserve the first submission score; later re-scoring cannot rewrite history.
        conn.execute("UPDATE applications SET applied_match_score=?,score_snapshot_origin='submission' WHERE application_id=? AND score_snapshot_origin IS NULL",
                     (match_score, app_id))
    conn.commit()
    row = conn.execute("SELECT * FROM applications WHERE application_id=?", (app_id,)).fetchone()
    conn.close()
    return row_dict(row)


def update_application_status(application_id_value: str, status: str, *, db_path=DEFAULT_DB_PATH,
                              source="system", metadata=None, unique_key=None):
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"Invalid application status: {status}")
    conn = connect(db_path)
    conn.execute('BEGIN IMMEDIATE')
    row = conn.execute("SELECT status,applied_at FROM applications WHERE application_id=?", (application_id_value,)).fetchone()
    if not row:
        conn.close()
        raise ValueError("Application not found")
    previous = row["status"]
    now = utc_now()
    applied_at = now if status == "applied" else None
    conn.execute(
        "UPDATE applications SET status=?, applied_at=COALESCE(applied_at, ?), updated_at=? WHERE application_id=?",
        (status, applied_at, now, application_id_value),
    )
    if status == 'applied' and not row['applied_at']:
        conn.execute("UPDATE applications SET applied_match_score=match_score,score_snapshot_origin='submission' WHERE application_id=?", (application_id_value,))
    _record_event_conn(conn, application_id_value, "status_changed", source, previous, status,
                       metadata or {}, False, None, unique_key)
    conn.commit()
    conn.close()


def link_application(application_id_value: str, *, db_path=DEFAULT_DB_PATH,
                     notion_page_id="", ats_requisition_id="", email_thread_id="",
                     email_message_id="", title="", company=""):
    """Attach identifiers discovered later without changing application identity."""
    conn = connect(db_path)
    now = utc_now()
    conn.execute(
        """UPDATE applications SET
           notion_page_id=COALESCE(NULLIF(?,''),notion_page_id),
           ats_requisition_id=COALESCE(NULLIF(?,''),ats_requisition_id),
           email_thread_id=COALESCE(NULLIF(?,''),email_thread_id),
           email_message_id=COALESCE(NULLIF(?,''),email_message_id),
           title=COALESCE(NULLIF(?,''),title),
           company=COALESCE(NULLIF(?,''),company),
           company_normalized=CASE WHEN ?!='' THEN ? ELSE company_normalized END,
           updated_at=? WHERE application_id=?""",
        (notion_page_id, ats_requisition_id, email_thread_id, email_message_id,
         title, company, company, normalize_text(company), now, application_id_value),
    )
    conn.commit()
    conn.close()


def _record_event_conn(conn, app_id, event_type, source, status_from, status_to,
                       metadata, requires_approval, approved_at, unique_key):
    conn.execute(
        """INSERT OR IGNORE INTO application_events
           (application_id,event_type,event_at,source,status_from,status_to,metadata_json,
            requires_approval,approved_at,unique_key) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (app_id, event_type, utc_now(), source, status_from, status_to,
         json.dumps(metadata or {}, ensure_ascii=False), int(bool(requires_approval)),
         approved_at, unique_key),
    )


def record_event(application_id_value, event_type, *, db_path=DEFAULT_DB_PATH, source="system",
                 status_from=None, status_to=None, metadata=None, requires_approval=False,
                 approved_at=None, unique_key=None):
    conn = connect(db_path)
    _record_event_conn(conn, application_id_value, event_type, source, status_from, status_to,
                       metadata, requires_approval, approved_at, unique_key)
    conn.commit()
    conn.close()


def resolve_application(*, db_path=DEFAULT_DB_PATH, job_url="", job_url_hash="",
                        ats_requisition_id="", email_thread_id="", email_message_id="",
                        notion_page_id="", company="", title="", candidate=""):
    init_schema(db_path)
    conn = connect(db_path)
    checks = [
        ("email_message_id", email_message_id), ("email_thread_id", email_thread_id),
        ("ats_requisition_id", ats_requisition_id),
        ("notion_page_id", notion_page_id),
        ("job_url_hash", job_url_hash or (stable_hash(job_url) if job_url else "")),
    ]
    for column, value in checks:
        if value:
            params = [value]
            sql = f"SELECT * FROM applications WHERE {column}=?"
            if candidate:
                sql += " AND candidate=?"
                params.append(candidate.lower())
            sql += " ORDER BY updated_at DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
            if row:
                conn.close()
                return row_dict(row)

    norm_company = normalize_text(company)
    if norm_company:
        rows = conn.execute(
            """SELECT * FROM applications WHERE company_normalized=?
               AND status NOT IN ('rejected','withdrawn','closed')
               ORDER BY updated_at DESC LIMIT 10""", (norm_company,),
        ).fetchall()
        norm_title = normalize_text(title)
        if len(rows) == 1:
            conn.close()
            return row_dict(rows[0])
        if norm_title:
            for row in rows:
                saved = normalize_text(row["title"])
                if saved and (saved in norm_title or norm_title in saved):
                    conn.close()
                    return row_dict(row)
    conn.close()
    return None


def upsert_email_message(*, message_id, thread_id="", subject="", sender="", snippet="",
                         classification="", confidence=0.0, reason="", application_id_value=None,
                         review_status="none", processed_at=None, db_path=DEFAULT_DB_PATH):
    init_schema(db_path)
    now = utc_now()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO email_messages
           (message_id,thread_id,application_id,subject,sender,snippet,classification,
            confidence,classification_reason,review_status,processed_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(message_id) DO UPDATE SET
             thread_id=COALESCE(NULLIF(excluded.thread_id,''),email_messages.thread_id),
             application_id=COALESCE(excluded.application_id,email_messages.application_id),
             classification=COALESCE(NULLIF(excluded.classification,''),email_messages.classification),
             confidence=excluded.confidence, classification_reason=excluded.classification_reason,
             review_status=excluded.review_status,
             processed_at=COALESCE(excluded.processed_at,email_messages.processed_at),
             updated_at=excluded.updated_at""",
        (message_id, thread_id, application_id_value, subject, sender, snippet[:1000],
         classification, float(confidence or 0), reason, review_status, processed_at, now, now),
    )
    conn.commit()
    conn.close()


def queue_classification_review(*, message_id, proposed_category, confidence, reason,
                                db_path=DEFAULT_DB_PATH):
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO classification_reviews
           (message_id,proposed_category,confidence,reason,status,created_at)
           VALUES (?,?,?,?, 'pending', ?)
           ON CONFLICT(message_id) DO UPDATE SET
             proposed_category=excluded.proposed_category, confidence=excluded.confidence,
             reason=excluded.reason
           WHERE classification_reviews.status='pending'""",
        (message_id, proposed_category, float(confidence or 0), reason, utc_now()),
    )
    conn.execute("UPDATE email_messages SET review_status='pending',updated_at=? WHERE message_id=?",
                 (utc_now(), message_id))
    conn.commit()
    conn.close()


def list_classification_reviews(*, db_path=DEFAULT_DB_PATH, status="pending", limit=100):
    conn = connect(db_path)
    rows = conn.execute(
        """SELECT r.*,m.subject,m.sender,m.snippet,m.thread_id
           FROM classification_reviews r JOIN email_messages m USING(message_id)
           WHERE r.status=? ORDER BY r.created_at ASC LIMIT ?""", (status, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def decide_classification(review_id: int, category: str, *, db_path=DEFAULT_DB_PATH):
    allowed = {"Applied", "Rejected", "Interviewing", "Mid-Convo", "None"}
    if category not in allowed:
        raise ValueError("Invalid classification category")
    conn = connect(db_path)
    row = conn.execute("SELECT message_id FROM classification_reviews WHERE review_id=?", (review_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError("Review not found")
    now = utc_now()
    conn.execute(
        "UPDATE classification_reviews SET status='approved',reviewed_category=?,reviewed_at=? WHERE review_id=?",
        (category, now, review_id),
    )
    conn.execute(
        "UPDATE email_messages SET classification=?,confidence=1.0,review_status='approved',updated_at=? WHERE message_id=?",
        (category, now, row["message_id"]),
    )
    conn.commit()
    conn.close()


def classification_decision(message_id: str, *, db_path=DEFAULT_DB_PATH):
    conn = connect(db_path)
    row = conn.execute(
        "SELECT classification,confidence,classification_reason,review_status,processed_at FROM email_messages WHERE message_id=?",
        (message_id,),
    ).fetchone()
    conn.close()
    return row_dict(row)


def schedule_followup(application_id_value: str, due_at: str, *, db_path=DEFAULT_DB_PATH):
    now = utc_now()
    conn = connect(db_path)
    conn.execute(
        """INSERT OR IGNORE INTO followups
           (application_id,due_at,state,created_at,updated_at) VALUES (?,?,'due',?,?)""",
        (application_id_value, due_at, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM followups WHERE application_id=? AND due_at=?", (application_id_value, due_at),
    ).fetchone()
    conn.close()
    return row_dict(row)


def update_followup(followup_id: int, state: str, *, db_path=DEFAULT_DB_PATH,
                    draft_text=None, recipient=None, subject=None):
    if state not in FOLLOWUP_STATES:
        raise ValueError("Invalid follow-up state")
    allowed = {
        "due": {"draft_ready", "cancelled"},
        "draft_ready": {"approved", "cancelled"},
        "approved": {"sent", "cancelled"},
        "sent": set(), "cancelled": set(),
    }
    conn = connect(db_path)
    current = conn.execute("SELECT * FROM followups WHERE followup_id=?", (followup_id,)).fetchone()
    if not current:
        conn.close()
        raise ValueError("Follow-up not found")
    if state != current["state"] and state not in allowed.get(current["state"], set()):
        conn.close()
        raise ValueError(f"Cannot move follow-up from {current['state']} to {state}")
    now = utc_now()
    drafted_at = now if state == "draft_ready" else current["drafted_at"]
    approved_at = now if state == "approved" else current["approved_at"]
    sent_at = now if state == "sent" else current["sent_at"]
    conn.execute(
        """UPDATE followups SET state=?,draft_text=COALESCE(?,draft_text),
           recipient=COALESCE(?,recipient),subject=COALESCE(?,subject),drafted_at=?,
           approved_at=?,sent_at=?,updated_at=? WHERE followup_id=?""",
        (state, draft_text, recipient, subject, drafted_at, approved_at, sent_at, now, followup_id),
    )
    _record_event_conn(conn, current["application_id"], f"followup_{state}", "dashboard",
                       None, None, {"followup_id": followup_id}, state in {"draft_ready", "approved"},
                       approved_at, f"followup:{followup_id}:{state}")
    conn.commit()
    row = conn.execute("SELECT * FROM followups WHERE followup_id=?", (followup_id,)).fetchone()
    conn.close()
    return row_dict(row)


def list_followups(*, db_path=DEFAULT_DB_PATH, state=None, limit=100):
    conn = connect(db_path)
    where, params = "", []
    if state:
        states = state if isinstance(state, (list, tuple)) else [state]
        where = f"WHERE f.state IN ({','.join('?' for _ in states)})"
        params.extend(states)
    rows = conn.execute(
        f"""SELECT f.*,a.title,a.company,a.candidate,a.status AS application_status
            FROM followups f JOIN applications a USING(application_id)
            {where} ORDER BY f.due_at ASC LIMIT ?""", params + [limit],
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def next_application_action(application, followup=None, interview_artifact=None, *, now=None):
    """Describe the next human step without changing application state."""
    now = now or datetime.now(timezone.utc)
    if followup:
        state = followup["state"]
        due = _parse_datetime(followup.get("due_at"))
        if state == "approved":
            return {"label": "Send approved follow-up", "detail": "Send it yourself, then confirm sent.",
                    "target": "followup", "followup_id": followup["followup_id"], "priority": 0}
        if state == "draft_ready":
            return {"label": "Review follow-up draft", "detail": "Approve only after checking the message.",
                    "target": "followup", "followup_id": followup["followup_id"], "priority": 0}
        if state == "due" and (due is None or due <= now):
            return {"label": "Draft overdue follow-up", "detail": "A follow-up is due and needs a draft.",
                    "target": "followup", "followup_id": followup["followup_id"], "priority": 0}

    status = application["status"]
    if status == "interviewing":
        artifact_state = (interview_artifact or {}).get("state")
        label = ("Add role details for interview prep" if not artifact_state and
                 (not application.get("title") or not application.get("company")) else
                 "Review interview prep draft" if artifact_state == "draft" else
                 "Use interview prep" if artifact_state == "approved" else "Prepare interview")
        return {"label": label, "detail": "Confirm the role and company; paste the posting for role-specific questions.",
                "target": "interview", "priority": 1}
    if status == "offer":
        return {"label": "Review offer details", "detail": "Check the terms and your response deadline.",
                "target": None, "priority": 1}
    if status == "mid_convo":
        return {"label": "Review conversation", "detail": "Check whether a reply is needed.",
                "target": None, "priority": 3}
    if status == "shortlisted":
        return {"label": "Apply on employer site", "detail": "Mark applied only after submitting.",
                "target": "job" if application.get("job_url") else None, "priority": 2}
    if followup and followup["state"] == "due":
        return {"label": "Follow-up scheduled", "detail": "Due on the date shown below.",
                "target": "followup", "followup_id": followup["followup_id"],
                "due_at": followup["due_at"], "priority": 3}
    if status == "applied":
        return {"label": "Watch for response", "detail": "No follow-up is currently scheduled.",
                "target": None, "priority": 4}
    return {"label": "Review application", "detail": "Check the status and decide the next step.",
            "target": "job" if application.get("job_url") else None, "priority": 5}


def list_applications_with_next_actions(*, db_path=DEFAULT_DB_PATH, statuses=None, limit=500, now=None):
    """Load applications and action context in three bounded queries."""
    conn = connect(db_path)
    statuses = list(statuses or [])
    where = f"WHERE status IN ({','.join('?' for _ in statuses)})" if statuses else ""
    rows = conn.execute(
        f"SELECT * FROM applications {where} ORDER BY updated_at DESC LIMIT ?",
        statuses + [limit],
    ).fetchall()
    applications = [dict(row) for row in rows]
    if not applications:
        conn.close()
        return []
    ids = [item["application_id"] for item in applications]
    slots = ",".join("?" for _ in ids)
    pending = conn.execute(
        f"SELECT application_id,followup_id,due_at,state FROM followups "
        f"WHERE application_id IN ({slots}) AND state IN ('due','draft_ready','approved')",
        ids,
    ).fetchall()
    artifacts = conn.execute(
        f"SELECT application_id,state FROM artifacts WHERE application_id IN ({slots}) "
        "AND artifact_type='interview_prep' ORDER BY created_at DESC,artifact_id DESC",
        ids,
    ).fetchall()
    conn.close()
    now = now or datetime.now(timezone.utc)
    followups = {}
    for row in pending:
        item = dict(row)
        current = followups.get(item["application_id"])
        def rank(value):
            due = _parse_datetime(value["due_at"])
            state_rank = {"approved": 0, "draft_ready": 1}.get(value["state"],
                2 if due is None or due <= now else 3)
            return (state_rank, value["due_at"])
        if current is None or rank(item) < rank(current):
            followups[item["application_id"]] = item
    latest_artifacts = {}
    for row in artifacts:
        latest_artifacts.setdefault(row["application_id"], dict(row))
    for item in applications:
        item["next_action"] = next_application_action(
            item, followups.get(item["application_id"]),
            latest_artifacts.get(item["application_id"]), now=now,
        )
    applications.sort(key=lambda item: item["next_action"]["priority"])
    return applications


def save_artifact(application_id_value, artifact_type, content, *, db_path=DEFAULT_DB_PATH,
                  model=None, state="draft"):
    conn = connect(db_path)
    now = utc_now()
    cur = conn.execute(
        """INSERT INTO artifacts(application_id,artifact_type,content_json,state,model,created_at)
           VALUES (?,?,?,?,?,?)""",
        (application_id_value, artifact_type, json.dumps(content, ensure_ascii=False), state, model, now),
    )
    artifact_id = cur.lastrowid
    _record_event_conn(conn, application_id_value, f"{artifact_type}_generated", "dashboard",
                       None, None, {"artifact_id": artifact_id}, True, None,
                       f"artifact:{artifact_id}:generated")
    conn.commit()
    conn.close()
    return artifact_id


def list_artifacts(application_id_value, *, db_path=DEFAULT_DB_PATH):
    conn = connect(db_path)
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE application_id=? ORDER BY created_at DESC", (application_id_value,),
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["content"] = json.loads(item.pop("content_json"))
        item["content"].pop('pdf_base64', None)
        result.append(item)
    return result


def approve_artifact(artifact_id: int, *, db_path=DEFAULT_DB_PATH):
    conn = connect(db_path)
    row = conn.execute("SELECT application_id FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError("Artifact not found")
    now = utc_now()
    conn.execute("UPDATE artifacts SET state='approved',approved_at=? WHERE artifact_id=?", (now, artifact_id))
    _record_event_conn(conn, row["application_id"], "artifact_approved", "dashboard", None, None,
                       {"artifact_id": artifact_id}, True, now, f"artifact:{artifact_id}:approved")
    conn.commit()
    conn.close()


def add_accomplishments(candidate, items, *, source_resume="", db_path=DEFAULT_DB_PATH):
    conn = connect(db_path)
    now = utc_now()
    for item in items:
        text = (item.get("text") if isinstance(item, dict) else str(item)).strip()
        tags = item.get("tags", "") if isinstance(item, dict) else ""
        if text:
            conn.execute(
                """INSERT OR IGNORE INTO accomplishments
                   (candidate,source_resume,original_text,tags,created_at) VALUES (?,?,?,?,?)""",
                (candidate.lower(), source_resume, text, tags, now),
            )
    conn.commit()
    conn.close()


def clear_accomplishments(candidate, *, db_path=DEFAULT_DB_PATH):
    conn = connect(db_path)
    conn.execute("DELETE FROM accomplishments WHERE candidate=?", (candidate.lower(),))
    conn.commit()
    conn.close()


def list_accomplishments(candidate, *, db_path=DEFAULT_DB_PATH, limit=100):
    conn = connect(db_path)
    rows = conn.execute(
        "SELECT * FROM accomplishments WHERE candidate=? AND active=1 ORDER BY accomplishment_id LIMIT ?",
        (candidate.lower(), limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def migrate_jobs_history(db_path=DEFAULT_DB_PATH):
    """Idempotently backfill canonical applications from the existing job index."""
    init_schema(db_path)
    conn = connect(db_path)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs_history'"
    ).fetchone()
    if not exists:
        conn.close()
        return 0
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs_history)")}
    wanted = [name for name in (
        "job_url", "title", "company", "source", "location", "candidate", "match_score",
        "applied", "notion_page_id", "notion_status", "processed_at",
    ) if name in columns]
    rows = conn.execute(
        f"""SELECT {','.join('j.' + name for name in wanted)} FROM jobs_history j
            WHERE NOT EXISTS (
              SELECT 1 FROM applications a
              WHERE a.job_url=j.job_url
                AND a.candidate=LOWER(COALESCE(NULLIF(j.candidate,''),'candidate_a'))
            )"""
    ).fetchall()
    conn.close()
    count = 0
    for row in rows:
        data = dict(row)
        status = "applied" if data.get("applied") else "discovered"
        notion_status = (data.get("notion_status") or "").lower()
        status_map = {"shortlisted": "shortlisted", "rejected": "rejected", "interview": "interviewing",
                      "interviewing": "interviewing", "mid-convo": "mid_convo", "offer": "offer"}
        status = status_map.get(notion_status, status)
        ensure_application(
            db_path=db_path, historical_import=True, job_url=data.get("job_url") or "", candidate=data.get("candidate") or "candidate_a",
            title=data.get("title") or "", company=data.get("company") or "",
            source=data.get("source") or "", location=data.get("location") or "",
            match_score=data.get("match_score"), notion_page_id=data.get("notion_page_id") or "",
            status=status, applied_at=data.get("processed_at") if status == "applied" else None,
        )
        count += 1
    return count


def import_processed_email_history(jsonl_path, *, db_path=DEFAULT_DB_PATH):
    """Seed historical outcome events from the legacy JSONL audit, idempotently."""
    if not os.path.exists(jsonl_path):
        return 0
    valid = {"Applied": "applied", "Rejected": "rejected",
             "Interviewing": "interviewing", "Mid-Convo": "mid_convo"}
    imported = 0
    with open(jsonl_path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            category = item.get("category")
            if category not in valid:
                continue
            message_id = item.get("message_id") or f"legacy-{stable_hash(str(item.get('uid')) + str(item.get('subject')) + str(item.get('date')))[:24]}"
            event_at = item.get("processed_at") or item.get("date") or utc_now()
            notion_result = item.get("notion_result") or ""
            title_match = re.search(r'["“](.+?)["”]', notion_result)
            title = title_match.group(1) if title_match else item.get("subject", "")
            company = ""
            if " - " in title:
                title, company = title.rsplit(" - ", 1)
            app = resolve_application(db_path=db_path, email_message_id=message_id,
                                      company=company, title=title, candidate="candidate_a")
            if not app:
                app = ensure_application(
                    db_path=db_path, historical_import=True, candidate="candidate_a", title=title, company=company,
                    email_message_id=message_id, status=valid[category],
                    applied_at=item.get("date") if category == "Applied" else None,
                )
            conn = connect(db_path)
            cursor = conn.execute(
                """INSERT OR IGNORE INTO application_events
                   (application_id,event_type,event_at,source,metadata_json,unique_key)
                   VALUES (?,?,?,?,?,?)""",
                (app["application_id"], f"email_{category.lower().replace('-', '_')}",
                 event_at, "legacy-email-audit",
                 json.dumps({"message_id": message_id, "subject": item.get("subject", "")}),
                 f"legacy-email:{line_number}:{message_id}:{category}"),
            )
            imported += cursor.rowcount
            conn.commit()
            conn.close()
    return imported


def build_weekly_report(*, db_path=DEFAULT_DB_PATH, week_start=None):
    init_schema(db_path)
    today = datetime.now(timezone.utc).date()
    start = week_start or (today - timedelta(days=today.weekday()))
    if isinstance(start, str):
        start = datetime.fromisoformat(start).date()
    end = start + timedelta(days=7)
    start_iso, end_iso = start.isoformat(), end.isoformat()
    conn = connect(db_path)

    event_counts = {
        row["event_type"]: row["count"] for row in conn.execute(
            """SELECT event_type,COUNT(*) count FROM application_events
               WHERE event_at>=? AND event_at<? GROUP BY event_type""", (start_iso, end_iso)
        ).fetchall()
    }
    statuses = {
        row["status"]: row["count"] for row in conn.execute(
            "SELECT status,COUNT(*) count FROM applications GROUP BY status"
        ).fetchall()
    }
    overdue = conn.execute(
        "SELECT COUNT(*) FROM followups WHERE state IN ('due','draft_ready') AND due_at<?", (utc_now(),)
    ).fetchone()[0]
    pending_reviews = conn.execute(
        "SELECT COUNT(*) FROM classification_reviews WHERE status='pending'"
    ).fetchone()[0]
    applications = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE applied_at>=? AND applied_at<?", (start_iso, end_iso)
    ).fetchone()[0]
    # Weekly activity counts distinct applications, not repeated email/status events.
    def activity(status):
        return conn.execute("""SELECT COUNT(DISTINCT application_id) FROM application_events
            WHERE event_at>=? AND event_at<? AND (status_to=? OR event_type IN (?,?))""",
            (start_iso, end_iso, status, status, 'email_' + status)).fetchone()[0]
    interviews, rejections = activity('interviewing'), activity('rejected')
    cohort_rows = conn.execute("""SELECT a.*,
        EXISTS(SELECT 1 FROM application_events e WHERE e.application_id=a.application_id AND datetime(e.event_at)>=datetime(a.applied_at)
            AND (e.status_to='interviewing' OR e.event_type IN ('interviewing','email_interviewing'))) AS interviewed,
        EXISTS(SELECT 1 FROM application_events e WHERE e.application_id=a.application_id AND datetime(e.event_at)>=datetime(a.applied_at)
            AND (e.status_to IN ('interviewing','mid_convo','offer','rejected')
                OR e.event_type IN ('email_interviewing','email_mid_convo','email_rejected','interviewing','rejected'))) AS responded
        FROM applications a WHERE a.applied_at IS NOT NULL""").fetchall()
    def ever_interviewed(row):
        return bool(row['interviewed'] or row['status'] in ('interviewing', 'offer'))
    cohort = [r for r in cohort_rows if start_iso <= r['applied_at'] < end_iso]
    sources, bands, calibration = {}, {}, {}
    for row in cohort_rows:
        score = row['applied_match_score']
        band = 'unscored / historical unknown' if score is None else '80+' if score >= 80 else '60-79' if score >= 60 else '<60'
        for key, group, field in [(row['source'] or 'Unknown', sources, 'source'), (band, bands, 'band')]:
            item = group.setdefault(key, {field: key, 'total': 0, 'applied': 0, 'interviewing': 0})
            item['total'] += 1
            item['applied'] += 1
            item['interviewing'] += int(ever_interviewed(row))
        title = row['title'] or ''
        role_track = ('GRC' if re.search(r'grc|risk|compliance|audit|governance|assurance|privacy', title, re.I)
                      else 'Support / infrastructure' if re.search(r'support|help.?desk|network|noc|system|cloud', title, re.I)
                      else 'SOC / security')
        key = (row['candidate'], role_track, row['source'] or 'Unknown', band)
        item = calibration.setdefault(key, {'candidate': key[0], 'track': key[1], 'source': key[2], 'band': band,
                                           'resolved': 0, 'pending': 0, 'excluded': 0, 'interviews': 0, 'offers': 0})
        if row['status'] in ('offer', 'rejected'):
            item['resolved'] += 1
            item['interviews'] += int(ever_interviewed(row))
            item['offers'] += int(row['status'] == 'offer')
        elif row['status'] in ('withdrawn', 'closed'):
            item['excluded'] += 1
        else:
            item['pending'] += 1
    for item in calibration.values():
        n = item['resolved']
        item['interview_rate'] = round(item['interviews'] / n * 100, 1) if n >= 5 else None
        item['offer_rate'] = round(item['offers'] / n * 100, 1) if n >= 5 else None
        item['rate_note'] = 'Resolved applications only' if n >= 5 else 'Insufficient sample (minimum 5 resolved applications)'

    response_times = []
    for row in conn.execute(
        """SELECT a.applied_at,MIN(e.event_at) response_at FROM applications a
           JOIN application_events e USING(application_id)
           WHERE a.applied_at IS NOT NULL AND e.event_type IN
             ('email_rejected','email_interviewing','email_mid_convo')
           GROUP BY a.application_id"""
    ).fetchall():
        try:
            elapsed = (_parse_datetime(row["response_at"]) - _parse_datetime(row["applied_at"])).total_seconds() / 86400
            if elapsed >= 0:
                response_times.append(elapsed)
        except (TypeError, ValueError):
            pass
    response_times.sort()
    median_days = None
    if response_times:
        middle = len(response_times) // 2
        median_days = response_times[middle] if len(response_times) % 2 else (
            response_times[middle - 1] + response_times[middle]) / 2

    report = {
        "week_start": start_iso, "week_end": (end - timedelta(days=1)).isoformat(),
        "new_applications": applications, "interviews": interviews, "rejections": rejections,
        "overdue_followups": overdue, "pending_email_reviews": pending_reviews,
        "rate_basis": "Applications submitted this week; outcomes observed through report generation",
        "response_rate": round(sum(bool(r['responded'] or r['status'] in ('mid_convo','interviewing','offer','rejected')) for r in cohort) / len(cohort) * 100, 1) if cohort else None,
        "interview_conversion": round(sum(ever_interviewed(r) for r in cohort) / len(cohort) * 100, 1) if cohort else None,
        "median_days_to_response": round(median_days, 1) if median_days is not None else None,
        "current_funnel": statuses,
        "by_source": list(sources.values()),
        "by_score_band": list(bands.values()),
        "calibration": list(calibration.values()),
        "score_basis": "Score frozen at submission; legacy scores are unknown, not backfilled",
        "calibration_note": "Advisory offer/rejection outcome rates; pending, withdrawn and closed applications excluded, minimum 5 per group. No automatic tuning.",
        "events": event_counts,
    }
    conn.execute(
        """INSERT INTO weekly_reports(week_start,week_end,report_json,generated_at)
           VALUES (?,?,?,?) ON CONFLICT(week_start) DO UPDATE SET
           week_end=excluded.week_end,report_json=excluded.report_json,generated_at=excluded.generated_at""",
        (start_iso, report["week_end"], json.dumps(report), utc_now()),
    )
    conn.commit()
    conn.close()
    return report
