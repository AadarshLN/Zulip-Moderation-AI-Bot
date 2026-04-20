-- ChatSentry PostgreSQL Schema
-- Phase 1: Infrastructure & Ingestion

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Users table (D-04: UUIDs, source tracking, timestamps)
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(255) NOT NULL UNIQUE,
    email VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    source VARCHAR(32) NOT NULL DEFAULT 'real'
        CHECK (source IN ('real', 'synthetic'))
);

-- Messages table (D-01, D-02, D-03, D-04)
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    cleaned_text TEXT,
    is_toxicity BOOLEAN DEFAULT FALSE,
    is_suicide BOOLEAN DEFAULT FALSE,
    source VARCHAR(32) NOT NULL DEFAULT 'real'
        CHECK (source IN ('real', 'synthetic')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- GIN full-text search index on messages.text (D-03)
CREATE INDEX IF NOT EXISTS idx_messages_text_fts
    ON messages USING GIN (to_tsvector('english', text));

-- Flags table (D-01, D-04)
CREATE TABLE IF NOT EXISTS flags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES messages(id),
    flagged_by UUID REFERENCES users(id),
    reason TEXT,
    is_verified BOOLEAN DEFAULT FALSE,
    source VARCHAR(32) NOT NULL DEFAULT 'real'
        CHECK (source IN ('real', 'synthetic')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Moderation table (D-01, D-04)
CREATE TABLE IF NOT EXISTS moderation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES messages(id),
    action VARCHAR(50) NOT NULL,
    confidence FLOAT,
    model_version VARCHAR(100),
    source VARCHAR(32) NOT NULL DEFAULT 'real'
        CHECK (source IN ('real', 'synthetic')),
    decided_at TIMESTAMPTZ DEFAULT NOW()
);
