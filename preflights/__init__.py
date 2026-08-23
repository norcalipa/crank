# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Deterministic preflight scripts for OpenClaw maintenance jobs.

These scripts are read-only and designed to be invoked by OpenClaw's
event-trigger system. They return JSON on stdout and exit 0 on success
(regardless of whether work should fire). A non-zero exit indicates a
preflight failure (API error, rate limit, etc.) that an operator should
investigate.
"""
