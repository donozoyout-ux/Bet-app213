import re
from .client import SourceError
from .schedule import schedule_sections


def discover_rounds(payload):
    return sorted(schedule_sections(payload))
