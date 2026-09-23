"""Proseth Worker - the deploy agent that runs inside a customer's network.

See `agent.py` for the loop and `jobs.py` for what it can be asked to do.
"""
from .agent import VERSION, main

__all__ = ["VERSION", "main"]
