"""Migration v001: Initial schema for profiles, scans, checkpoints, jobs, operations, events, receipts, and results."""

from __future__ import annotations

import sqlite3

VERSION = 1
DESCRIPTION = "Initial core tables for octodot durable state"


def up(conn: sqlite3.Connection) -> None:
    """Apply migration v001."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            profile TEXT PRIMARY KEY,
            reconciled_epoch INTEGER NOT NULL DEFAULT 0,
            journal_seq INTEGER NOT NULL DEFAULT 0,
            identity_validated INTEGER NOT NULL DEFAULT 0,
            validated_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scans (
            scan_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            coverage_json TEXT,
            details_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            scan_id TEXT NOT NULL,
            position TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (scan_id) REFERENCES scans (scan_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            details_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            profile TEXT,
            profile_epoch INTEGER,
            source TEXT,
            repository TEXT,
            starting_branch TEXT,
            session TEXT,
            ticket_id TEXT,
            api_accepted INTEGER NOT NULL DEFAULT 0,
            effect_observed INTEGER NOT NULL DEFAULT 0,
            attribution TEXT NOT NULL DEFAULT '',
            ui_verified INTEGER NOT NULL DEFAULT 0,
            accepted_identity_unverified INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT,
            session_id TEXT,
            transition_id TEXT,
            acked INTEGER NOT NULL DEFAULT 0,
            acked_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS receiver_receipts (
            receipt_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            receiver_accepted INTEGER NOT NULL DEFAULT 0,
            channel_send_accepted INTEGER NOT NULL DEFAULT 0,
            delivery_unknown INTEGER NOT NULL DEFAULT 0,
            channel TEXT,
            timestamp TEXT,
            metadata_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS action_results (
            action_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            op TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            error_code TEXT,
            coverage_json TEXT,
            data_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
