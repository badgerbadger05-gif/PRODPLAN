"""Real PostgreSQL regression for the rebase/executor writer boundary."""
import concurrent.futures
import os
import threading
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.schema import CreateTable

from app import models
from app.services.specification_mrp_rebase import rebase_fixed_plan_remaining_roots


def probe_rebase_writer_boundary(database_url: str) -> None:
    # This probe owns only an isolated test schema, never production tables.
    assert (make_url(database_url).database or '').startswith('test_')
    schema = 'test_rebase_lock_' + uuid.uuid4().hex
    admin = create_engine(database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(database_url, connect_args={'options': f'-csearch_path={schema}'})
    ready = threading.Event()

    def rebase():
        with Session(engine) as session:
            session.connection()  # Establish the second connection before timing.
            ready.set()
            try:
                rebase_fixed_plan_remaining_roots(session, 999999)
            except ValueError as exc:
                return str(exc)
            raise AssertionError('The isolated test has no accepted planning truth')

    try:
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE ledger_generation (id BIGINT PRIMARY KEY)'))
            models.PlanningTruthState.__table__.create(connection)
            # The run is deliberately absent; its normal lookup still needs
            # its real column shape. No unrelated FK targets are needed here.
            connection.execute(CreateTable(models.PlanningRun.__table__, include_foreign_key_constraints=[]))
            connection.execute(models.PlanningTruthState.__table__.insert().values(id=1))
        configure_mappers()  # Mapper startup must not imitate a blocked writer.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as executor:
                executor.execute(text('SELECT id FROM planning_truth_state WHERE id=1 FOR UPDATE'))
                future = pool.submit(rebase)
                assert ready.wait(5), 'Second PostgreSQL connection did not start'
                # A rebase must wait for the executor, including before validating
                # or locking its predecessor. The former order returned or touched
                # domain rows while the executor held this boundary.
                try:
                    future.result(timeout=0.5)
                except concurrent.futures.TimeoutError:
                    pass
                else:
                    raise AssertionError('Rebase bypassed the active executor writer')
            assert future.result(timeout=5) == 'current accepted planning truth is unavailable'
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_rebase_waits_for_existing_executor_writer():
    import pytest
    database_url = os.environ.get('PRODPLAN_REBASE_LOCK_TEST_URL')
    if not database_url:
        pytest.skip('requires an isolated PostgreSQL test database')
    probe_rebase_writer_boundary(database_url)


if __name__ == '__main__':
    probe_rebase_writer_boundary(os.environ['PRODPLAN_REBASE_LOCK_TEST_URL'])
    print('PostgreSQL rebase/executor writer boundary verified')
