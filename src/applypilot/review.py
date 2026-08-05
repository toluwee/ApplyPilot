"""Human-in-the-loop review TUI for approving/rejecting scored jobs before tailoring."""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from applypilot.database import get_connection, get_pending_review, set_approval

console = Console()


def _render_job(job: dict, index: int, total: int) -> None:
    """Print a single job's details to the console."""
    score = job.get("fit_score", "?")
    title = job.get("title", "Unknown")
    site = job.get("site", "")
    location = job.get("location", "")
    url = job.get("url", "")
    reasoning = job.get("score_reasoning", "")
    full_description = job.get("full_description", "")

    score_color = "green" if (isinstance(score, int) and score >= 7) else "yellow"

    console.print()
    console.print(Rule(
        f"[bold]Job {index}/{total}[/bold]  [{score_color}]Score {score}/10[/{score_color}]  "
        f"[bold]{title}[/bold]  [dim]{site} · {location}[/dim]"
    ))

    if reasoning:
        console.print(Panel(
            reasoning[:500] + ("..." if len(reasoning) > 500 else ""),
            title="[cyan]Score Reasoning[/cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))

    if full_description:
        preview = full_description[:800] + ("..." if len(full_description) > 800 else "")
        console.print(Panel(
            preview,
            title="[yellow]Job Description[/yellow]",
            border_style="yellow",
            padding=(0, 1),
        ))

    console.print(f"  [dim]URL: {url}[/dim]")


def _prompt_decision() -> tuple[str, str]:
    """Prompt for approve/reject/skip/quit. Returns (decision, note)."""
    console.print(
        "\n  [bold green][A][/bold green]pprove  "
        "[bold red][R][/bold red]eject  "
        "[bold yellow][S][/bold yellow]kip  "
        "[bold][Q][/bold]uit"
    )
    while True:
        try:
            raw = input("  Decision: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit", ""

        if raw in ("a", "approve"):
            return "approved", ""
        if raw in ("r", "reject"):
            try:
                note = input("  Reason (optional, Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                note = ""
            return "rejected", note
        if raw in ("s", "skip"):
            return "skip", ""
        if raw in ("q", "quit"):
            return "quit", ""
        console.print("  [dim]Please enter A, R, S, or Q.[/dim]")


def _print_summary(approved: int, rejected: int, skipped: int) -> None:
    console.print()
    console.print(Rule("Review Complete"))
    console.print(
        f"  [green]Approved: {approved}[/green]  "
        f"[red]Rejected: {rejected}[/red]  "
        f"[dim]Skipped: {skipped}[/dim]"
    )
    if approved:
        console.print(
            f"\n  Run [bold]applypilot run tailor cover[/bold] "
            f"to generate tailored resumes for your {approved} approved job(s),\n"
            f"  then [bold]applypilot apply --approved-only[/bold] to submit."
        )
    console.print()


def run_review(
    list_: bool = False,
    approve: str | None = None,
    reject: str | None = None,
    note: str = "",
) -> None:
    """Entry point for the review command.

    Args:
        list_: Print pending jobs table and exit.
        approve: Approve a specific job by URL.
        reject: Reject a specific job by URL.
        note: Note to attach to approve/reject decision.
    """
    conn = get_connection()

    # --- Non-interactive: --approve / --reject ---
    if approve:
        set_approval(approve, "approved", notes=note, conn=conn)
        console.print(f"[green]✓ Approved:[/green] {approve}")
        return

    if reject:
        set_approval(reject, "rejected", notes=note, conn=conn)
        console.print(f"[red]✗ Rejected:[/red] {reject}")
        return

    # --- Non-interactive: --list ---
    if list_:
        jobs = get_pending_review(conn)
        if not jobs:
            console.print("[dim]No jobs awaiting review.[/dim]")
            return

        table = Table(
            title=f"Pending Review ({len(jobs)} jobs)",
            box=box.SIMPLE,
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Score", justify="center", width=6)
        table.add_column("Title", max_width=40)
        table.add_column("Site", width=16)
        table.add_column("URL", max_width=50, no_wrap=True, style="dim")

        for i, job in enumerate(jobs, 1):
            score = job.get("fit_score", "?")
            color = "green" if isinstance(score, int) and score >= 7 else "yellow"
            table.add_row(
                str(i),
                f"[{color}]{score}[/{color}]",
                job.get("title", ""),
                job.get("site", ""),
                job.get("url", ""),
            )

        console.print()
        console.print(table)
        console.print(
            "  Run [bold]applypilot review[/bold] (no flags) to approve/reject interactively.\n"
        )
        return

    # --- Interactive review ---
    jobs = get_pending_review(conn)
    if not jobs:
        console.print("\n[dim]No jobs awaiting review.[/dim]")
        console.print(
            "  Run [bold]applypilot run score[/bold] first to score jobs,\n"
            "  or [bold]applypilot dashboard[/bold] to review in your browser.\n"
        )
        return

    total = len(jobs)
    console.print(f"\n[bold]Reviewing {total} job(s)[/bold] — press Q at any time to quit.\n")

    approved_count = rejected_count = skipped_count = 0

    for i, job in enumerate(jobs, 1):
        _render_job(job, i, total)
        decision, job_note = _prompt_decision()

        if decision == "approved":
            set_approval(job["url"], "approved", notes=job_note, conn=conn)
            console.print("  [green]✓ Approved[/green]")
            approved_count += 1
        elif decision == "rejected":
            set_approval(job["url"], "rejected", notes=job_note, conn=conn)
            console.print("  [red]✗ Rejected[/red]" + (f" — {job_note}" if job_note else ""))
            rejected_count += 1
        elif decision == "skip":
            console.print("  [dim]— Skipped[/dim]")
            skipped_count += 1
        elif decision == "quit":
            skipped_count += total - i
            break

    _print_summary(approved_count, rejected_count, skipped_count)
