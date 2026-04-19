"""Common shared utilities for komari-bot."""

from .database_config import (
    DatabaseConfigSchema,
    get_shared_database_config,
    load_database_config_from_file,
)
from .embedding_migration import (
    KNOWLEDGE_MIGRATION_SPEC,
    MEMORY_MIGRATION_SPEC,
    TableMigrationResult,
    TableMigrationSpec,
    get_pool_key,
    load_embedding_config,
    migrate_table_embeddings,
    resolve_shared_database_config,
)
from .postgres import create_postgres_pool
from .vector_storage_schema import (
    apply_schema_statements,
    build_knowledge_schema_statements,
    build_memory_schema_statements,
)

__all__ = [
    "KNOWLEDGE_MIGRATION_SPEC",
    "MEMORY_MIGRATION_SPEC",
    "DatabaseConfigSchema",
    "TableMigrationResult",
    "TableMigrationSpec",
    "apply_schema_statements",
    "build_knowledge_schema_statements",
    "build_memory_schema_statements",
    "create_postgres_pool",
    "get_pool_key",
    "get_shared_database_config",
    "load_database_config_from_file",
    "load_embedding_config",
    "migrate_table_embeddings",
    "resolve_shared_database_config",
]
