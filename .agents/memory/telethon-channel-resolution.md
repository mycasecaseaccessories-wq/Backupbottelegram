---
name: Telethon numeric channel-ID resolution
description: Why forwarding to a channel by numeric ID fails with "Could not find the input entity" and how backfill dedup can be poisoned.
---

# Telethon numeric channel-ID resolution

Telethon cannot resolve a channel by numeric ID (`PeerChannel`) unless that
channel's access-hash is already in the session's entity cache. A fresh session
that has never "seen" the channel raises `Could not find the input entity`.

**Why:** access hashes are per-account and only get cached after the client has
encountered the entity (e.g. via `get_dialogs()` / iterating dialogs). The
numeric ID alone is not enough.

**How to apply:** when resolving a destination channel by ID, on failure call
`get_dialogs()` to populate the cache, then retry `get_entity`. Only cache the
resolved entity on success. If it still can't resolve, the account is almost
certainly not a member of that channel — abort rather than falling back to a
default channel.

# Backfill dedup ordering

Log a message as backed-up ONLY after the forward succeeds. If you log before
forwarding and the forward fails, the dedup check permanently skips that message
on every future retry, so it never gets backed up ("phantom" log rows).

**Why:** dedup keys off the logs table; a row means "already done".
**How to apply:** forward first, write the log row second. On unresolved
destination, abort early (leave target pending) so a later retry re-forwards.

# Per-user target channel lookup

A per-target backup channel must be looked up scoped by `user_id`, not by
username alone. Multiple users can register a target with the same username; an
unscoped `LIMIT 1` lookup returns the wrong user's row (often a NULL channel),
silently forwarding to the global default.
