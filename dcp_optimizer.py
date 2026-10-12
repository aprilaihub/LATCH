#!/usr/bin/env python3
"""Thin compatibility-free forwarder to the owned contest application."""

from optimizer_pipeline.apps.fpl_contest.cli import main_sync


if __name__ == "__main__":
    raise SystemExit(main_sync())
