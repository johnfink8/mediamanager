-- Mounted into the postgres container at /docker-entrypoint-initdb.d/.
-- Runs once on first container init, after POSTGRES_DB is created.
-- The pytest run targets this database (routed by session.py when
-- "pytest" is in sys.argv) so tests never touch the real DB.
CREATE DATABASE indexer_utils_test;
