"""Phase 0 evaluation harness (docs/03 §24, docs/14 §18 WP12).

Provider-independent: the versioned docs/17 catalog (``catalog*``),
deterministic assertions (``text_checks``, ``system_checks``), scoring,
the runner over the WP5 evaluation repositories, release gates, and the
baseline report. Executors (offline fakes, or approved live providers)
are injected; this package never imports an adapter or persistence module.
"""
