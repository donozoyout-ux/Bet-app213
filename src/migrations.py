"""Additive, idempotent migration for existing PostgreSQL/SQLite installations."""
from sqlalchemy import inspect, text


async def migrate_competitions(conn):
    timestamp = 'TIMESTAMP WITH TIME ZONE' if conn.dialect.name == 'postgresql' else 'DATETIME'
    additions = {
        'leagues': {
            'competition_type': "VARCHAR(16) NOT NULL DEFAULT 'club'",
            'enabled': 'BOOLEAN NOT NULL DEFAULT TRUE',
            'backfill_from_year': 'INTEGER NOT NULL DEFAULT 2024',
            'priority': 'INTEGER NOT NULL DEFAULT 100',
            'schedule_format': "VARCHAR(8) NOT NULL DEFAULT 'league'",
            'latest_season': 'VARCHAR(20)', 'verified_at': timestamp,
            'created_at': timestamp, 'updated_at': timestamp,
        },
        'matches': {'round_label': 'VARCHAR(160)', 'stage_key': 'VARCHAR(80)', 'referee_id':'INTEGER REFERENCES referees(id)', 'referee_observed_at':timestamp},
        'scraper_jobs': {'priority': 'INTEGER NOT NULL DEFAULT 100'},
    }
    # Respect isolated PostgreSQL test schemas as well as the production schema.
    schema = conn.get_execution_options().get('schema_translate_map', {}).get(None) if hasattr(conn, 'get_execution_options') else None
    if schema is None:
        schema = conn.sync_connection.get_execution_options().get('schema_translate_map', {}).get(None)
    preparer = conn.dialect.identifier_preparer
    for table, columns in additions.items():
        existing = await conn.run_sync(lambda sync: {column['name'] for column in inspect(sync).get_columns(table, schema=schema)})
        qualified = (preparer.quote_schema(schema) + '.' if schema else '') + preparer.quote(table)
        for name, sql_type in columns.items():
            if name not in existing:
                if schema and 'REFERENCES referees(' in sql_type:
                    sql_type=sql_type.replace('REFERENCES referees(',f'REFERENCES {preparer.quote_schema(schema)}.referees(')
                await conn.execute(text(f'ALTER TABLE {qualified} ADD COLUMN {preparer.quote(name)} {sql_type}'))
    leagues = (preparer.quote_schema(schema) + '.' if schema else '') + 'leagues'
    await conn.execute(text(f'UPDATE {leagues} SET created_at=COALESCE(created_at,CURRENT_TIMESTAMP), updated_at=COALESCE(updated_at,CURRENT_TIMESTAMP) WHERE created_at IS NULL OR updated_at IS NULL'))
