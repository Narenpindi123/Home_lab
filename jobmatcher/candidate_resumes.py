"""Shared, explicit resume ownership for scheduled and dashboard matching."""
import hashlib
import os
from pathlib import Path

CANDIDATE_RESUMES = {
    "candidate_a": [str(Path(os.environ.get("RESUME_DIR", "/app/resumes")) / "candidate_a.pdf")],
    "candidate_b": [str(Path(os.environ.get("RESUME_DIR", "/app/resumes")) / "candidate_b.pdf")],
}


def resume_fingerprint(paths):
    """Detect replacement, deletion, or arrival without restarting the dashboard."""
    result = []
    for path in paths:
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except FileNotFoundError:
            digest = None
        result.append((str(path), digest))
    return tuple(result)
