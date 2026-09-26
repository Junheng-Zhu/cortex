---
name: Python Debugging
description: Diagnose Python exceptions using a minimal reproduction and traceback-first workflow.
---
# Python debugging workflow

1. Preserve the complete exception type, message, and traceback.
2. Reduce the failure to the smallest reproducible input before changing code.
3. Form one hypothesis at a time and verify it with a focused test.
4. Add a regression test before considering the repair complete.

Read `references/tracebacks.md` only when traceback interpretation is needed.
