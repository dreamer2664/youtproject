"""Sentry crash reporting (student-pack freebie).

init_sentry(cfg, command) runs once from main() before dispatch, so every
command (bot, crew, generate, ...) reports uncaught exceptions automatically
via the SDK's global excepthook. No DSN configured (or SDK missing) = a
silent no-op that never breaks a run. The DSN lives in config.yaml
(gitignored) or SENTRY_DSN env — never in code; this repo is public.
"""

from __future__ import annotations


def init_sentry(cfg, command: str = "") -> bool:
    """Initialise crash reporting. True when active, False when skipped."""
    try:
        dsn = (cfg.sentry_dsn or "").strip()
    except Exception:  # noqa: BLE001 - reporting must never break startup
        return False
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=cfg.sentry_environment or "production",
            send_default_pii=False,
            traces_sample_rate=0.0,  # errors only; transactions would eat quota
        )
        if command:
            sentry_sdk.set_tag("command", command)
        return True
    except Exception:  # noqa: BLE001 - same as above
        return False
