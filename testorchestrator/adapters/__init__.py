"""Integration adapter seams.

Dagu workflow orchestration, Allure report generation, KRE integration,
the runner agent, and the updater each live behind a dedicated adapter module
so they are never rewritten inside Flask request handlers. Long-running
executions, repository synchronization, and report publication run in separate
worker processes, scheduled outside web request handling.
"""
