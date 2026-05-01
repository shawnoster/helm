"""Effect routes — POST /effects/<name>.

Each route shells out to a stdlib-only Python helper bundled at
/usr/local/bin/ in the container (see Dockerfile). The helpers send
one HTTP PUT to the Nanoleaf controller and exit immediately; the
animation loop runs on the panels themselves until something else
changes their state.

Concurrency: a module-level lock serialises requests so that a
previous helper subprocess is terminated before a new one is spawned.
In practice the helpers exit in tens of milliseconds, but the guard
makes the contract well-defined under bursty input.
"""

import shutil
import subprocess
import threading
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

# Auth is applied by the parent `authenticated` router in app.main; declaring
# it here too would double-evaluate the dependency on every request.
router = APIRouter(
    prefix="/effects",
    tags=["effects"],
)

_KITT_BINARY = shutil.which("nanoleaf-kitt") or "/usr/local/bin/nanoleaf-kitt"

_kitt_lock = threading.Lock()
_kitt_proc: subprocess.Popen[bytes] | None = None


def _terminate_safely(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort terminate-then-kill. Tolerant of races and stuck processes.

    Three known failure modes:
    - The process exits between our `poll()` check and `terminate()` →
      `terminate()` raises `ProcessLookupError`. Harmless; nothing left to kill.
    - SIGTERM doesn't take effect within 2s → escalate to SIGKILL.
    - SIGKILL also fails to reap (zombie/stuck), or `wait()` times out again →
      give up and let the new Popen replace the reference. Better to over-spawn
      and let the OS clean up than 500 the route.
    """
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            return


class KittArgs(BaseModel):
    """Optional knobs for the KITT scanner; defaults match `nanoleaf-kitt`'s CLI."""

    color: str = "red"
    period: float = Field(default=1.8, gt=0, description="Cycle time in seconds; must be positive.")
    trail: int = Field(default=4, ge=1, description="Trail length in panels; must be >= 1.")


@router.post("/kitt", status_code=status.HTTP_202_ACCEPTED)
def kitt(args: KittArgs | None = None) -> dict[str, Any]:
    """Spawn `nanoleaf-kitt` as a fire-and-forget subprocess.

    Body is fully optional — every field has a default, and a missing
    body is equivalent to `{}`. Kills any previous still-running
    `nanoleaf-kitt` subprocess first to keep the panels under
    single-writer control.
    """
    global _kitt_proc

    if args is None:
        args = KittArgs()

    cmd = [
        _KITT_BINARY,
        "--color",
        args.color,
        "--period",
        str(args.period),
        "--trail",
        str(args.trail),
    ]

    with _kitt_lock:
        if _kitt_proc is not None and _kitt_proc.poll() is None:
            _terminate_safely(_kitt_proc)

        _kitt_proc = subprocess.Popen(  # noqa: S603 — fixed binary path, validated args
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    return {"started": True, "args": args.model_dump()}
