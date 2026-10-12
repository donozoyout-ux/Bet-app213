"""Explicit price observation provenance for synthetic test records only."""
def observed_prices(at, *fields):
    return {'_field_observed_at': {field: at.isoformat() for field in fields}}


def move_observations(row, at):
    row.updated_at = at
    row.raw = {**row.raw, '_field_observed_at': {
        field: at.isoformat() for field in row.raw.get('_field_observed_at', {})}}
