"""Proseth Worker - the deploy agent that runs inside a customer's network.

See `agent.py` for the loop and `jobs.py` for what it can be asked to do.

**Deliberately imports nothing.** This used to re-export `VERSION` and `main`
from `.agent`, which meant importing the package imported that submodule - and
then `python -m proseth_worker.agent` re-executed a module already in
`sys.modules`, so every run of the documented `proseth-worker --check` began
with:

    RuntimeWarning: 'proseth_worker.agent' found in sys.modules after import of
    package 'proseth_worker', but prior to execution of 'proseth_worker.agent';
    this may result in unpredictable behaviour

Nothing used the re-export, so the cause is gone rather than the warning
suppressed. Import from the module that owns it: `from .agent import VERSION`.
"""
