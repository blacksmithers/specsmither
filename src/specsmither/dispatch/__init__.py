"""Dispatch facade + wire envelopes + error guidance (the agent surface seam).

``facade`` is the single module that imports lifecycle + operations + gates (the
façade rule); it routes the 18 MCP tools to lifecycle verbs (forwarded verbatim) or
operations primitives (thin composers) and returns one of the three content
shapes from ``envelopes`` (lifecycle / bare success / standard_error). Domain
errors travel as the ``standard_error`` content envelope (``error_guidance``),
never as protocol errors.
"""
