"""Config loader — reads claudefairy.toml + .env.

Responsibilities:
- Load .env via python-dotenv (populates os.environ BEFORE secrets.py)
- Parse claudefairy.toml (tomli) into a typed DaemonConfig
- Provide sane defaults for optional fields
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from dotenv import load_dotenv


@dataclass
class TargetProject:
    name: str
    path: str
    description: str = ""
    allow_write: bool = False
    primary: bool = False


@dataclass
class BudgetConfig:
    daily_token_limit: int = 500_000
    per_task_token_limit: int = 50_000
    source_fetch_qps: int = 2
    source_fetch_backoff: bool = True
    over_budget_mode: str = "report_only"  # report_only | pause | alert_only


@dataclass
class ScoutConfig:
    min_stars_search: int = 100
    min_stars_trending: int = 50
    l2_min_score: float = 6.0
    l3_notify_score: float = 8.0
    languages: list[str] = field(default_factory=lambda: ["rust", "python", "typescript"])
    keywords: list[str] = field(default_factory=list)


@dataclass
class RetryConfig:
    max_retries: int = 3
    backoff_base_secs: int = 30
    backoff_max_secs: int = 300
    transient_errors: list[str] = field(
        default_factory=lambda: ["rate_limit", "timeout", "connection_error"]
    )


@dataclass
class TriageConfig:
    max_retries: int = 3              # max triage retries before marking failed
    model: str = "claude-sonnet-4-6"  # model used for triage (readonly)
    timeout_secs: int = 60            # timeout per triage Claude call
    queue_scan_interval_secs: int = 60  # how often scheduler scans for unclaimed tasks


@dataclass
class NotificationConfig:
    quiet_hours_start: str = "23:00"  # local time, HH:MM
    quiet_hours_end: str = "08:00"    # local time, HH:MM
    # P0 tasks bypass quiet hours


@dataclass
class DaemonConfig:
    log_level: str = "info"
    log_dir: str = "data/logs"
    db_path: str = "data/claudefairy.db"
    scout_interval_secs: int = 3600
    daily_report_time: str = "09:00"
    targets: list[TargetProject] = field(default_factory=list)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    scout: ScoutConfig = field(default_factory=ScoutConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    triage: TriageConfig = field(default_factory=TriageConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    lock_ttl_minutes: int = 60
    approval_default_ttl_minutes: int = 30


def load_config(
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> DaemonConfig:
    """Load .env (into os.environ) then parse claudefairy.toml."""

    # 1. Load .env first so secrets are available
    env_file = env_path or Path(".env")
    if env_file.exists():
        load_dotenv(env_file, override=False)

    # 2. Find config file
    toml_path = config_path or Path("claudefairy.toml")
    if not toml_path.exists():
        # Fall back to looking next to the package root
        candidate = Path(__file__).parent.parent.parent.parent / "claudefairy.toml"
        if candidate.exists():
            toml_path = candidate

    if not toml_path.exists():
        return DaemonConfig()  # all defaults

    with open(toml_path, "rb") as f:
        raw = tomllib.load(f)

    daemon_raw = raw.get("daemon", {})
    cfg = DaemonConfig(
        log_level=daemon_raw.get("log_level", "info"),
        log_dir=daemon_raw.get("log_dir", "data/logs"),
        db_path=daemon_raw.get("db_path", "data/claudefairy.db"),
        scout_interval_secs=daemon_raw.get("scout_interval_secs", 3600),
        daily_report_time=daemon_raw.get("daily_report_time", "09:00"),
    )

    # Targets
    targets_raw = raw.get("targets", {})
    for proj in targets_raw.get("projects", []):
        cfg.targets.append(
            TargetProject(
                name=proj["name"],
                path=proj["path"],
                description=proj.get("description", ""),
                allow_write=proj.get("allow_write", False),
                primary=proj.get("primary", False),
            )
        )

    # Budget
    b = raw.get("budget", {})
    cfg.budget = BudgetConfig(
        daily_token_limit=b.get("daily_token_limit", 500_000),
        per_task_token_limit=b.get("per_task_token_limit", 50_000),
        source_fetch_qps=b.get("source_fetch_qps", 2),
        source_fetch_backoff=b.get("source_fetch_backoff", True),
        over_budget_mode=b.get("over_budget_mode", "report_only"),
    )

    # Scout
    s = raw.get("scout", {})
    cfg.scout = ScoutConfig(
        min_stars_search=s.get("min_stars_search", 100),
        min_stars_trending=s.get("min_stars_trending", 50),
        l2_min_score=s.get("l2_min_score", 6.0),
        l3_notify_score=s.get("l3_notify_score", 8.0),
        languages=s.get("languages", ["rust", "python", "typescript"]),
        keywords=s.get("keywords", []),
    )

    # Retry
    r = raw.get("retry", {})
    cfg.retry = RetryConfig(
        max_retries=r.get("max_retries", 3),
        backoff_base_secs=r.get("backoff_base_secs", 30),
        backoff_max_secs=r.get("backoff_max_secs", 300),
        transient_errors=r.get("transient_errors", ["rate_limit", "timeout", "connection_error"]),
    )

    lock_raw = raw.get("lock", {})
    cfg.lock_ttl_minutes = lock_raw.get("ttl_minutes", 60)

    approval_raw = raw.get("approval", {})
    cfg.approval_default_ttl_minutes = approval_raw.get("default_ttl_minutes", 30)

    # Triage
    t = raw.get("triage", {})
    cfg.triage = TriageConfig(
        max_retries=t.get("max_retries", 3),
        model=t.get("model", "claude-sonnet-4-6"),
        timeout_secs=t.get("timeout_secs", 60),
        queue_scan_interval_secs=t.get("queue_scan_interval_secs", 60),
    )

    # Notification
    n = raw.get("notification", {})
    cfg.notification = NotificationConfig(
        quiet_hours_start=n.get("quiet_hours_start", "23:00"),
        quiet_hours_end=n.get("quiet_hours_end", "08:00"),
    )

    return cfg
