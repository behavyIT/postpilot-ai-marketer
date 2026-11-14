-- Выполняется один раз при первом создании тома PostgreSQL.
-- Отдельная база для автотестов и расширение pgvector в обеих базах.
CREATE DATABASE aimarketer_test OWNER aimarketer;

CREATE EXTENSION IF NOT EXISTS vector;

\connect aimarketer_test
CREATE EXTENSION IF NOT EXISTS vector;
