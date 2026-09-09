"""The three pipeline stages, one module each.

Each stage has a single async entry point that takes the previous stage's
output and returns the next stage's input. Types cross the stage boundary as
pydantic models defined in pap_pipeline.schemas.

Stages are Phase 2 work; this package currently holds skeletons only.
"""
