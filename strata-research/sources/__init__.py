"""Typed source clients. One module per source.

Contract for every client: network methods return parsed Pydantic objects AND a
SourceRecord (content-addressed snapshot) so nothing enters the system undated or
unprovenanced. Parsing is split into pure functions that take raw payloads, so the
extraction logic is unit-tested offline without touching the network.
"""
