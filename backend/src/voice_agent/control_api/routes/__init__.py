"""Route handlers for the approved Phase 0 endpoint catalogue (docs/04 §23).

Handlers validate input, call one application service, and wrap the result
in the approved envelope. They never stream audio or run provider work.
"""
