"""
CLI entry point — `python -m kinetograph` or `kinetograph` command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.prompt import Prompt

from kinetograph import __version__
from kinetograph.config import settings
from kinetograph.orchestrator import compile_graph
from kinetograph.state import Phase

console = Console()


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _print_banner():
    console.print(
        Panel.fit(
            f"[bold cyan]🎬 KINETOGRAPH[/bold cyan] v{__version__}\n"
            "[dim]Autonomous Multi-Agent Video Orchestration Engine[/dim]",
            border_style="cyan",
        )
    )


def _safe_update(update):
    """Safely extract info from a stream update — may be dict or tuple."""
    if isinstance(update, dict):
        return update
    return {}


def _load_state_from_disk() -> dict | None:
    """Load previously saved pipeline state from the state/ directory."""
    index_path = settings.state_dir / "master_index.json"
    if not index_path.exists():
        return None

    with open(index_path) as f:
        master_index = json.load(f)

    # Rebuild raw_assets from the files referenced in the index
    seen_files = set()
    raw_assets = []
    for entry in master_index:
        fpath = entry.get("asset_file", "")
        if fpath and fpath not in seen_files:
            seen_files.add(fpath)
            p = Path(fpath)
            # Determine asset type from path
            if "b-roll-synth" in fpath:
                atype = "b-roll-synth"
            elif "b-roll" in fpath:
                atype = "b-roll"
            else:
                atype = "a-roll"
            raw_assets.append(
                {
                    "file_path": fpath,
                    "file_name": p.name,
                    "asset_type": atype,
                    "duration_ms": 0,
                    "width": 0,
                    "height": 0,
                    "fps": 0.0,
                    "has_audio": True,
                }
            )

    # Also load paper_edit if it exists
    paper_edit = None
    pe_path = settings.state_dir / "paper_edit.json"
    if pe_path.exists():
        with open(pe_path) as f:
            paper_edit = json.load(f)

    # Load approved_edit if it exists (needed for --resume director)
    approved_edit = None
    ae_path = settings.state_dir / "approved_edit.json"
    if ae_path.exists():
        with open(ae_path) as f:
            approved_edit = json.load(f)

    console.print(f"  [green]✓[/green] Loaded master index: {len(master_index)} entries")
    console.print(f"  [green]✓[/green] Found {len(raw_assets)} source file(s)")
    if paper_edit:
        console.print(
            f"  [green]✓[/green] Loaded existing Paper Edit: {paper_edit.get('title', '?')}"
        )
    if approved_edit:
        console.print(f"  [green]✓[/green] Loaded approved edit: {approved_edit.get('title', '?')}")

    state: dict = {
        "master_index": master_index,
        "raw_assets": raw_assets,
        "paper_edit": paper_edit,
    }
    if approved_edit:
        state["approved_edit"] = approved_edit

    # ── Discover render_path from output/ directory ──
    output_dir = settings.output_dir
    if approved_edit and output_dir.exists():
        title = approved_edit.get("title", "")
        if title:
            safe_title = title.replace(" ", "_")
            # Look for the base render (not mastered, not captioned)
            for candidate in sorted(
                output_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True
            ):
                name = candidate.stem
                if "mastered" in name or "captioned" in name:
                    continue
                if safe_title in name or name in safe_title:
                    state["render_path"] = str(candidate)
                    console.print(f"  [green]✓[/green] Found rendered video: {candidate.name}")
                    break
        # Fallback: most recent non-mastered, non-captioned mp4
        if "render_path" not in state:
            for candidate in sorted(
                output_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True
            ):
                name = candidate.stem
                if "mastered" not in name and "captioned" not in name:
                    state["render_path"] = str(candidate)
                    console.print(
                        f"  [green]✓[/green] Found rendered video (fallback): {candidate.name}"
                    )
                    break

    return state


async def run_pipeline(prompt: str, project_name: str = "untitled", resume_from: str | None = None):
    """Run the full Kinetograph pipeline.

    Args:
        resume_from: Skip to this agent by loading state from disk.
                     e.g. "scripter" loads the master index and starts at scripter.
                     e.g. "scripter" loads the master index and starts at scripter.
    """
    from kinetograph.observability import init_telemetry

    init_telemetry()
    graph = compile_graph() if not resume_from else compile_graph(start_from=resume_from)
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "phase": Phase.IDLE,
        "user_prompt": prompt,
        "project_name": project_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_assets": [],
        "master_index": [],
        "synth_assets": [],
        "errors": [],
        "normalized_clips": {},
        "completed_agents": [],
    }

    # ── Resume: load state from disk ──
    if resume_from:
        console.print(
            f"\n[bold yellow]⏩ Resuming from {resume_from}[/bold yellow] — "
            f"loading state from disk..."
        )
        saved = _load_state_from_disk()
        if not saved:
            console.print("[red]Error: No saved state found in state/ directory.[/red]")
            console.print("[dim]Run the full pipeline first to generate state.[/dim]")
            sys.exit(1)
        initial_state["master_index"] = saved["master_index"]
        initial_state["raw_assets"] = saved["raw_assets"]
        if resume_from in ("human_review",) and saved.get("paper_edit"):
            initial_state["paper_edit"] = saved["paper_edit"]
            initial_state["phase"] = Phase.SCRIPTED
        elif resume_from in ("synthesizer", "director", "captioner", "sound_engineer", "export"):
            # These stages need the approved edit (and paper_edit)
            if saved.get("paper_edit"):
                initial_state["paper_edit"] = saved["paper_edit"]
            if saved.get("approved_edit"):
                initial_state["approved_edit"] = saved["approved_edit"]
            if resume_from in ("captioner", "sound_engineer", "export"):
                # These need the rendered video path
                if saved.get("render_path"):
                    initial_state["render_path"] = saved["render_path"]
                initial_state["phase"] = Phase.RENDERED
            elif resume_from == "director":
                initial_state["phase"] = Phase.APPROVED
            else:
                initial_state["phase"] = Phase.SCRIPTED
        else:
            initial_state["phase"] = Phase.INDEXED
    else:
        console.print(
            f"\n[bold green]▶ Starting pipeline[/bold green] — thread: {thread_id[:8]}..."
        )

    console.print(f"[dim]Prompt: {prompt}[/dim]\n")

    # Run until interrupt or completion
    try:
        async for event in graph.astream(initial_state, config, stream_mode="updates"):
            for node_name, update in event.items():
                update = _safe_update(update)
                phase = update.get("phase", "")
                if phase:
                    console.print(f"  [cyan]⟫[/cyan] {node_name}: phase → [bold]{phase}[/bold]")

                # Check for errors
                new_errors = update.get("errors", [])
                for err in new_errors:
                    if isinstance(err, dict):
                        console.print(
                            f"  [red]✗[/red] [{err.get('agent', '?')}] {err.get('message', '')}"
                        )

        # Check if we hit an interrupt (human_review waiting for approval)
        snapshot = graph.get_state(config)

        if snapshot.next and "human_review" in snapshot.next:
            console.print("\n" + "━" * 60)
            console.print("[bold yellow]⏸  PIPELINE PAUSED — Human Review Required[/bold yellow]")

            # Display the interrupt payload
            tasks = snapshot.tasks
            if tasks:
                for task in tasks:
                    if hasattr(task, "interrupts") and task.interrupts:
                        for intr in task.interrupts:
                            payload = intr.value if hasattr(intr, "value") else intr
                            if isinstance(payload, dict):
                                console.print(f"\n  {payload.get('message', '')}")

            console.print("\n[bold]Options:[/bold]")
            console.print("  [green]approve[/green] — Accept the Paper Edit as-is")
            console.print("  [yellow]edit[/yellow]    — Edit the Paper Edit (opens JSON file)")
            console.print("  [red]reject[/red]  — Reject and re-generate\n")

            choice = Prompt.ask(
                "Decision",
                choices=["approve", "edit", "reject"],
                default="approve",
            )

            if choice == "approve":
                decision = {"action": "approve"}
            elif choice == "reject":
                reason = Prompt.ask("Reason for rejection", default="Not what I wanted")
                decision = {"action": "reject", "reason": reason}
            elif choice == "edit":
                console.print(
                    f"\n[dim]Edit the Paper Edit at:[/dim] "
                    f"{settings.state_dir / 'paper_edit_review.json'}"
                )
                console.print("[dim]Press Enter when done editing...[/dim]")
                input()
                # Re-read the edited file
                import json

                edit_path = settings.state_dir / "paper_edit_review.json"
                with open(edit_path) as f:
                    edited = json.load(f)
                decision = {"action": "approve", "paper_edit": edited}
            else:
                decision = {"action": "approve"}

            # Resume the graph
            console.print("\n[bold green]▶ Resuming pipeline...[/bold green]\n")

            from langgraph.types import Command

            async for event in graph.astream(
                Command(resume=decision), config, stream_mode="updates"
            ):
                for node_name, update in event.items():
                    update = _safe_update(update)
                    phase = update.get("phase", "")
                    if phase:
                        console.print(f"  [cyan]⟫[/cyan] {node_name}: phase → [bold]{phase}[/bold]")

                    new_errors = update.get("errors", [])
                    for err in new_errors:
                        if isinstance(err, dict):
                            console.print(
                                f"  [red]✗[/red] [{err.get('agent', '?')}] {err.get('message', '')}"
                            )

        # Final status
        final_state = graph.get_state(config)
        final_phase = final_state.values.get("phase", Phase.ERROR)
        render_path = final_state.values.get("render_path")
        timeline_path = final_state.values.get("timeline_path")

        console.print("\n" + "━" * 60)
        if final_phase == Phase.COMPLETE:
            console.print("[bold green]✅ Pipeline Complete![/bold green]")
            if render_path:
                console.print(f"  🎬 Video:    {render_path}")
            if timeline_path:
                console.print(f"  📋 Timeline: {timeline_path}")
        else:
            console.print(f"[bold red]❌ Pipeline ended at phase: {final_phase}[/bold red]")
            all_errors = final_state.values.get("errors", [])
            for err in all_errors:
                console.print(f"  [red]•[/red] [{err.get('agent')}] {err.get('message')}")

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠ Pipeline interrupted by user[/yellow]")
    except Exception as exc:
        console.print(f"\n[bold red]💥 Unexpected error: {exc}[/bold red]")
        raise


def main():
    parser = argparse.ArgumentParser(
        prog="kinetograph",
        description="🎬 Kinetograph — Autonomous Multi-Agent Video Orchestration Engine",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    # `kinetograph run`
    run_parser = sub.add_parser("run", help="Run the video editing pipeline")
    run_parser.add_argument(
        "--prompt",
        "-p",
        type=str,
        help="Creative brief / editing instructions",
    )
    run_parser.add_argument(
        "--name",
        "-n",
        type=str,
        default="untitled",
        help="Project name",
    )
    run_parser.add_argument("--verbose", "-v", action="store_true")
    run_parser.add_argument(
        "--resume",
        "-r",
        type=str,
        choices=[
            "scripter",
            "human_review",
            "synthesizer",
            "director",
            "captioner",
            "sound_engineer",
            "export",
        ],
        default=None,
        help="Skip to this agent — loads saved state from state/ directory. "
        "e.g. --resume scripter skips the Archivist and starts from scripting.",
    )

    # `kinetograph serve`
    serve_parser = sub.add_parser("serve", help="Start the web UI server")
    serve_parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if args.command == "run":
        _setup_logging(args.verbose)
        _print_banner()

        prompt = args.prompt
        if not prompt:
            prompt = Prompt.ask("\n[bold]Enter your creative brief[/bold]")

        if not prompt.strip():
            console.print("[red]Error: prompt cannot be empty[/red]")
            sys.exit(1)

        asyncio.run(run_pipeline(prompt, args.name, resume_from=args.resume))

    elif args.command == "serve":
        _setup_logging(args.verbose)
        _print_banner()
        console.print(
            f"\n[bold]Starting web UI on http://{settings.api_host}:{settings.api_port}[/bold]\n"
        )

        import uvicorn

        from kinetograph.server import app

        uvicorn.run(app, host=settings.api_host, port=settings.api_port)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
