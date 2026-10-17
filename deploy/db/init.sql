-- Runs once on a fresh Postgres volume. Schema for runs/jobs/etc. arrives in M1.
CREATE EXTENSION IF NOT EXISTS vector;
-- Separate database for the optional self-hosted Langfuse profile.
CREATE DATABASE langfuse;
