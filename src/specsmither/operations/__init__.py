"""Operations surface: CRUD primitives, queries, search, reports, lookup, the
error model, reopen, and link-pull-request. Every mutation runs the recompute
worklist inside its own ``Session.begin()``.
"""
