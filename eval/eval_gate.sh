#!/usr/bin/env bash
set -euo pipefail
python eval/trace_eval.py --db .trace.db --case calendar_discovery
