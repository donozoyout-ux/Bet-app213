"""Explicit durable historical statistics collection; never queued at startup."""
from .cli import main

if __name__=='__main__':main('stats_backfill')
