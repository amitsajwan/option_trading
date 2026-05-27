"""One-shot Mongo migrations for the sim/replay subsystem and other ops.

Each migration here is idempotent: safe to run repeatedly with no effect
after the first successful run. See individual modules for specifics.
"""
