"""Adapters: the WritePlan executor (apply ``WritePlanItem[]`` in one
``Session.begin()`` then run the recompute worklist) and the in-memory
operations projector (project a post-mutation ``SpecFull`` without persisting,
so the gate can score as-if-applied).
"""
