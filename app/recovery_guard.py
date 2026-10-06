"""A restored tree is inert until an explicitly reviewed cutover removes its guard."""
from pathlib import Path


def assert_execution_allowed(*paths):
    for value in paths:
        if not value or str(value)==':memory:':
            continue
        path=Path(value).resolve()
        for directory in path.parents:
            if (directory/'RESTORE_DISABLED').exists():
                raise RuntimeError('Recovery tree is disabled: reconciliation and authorized cutover required')


def assert_database_execution_allowed(con):
    assert_execution_allowed(*(row[2] for row in con.execute('PRAGMA database_list')))
