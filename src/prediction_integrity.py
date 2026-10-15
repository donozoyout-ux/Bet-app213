"""Install database enforcement without rewriting any historical rows."""
from sqlalchemy import text


async def install_prediction_guards(conn):
    if conn.dialect.name == 'sqlite':
        for table in ('prediction_snapshots', 'prediction_settlements', 'learning_reports', 'learning_slices', 'learning_lessons'):
            operations = ('UPDATE', 'DELETE')
            for operation in operations:
                await conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable prediction history'); END"))
    elif conn.dialect.name == 'postgresql':
        schema = conn.sync_connection.get_execution_options().get('schema_translate_map', {}).get(None)
        quote = conn.dialect.identifier_preparer.quote
        prefix = quote(schema) + '.' if schema else ''
        # Only widen metadata; historical version strings are never rewritten.
        length = await conn.scalar(text("SELECT character_maximum_length FROM information_schema.columns WHERE table_schema=COALESCE(:schema,current_schema()) AND table_name='prediction_snapshots' AND column_name='model_version'"), {'schema': schema})
        if length is not None and length < 100:
            await conn.execute(text(f'ALTER TABLE {prefix}prediction_snapshots ALTER COLUMN model_version TYPE VARCHAR(100)'))
        function = prefix + 'reject_prediction_history_change'
        await conn.execute(text(f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable prediction history'; END $$"))
        for table in ('prediction_snapshots', 'prediction_settlements', 'learning_reports', 'learning_slices', 'learning_lessons'):
            target = prefix + table
            await conn.execute(text(f'DROP TRIGGER IF EXISTS immutable_history ON {target}'))
            await conn.execute(text(f'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {target} FOR EACH ROW EXECUTE FUNCTION {function}()'))
