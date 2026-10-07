-- Schema dumped from data/phase3-demo.db (sqlite_master), SQLite 3.45.3
-- schema version 2: 4 core tables + 2 FTS5 search indexes + 6 sync triggers

-- table: memories
CREATE TABLE memories (
    id                 TEXT    PRIMARY KEY,
    type               TEXT    NOT NULL
                               CHECK (type IN ('knowledge', 'experience', 'event', 'profile')),
    title              TEXT    NOT NULL CHECK (length(trim(title)) > 0),
    content            TEXT    NOT NULL CHECK (length(trim(content)) > 0),
    summary            TEXT,
    tags_json          TEXT    NOT NULL DEFAULT '[]'
                               CHECK (json_valid(tags_json) AND json_type(tags_json) = 'array'),
    importance         REAL    NOT NULL DEFAULT 0.5
                               CHECK (typeof(importance) IN ('integer', 'real')
                                      AND importance >= 0.0 AND importance <= 1.0),
    confidence         REAL    NOT NULL DEFAULT 0.5
                               CHECK (typeof(confidence) IN ('integer', 'real')
                                      AND confidence >= 0.0 AND confidence <= 1.0),
    information_origin TEXT    NOT NULL
                               CHECK (information_origin IN ('user_explicit', 'source_content', 'agent_inference')),
    status             TEXT    NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'pending', 'archived')),
    created_at         TEXT    NOT NULL CHECK (length(created_at) > 0),
    updated_at         TEXT    NOT NULL CHECK (length(updated_at) > 0),
    schema_version     INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
);

-- table: memory_fts_trigram
CREATE VIRTUAL TABLE memory_fts_trigram USING fts5(memory_id UNINDEXED, title, content, summary, tags, tokenize='trigram');

-- table: memory_fts_word
CREATE VIRTUAL TABLE memory_fts_word USING fts5(memory_id UNINDEXED, title, content, summary, tags, tokenize='unicode61');

-- table: memory_sources
CREATE TABLE memory_sources (
    memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    source_id  TEXT NOT NULL REFERENCES sources(id)  ON DELETE CASCADE,
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    PRIMARY KEY (memory_id, source_id)
);

-- table: schema_migrations
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
);

-- table: sources
CREATE TABLE sources (
    id            TEXT    PRIMARY KEY,
    source_type   TEXT    NOT NULL
                          CHECK (source_type IN ('text', 'chat', 'article', 'web', 'file')),
    title         TEXT    NOT NULL CHECK (length(trim(title)) > 0),
    content       TEXT    NOT NULL CHECK (length(trim(content)) > 0),
    url           TEXT    CHECK (url IS NULL OR url LIKE 'http%'),
    content_hash  TEXT    NOT NULL UNIQUE
                          CHECK (length(content_hash) = 64 AND content_hash = lower(content_hash)),
    metadata_json TEXT    NOT NULL DEFAULT '{}'
                          CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    created_at    TEXT    NOT NULL CHECK (length(created_at) > 0),
    updated_at    TEXT    NOT NULL CHECK (length(updated_at) > 0)
);

-- index: idx_memories_created_at
CREATE INDEX idx_memories_created_at ON memories (created_at);

-- index: idx_memories_importance
CREATE INDEX idx_memories_importance ON memories (importance);

-- index: idx_memories_status_created
CREATE INDEX idx_memories_status_created ON memories (status, created_at);

-- index: idx_memories_type_status
CREATE INDEX idx_memories_type_status ON memories (type, status);

-- index: idx_memory_sources_source
CREATE INDEX idx_memory_sources_source ON memory_sources (source_id, memory_id);

-- index: idx_sources_created_at
CREATE INDEX idx_sources_created_at ON sources (created_at);

-- index: idx_sources_type_created
CREATE INDEX idx_sources_type_created ON sources (source_type, created_at);

-- trigger: memory_fts_trigram_ad
CREATE TRIGGER memory_fts_trigram_ad AFTER DELETE ON memories BEGIN DELETE FROM memory_fts_trigram WHERE memory_id = old.id; END;

-- trigger: memory_fts_trigram_ai
CREATE TRIGGER memory_fts_trigram_ai AFTER INSERT ON memories BEGIN INSERT INTO memory_fts_trigram (memory_id, title, content, summary, tags) VALUES (new.id, new.title, new.content, COALESCE(new.summary, ''), COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags_json)), '')); END;

-- trigger: memory_fts_trigram_au
CREATE TRIGGER memory_fts_trigram_au AFTER UPDATE ON memories BEGIN DELETE FROM memory_fts_trigram WHERE memory_id = old.id; INSERT INTO memory_fts_trigram (memory_id, title, content, summary, tags) VALUES (new.id, new.title, new.content, COALESCE(new.summary, ''), COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags_json)), '')); END;

-- trigger: memory_fts_word_ad
CREATE TRIGGER memory_fts_word_ad AFTER DELETE ON memories BEGIN DELETE FROM memory_fts_word WHERE memory_id = old.id; END;

-- trigger: memory_fts_word_ai
CREATE TRIGGER memory_fts_word_ai AFTER INSERT ON memories BEGIN INSERT INTO memory_fts_word (memory_id, title, content, summary, tags) VALUES (new.id, new.title, new.content, COALESCE(new.summary, ''), COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags_json)), '')); END;

-- trigger: memory_fts_word_au
CREATE TRIGGER memory_fts_word_au AFTER UPDATE ON memories BEGIN DELETE FROM memory_fts_word WHERE memory_id = old.id; INSERT INTO memory_fts_word (memory_id, title, content, summary, tags) VALUES (new.id, new.title, new.content, COALESCE(new.summary, ''), COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags_json)), '')); END;

-- schema_migrations rows
-- 1 | initial_schema | 2026-10-04T14:20:17.020Z
-- 2 | memory_search_index | 2026-10-04T14:20:17.020Z