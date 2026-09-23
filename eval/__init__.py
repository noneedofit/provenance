"""W7: evaluation harness and independent gold labels for the Signalpost agent.

Sub-modules:
- splits: frozen, stable-hash dev/val/test/stress splits from the universe file, plus a
  daily_like() sampler that mimics the organiser's uniform daily batch.
- gold: schema + I/O helpers for the independently-researched gold-label collection.
- score: proxy competition scorer -- envelope contract checks, coverage/precision/recall vs
  gold, refresh checks, budget checks, and a weighted proxy total.
- report: compares two score() outputs (baseline vs challenger) and applies the promotion rule.
"""
