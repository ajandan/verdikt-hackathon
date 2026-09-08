"""Inbox agent: a business stakeholder emails a template to a monitored
mailbox, the agent ingests it, runs the design-time pipeline (upload →
field detect → mapping → test render), and replies with the result.

Off by default. Enabled via `DOCGEN_INBOX_ENABLED=true` plus IMAP + SMTP
credentials. Per-tenant behavior (sender allow-list, subject tag,
auto-publish threshold, extra CCs) lives in `tenants.inbound_config`.
"""
