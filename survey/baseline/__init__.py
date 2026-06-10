"""Baseline ("expected spectrum") sources. Each source is isolated behind the
BaselineSource interface with disk caching and graceful degradation — the sweep
must still run with zero internet (static allocation table only, labeled
"degraded baseline")."""
