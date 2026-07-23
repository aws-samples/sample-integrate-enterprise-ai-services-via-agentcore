"""In-memory mock knowledge base + the 4 tool implementations.

Everything here is MOCK data held in process memory. A real service would back
these with a document store / search index and a durable feedback table. Keeping
it in memory is enough to demonstrate the MCP tool surface for the demo.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

# --- mock document corpus ---------------------------------------------------
# Each document has: id, title, body, updated_at (ISO-8601 UTC).
_DOCUMENTS: list[dict] = [
    {
        "id": "KB-001",
        "title": "Employee Onboarding Guide",
        "body": (
            "Welcome to the company. This guide covers your first-week checklist: "
            "setting up SSO, enrolling in benefits, requesting hardware, and booking "
            "your orientation session with the People team."
        ),
        "updated_at": "2026-07-20T09:15:00Z",
    },
    {
        "id": "KB-002",
        "title": "VPN and Remote Access Policy",
        "body": (
            "All remote access must go through the corporate VPN using your Okta "
            "credentials and an approved device. Split tunneling is disabled. Contact "
            "the IT service desk to enroll a new laptop before your first remote day."
        ),
        "updated_at": "2026-07-22T14:40:00Z",
    },
    {
        "id": "KB-003",
        "title": "Expense Reimbursement Procedure",
        "body": (
            "Submit expenses within 30 days via the finance portal. Attach itemized "
            "receipts for anything over 25 USD. Travel bookings should use the "
            "preferred agency to qualify for automatic reimbursement."
        ),
        "updated_at": "2026-07-18T11:05:00Z",
    },
    {
        "id": "KB-004",
        "title": "Incident Response Runbook",
        "body": (
            "When a production incident is declared, page the on-call engineer, open "
            "an incident channel, and assign an incident commander. Sev-1 issues "
            "require a status update every 30 minutes until mitigation is confirmed."
        ),
        "updated_at": "2026-07-23T08:00:00Z",
    },
    {
        "id": "KB-005",
        "title": "Data Classification Standard",
        "body": (
            "Data is classified as Public, Internal, Confidential, or Restricted. "
            "Confidential and Restricted data must be encrypted at rest and in transit "
            "and may never be copied to unmanaged devices or personal cloud storage."
        ),
        "updated_at": "2026-07-21T16:30:00Z",
    },
]

# In-memory feedback log (mock). Each entry: ack_id, doc_id, feedback, received_at.
_FEEDBACK: list[dict] = []

_SNIPPET_LEN = 160


def _snippet(body: str) -> str:
    """Return a short preview of a document body."""
    body = body.strip()
    if len(body) <= _SNIPPET_LEN:
        return body
    return body[:_SNIPPET_LEN].rstrip() + "..."


def search_knowledge_base(query: str) -> dict:
    """Search internal docs; return matching titles + snippets.

    Simple case-insensitive substring match over title and body. Returns the
    query, a match count, and the list of matching documents (id/title/snippet).
    """
    q = (query or "").strip().lower()
    matches: list[dict] = []
    if q:
        for doc in _DOCUMENTS:
            if q in doc["title"].lower() or q in doc["body"].lower():
                matches.append(
                    {
                        "id": doc["id"],
                        "title": doc["title"],
                        "snippet": _snippet(doc["body"]),
                        "updated_at": doc["updated_at"],
                    }
                )
    return {"query": query, "count": len(matches), "results": matches}


def get_document(doc_id: str) -> dict:
    """Return the full content of a document by id, or a not-found marker."""
    for doc in _DOCUMENTS:
        if doc["id"] == doc_id:
            return {"found": True, "document": doc}
    return {"found": False, "document": None, "message": f"No document with id {doc_id!r}"}


def list_recent_updates(limit: int = 5) -> dict:
    """List the most recently updated documents (default 5).

    Sorted by updated_at descending. `limit` is clamped to the range 1..50.
    """
    limit = max(1, min(int(limit), 50))
    ordered = sorted(_DOCUMENTS, key=lambda d: d["updated_at"], reverse=True)
    recent = [
        {"id": d["id"], "title": d["title"], "updated_at": d["updated_at"]}
        for d in ordered[:limit]
    ]
    return {"count": len(recent), "updates": recent}


def submit_feedback(doc_id: str, feedback: str) -> dict:
    """Record feedback for a document and return an acknowledgement id."""
    known = any(d["id"] == doc_id for d in _DOCUMENTS)
    ack_id = f"FB-{uuid.uuid4().hex[:12]}"
    entry = {
        "ack_id": ack_id,
        "doc_id": doc_id,
        "feedback": feedback,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    _FEEDBACK.append(entry)
    return {
        "ack_id": ack_id,
        "doc_id": doc_id,
        "recorded": True,
        "known_document": known,
    }
