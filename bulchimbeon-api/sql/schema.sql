-- =====================================================================================
-- 불침번 API — 운영 DB 초기 스키마 (리비전 0001 → 0012)
--
-- 생성: `uv run alembic upgrade head --sql` (2026-08-12)
-- ⚠️ 손으로 고치지 마라. 정본은 `alembic/versions/*.py` 이고 이 파일은 그 출력이다.
--    스키마가 바뀌면 마이그레이션을 추가하고 이 파일을 **다시 생성**한다.
--
-- 검증: 빈 DB 에 이 파일을 적용한 결과가 `alembic upgrade head` 결과와
--       `pg_dump --schema-only` 기준 **바이트 단위로 동일**함을 확인했다.
--
-- -------------------------------------------------------------------------------------
-- 전제 조건
-- -------------------------------------------------------------------------------------
-- 1. PostgreSQL 18 (로컬·CI·배포를 pg18 로 통일한다 — `03 §1`)
-- 2. pgvector **0.8.0 이상**. 아래 DO 블록이 직접 막는다.
--    ⛔ 0.7 대에서는 HNSW `iterative_scan` 이 없어 **인덱스는 만들어지고 검색만 조용히
--       0건**이 된다 — 가장 나쁜 형태의 실패다.
-- 3. 접속 롤이 `CREATE EXTENSION vector` 를 실행할 수 있어야 한다.
--    매니지드 DB 는 보통 확장을 미리 허용 목록에 넣어 둔다(Railway·Supabase·Neon 확인됨).
--    권한이 없으면 관리자에게 `CREATE EXTENSION vector;` 를 먼저 요청하라.
--
-- -------------------------------------------------------------------------------------
-- 적용
-- -------------------------------------------------------------------------------------
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/schema.sql
--
-- 전체가 하나의 트랜잭션(BEGIN/COMMIT)이라 중간에 실패하면 **아무것도 남지 않는다.**
--
-- 마지막에 `alembic_version` 에 `0012` 가 기록되므로, 이후 스키마 변경은 그대로
-- `alembic upgrade head` 로 이어서 적용된다. 이 파일을 쓴 DB 와 마이그레이션으로 만든
-- DB 가 이후 경로를 공유한다는 뜻이다.
--
-- -------------------------------------------------------------------------------------
-- 적용 후 확인
-- -------------------------------------------------------------------------------------
--   SELECT extversion FROM pg_extension WHERE extname = 'vector';   -- 0.8.0 이상
--   SELECT count(*) FROM pg_tables WHERE schemaname = 'public';     -- 21
--   SELECT version_num FROM alembic_version;                        -- 0012
--   SET hnsw.iterative_scan = 'relaxed_order';                      -- 에러가 나면 버전 부족
-- =====================================================================================

BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE EXTENSION IF NOT EXISTS vector;

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

-- Running upgrade 0001 -> 0002

CREATE TABLE users (
    email TEXT NOT NULL, 
    password_hash TEXT NOT NULL, 
    name TEXT NOT NULL, 
    language TEXT NOT NULL, 
    timezone TEXT NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_users_language CHECK (language IN ('ko', 'en'))
);

CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE TABLE projects (
    name TEXT NOT NULL, 
    description TEXT, 
    invite_code TEXT NOT NULL, 
    answerer_id UUID NOT NULL, 
    away_mode BOOLEAN DEFAULT false NOT NULL, 
    settings JSONB NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(answerer_id) REFERENCES users (id)
);

CREATE INDEX ix_projects_answerer_id ON projects (answerer_id);

CREATE UNIQUE INDEX ix_projects_invite_code ON projects (invite_code);

CREATE TABLE events (
    project_id UUID NOT NULL, 
    actor_id UUID, 
    type TEXT NOT NULL, 
    entity_type TEXT, 
    entity_id UUID, 
    payload JSONB NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(actor_id) REFERENCES users (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE INDEX ix_events_project_created ON events (project_id, created_at);

CREATE TABLE guidelines (
    project_id UUID NOT NULL, 
    content TEXT NOT NULL, 
    updated_by UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (project_id), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(updated_by) REFERENCES users (id)
);

CREATE TABLE project_members (
    project_id UUID NOT NULL, 
    user_id UUID NOT NULL, 
    role TEXT NOT NULL, 
    status TEXT DEFAULT 'active' NOT NULL, 
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_project_members_role CHECK (role IN ('answerer', 'asker')), 
    CONSTRAINT ck_project_members_status CHECK (status IN ('active', 'left')), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(user_id) REFERENCES users (id), 
    CONSTRAINT uq_project_members_project_user UNIQUE (project_id, user_id)
);

CREATE INDEX ix_project_members_project_id ON project_members (project_id);

CREATE INDEX ix_project_members_user_id ON project_members (user_id);

CREATE UNIQUE INDEX uq_project_members_single_answerer ON project_members (project_id) WHERE role = 'answerer' AND status = 'active';

UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001';

-- Running upgrade 0002 -> 0003

DO $$
DECLARE ext_version text;
BEGIN
    SELECT extversion INTO ext_version FROM pg_extension WHERE extname = 'vector';
    IF ext_version IS NULL THEN
        RAISE EXCEPTION 'vector 확장이 없다. 리비전 0001 이 CREATE EXTENSION vector 를 수행한다.';
    END IF;
    IF (
        regexp_replace(split_part(ext_version, '.', 1), '[^0-9]', '', 'g')::int,
        regexp_replace(split_part(ext_version, '.', 2), '[^0-9]', '', 'g')::int
    ) < (0, 8) THEN
        RAISE EXCEPTION
            'pgvector % 은(는) 너무 낮다. 0.8.0 이상이 필요하다 (HNSW iterative_scan).',
            ext_version;
    END IF;
END $$;;

CREATE TABLE documents (
    project_id UUID NOT NULL, 
    title TEXT NOT NULL, 
    source_type TEXT NOT NULL, 
    source_ref TEXT, 
    status TEXT DEFAULT 'active' NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_documents_source_type CHECK (source_type IN ('upload', 'notion', 'github')), 
    CONSTRAINT ck_documents_status CHECK (status IN ('active', 'deleted')), 
    FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE INDEX ix_documents_project_id ON documents (project_id);

CREATE TABLE document_versions (
    document_id UUID NOT NULL, 
    version_no INTEGER NOT NULL, 
    original_filename TEXT NOT NULL, 
    mime TEXT NOT NULL, 
    storage_path TEXT NOT NULL, 
    is_active BOOLEAN DEFAULT false NOT NULL, 
    ingest_status TEXT DEFAULT 'pending' NOT NULL, 
    ingest_error TEXT, 
    uploaded_by UUID NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_document_versions_ingest_status CHECK (ingest_status IN ('pending', 'processing', 'ready', 'failed')), 
    FOREIGN KEY(document_id) REFERENCES documents (id), 
    FOREIGN KEY(uploaded_by) REFERENCES users (id), 
    CONSTRAINT uq_document_versions_document_no UNIQUE (document_id, version_no)
);

CREATE INDEX ix_document_versions_document_id ON document_versions (document_id);

CREATE UNIQUE INDEX uq_document_versions_single_active ON document_versions (document_id) WHERE is_active;

CREATE TABLE chunks (
    document_version_id UUID NOT NULL, 
    seq INTEGER NOT NULL, 
    content TEXT NOT NULL, 
    meta JSONB NOT NULL, 
    embedding VECTOR(1536) NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(document_version_id) REFERENCES document_versions (id), 
    CONSTRAINT uq_chunks_version_seq UNIQUE (document_version_id, seq)
);

CREATE INDEX ix_chunks_document_version_id ON chunks (document_version_id);

CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);

UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002';

-- Running upgrade 0003 -> 0004

CREATE TABLE questions (
    project_id UUID NOT NULL, 
    asker_id UUID NOT NULL, 
    content_ko TEXT NOT NULL, 
    content_en TEXT, 
    urgency TEXT DEFAULT 'normal' NOT NULL, 
    suggest_urgent BOOLEAN DEFAULT false NOT NULL, 
    status TEXT DEFAULT 'processing' NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_questions_urgency CHECK (urgency IN ('normal', 'urgent')), 
    CONSTRAINT ck_questions_status CHECK (status IN ('processing', 'answered', 'held', 'failed')), 
    FOREIGN KEY(asker_id) REFERENCES users (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE INDEX ix_questions_project_id ON questions (project_id);

CREATE INDEX ix_questions_asker_id ON questions (asker_id);

CREATE TABLE answers (
    question_id UUID NOT NULL, 
    grade TEXT NOT NULL, 
    matching_rate INTEGER, 
    search_score INTEGER, 
    grounding_score INTEGER, 
    sim_raw FLOAT, 
    held_reason TEXT, 
    question_struct JSONB, 
    state TEXT DEFAULT 'draft' NOT NULL, 
    content_ko TEXT DEFAULT '' NOT NULL, 
    content_en TEXT DEFAULT '' NOT NULL, 
    source TEXT DEFAULT 'generated' NOT NULL, 
    official_qa_id UUID, 
    similar_official_qa_id UUID, 
    degraded_from_red BOOLEAN DEFAULT false NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE, 
    verified_at TIMESTAMP WITH TIME ZONE, 
    verified_by UUID, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_answers_grade CHECK (grade IN ('green', 'yellow', 'red')), 
    CONSTRAINT ck_answers_state CHECK (state IN ('draft', 'verified', 'under_review', 'expired', 'rejected')), 
    CONSTRAINT ck_answers_source CHECK (source IN ('generated', 'reused')), 
    CONSTRAINT ck_answers_held_reason CHECK (held_reason IS NULL OR held_reason IN ('conflict', 'no_evidence', 'low_confidence', 'schema_failed', 'quota_exceeded')), 
    FOREIGN KEY(question_id) REFERENCES questions (id), 
    FOREIGN KEY(verified_by) REFERENCES users (id), 
    CONSTRAINT uq_answers_question UNIQUE (question_id)
);

CREATE TABLE official_qas (
    project_id UUID NOT NULL, 
    question_ko TEXT NOT NULL, 
    question_en TEXT NOT NULL, 
    answer_ko TEXT NOT NULL, 
    answer_en TEXT NOT NULL, 
    question_embedding VECTOR(1536) NOT NULL, 
    source_answer_id UUID NOT NULL, 
    status TEXT DEFAULT 'active' NOT NULL, 
    correct_count INTEGER DEFAULT 0 NOT NULL, 
    reuse_count INTEGER DEFAULT 0 NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_official_qas_status CHECK (status IN ('active', 'under_review', 'archived')), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(source_answer_id) REFERENCES answers (id)
);

CREATE INDEX ix_official_qas_project_id ON official_qas (project_id);

CREATE INDEX ix_official_qas_question_embedding_hnsw ON official_qas USING hnsw (question_embedding vector_cosine_ops);

ALTER TABLE answers ADD CONSTRAINT fk_answers_official_qa_id FOREIGN KEY(official_qa_id) REFERENCES official_qas (id);

ALTER TABLE answers ADD CONSTRAINT fk_answers_similar_official_qa_id FOREIGN KEY(similar_official_qa_id) REFERENCES official_qas (id);

CREATE TABLE answer_citations (
    answer_id UUID NOT NULL, 
    chunk_id UUID, 
    official_qa_id UUID, 
    quote TEXT NOT NULL, 
    similarity FLOAT NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(answer_id) REFERENCES answers (id), 
    FOREIGN KEY(chunk_id) REFERENCES chunks (id), 
    FOREIGN KEY(official_qa_id) REFERENCES official_qas (id)
);

CREATE INDEX ix_answer_citations_answer_id ON answer_citations (answer_id);

UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003';

-- Running upgrade 0004 -> 0005

CREATE TABLE review_cards (
    project_id UUID NOT NULL, 
    question_id UUID NOT NULL, 
    answer_id UUID, 
    reason TEXT NOT NULL, 
    document_version_id UUID, 
    status TEXT DEFAULT 'pending' NOT NULL, 
    resolution TEXT, 
    question_struct JSONB, 
    recommend_approve BOOLEAN DEFAULT false NOT NULL, 
    is_urgent BOOLEAN DEFAULT false NOT NULL, 
    first_viewed_at TIMESTAMP WITH TIME ZONE, 
    deferred_until TIMESTAMP WITH TIME ZONE, 
    resolved_at TIMESTAMP WITH TIME ZONE, 
    resolved_by UUID, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_review_cards_reason CHECK (reason IN ('green', 'yellow', 'red', 'feedback', 'doc_update', 'failed')), 
    CONSTRAINT ck_review_cards_status CHECK (status IN ('pending', 'deferred', 'resolved')), 
    CONSTRAINT ck_review_cards_resolution CHECK (resolution IS NULL OR resolution IN ('approved', 'edited', 'rejected', 'kept')), 
    FOREIGN KEY(answer_id) REFERENCES answers (id), 
    FOREIGN KEY(document_version_id) REFERENCES document_versions (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(question_id) REFERENCES questions (id), 
    FOREIGN KEY(resolved_by) REFERENCES users (id)
);

CREATE INDEX ix_review_cards_question_id ON review_cards (question_id);

CREATE INDEX ix_review_cards_answer_id ON review_cards (answer_id);

CREATE INDEX ix_review_cards_project_id_status ON review_cards (project_id, status);

CREATE TABLE feedbacks (
    answer_id UUID NOT NULL, 
    user_id UUID NOT NULL, 
    verdict TEXT NOT NULL, 
    note TEXT, 
    resolved BOOLEAN DEFAULT false NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_feedbacks_verdict CHECK (verdict IN ('correct', 'different')), 
    FOREIGN KEY(answer_id) REFERENCES answers (id), 
    FOREIGN KEY(user_id) REFERENCES users (id), 
    CONSTRAINT uq_feedbacks_answer_user UNIQUE (answer_id, user_id)
);

CREATE INDEX ix_feedbacks_answer_id ON feedbacks (answer_id);

UPDATE alembic_version SET version_num='0005' WHERE alembic_version.version_num = '0004';

-- Running upgrade 0005 -> 0006

CREATE TABLE notifications (
    user_id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    type TEXT NOT NULL, 
    title TEXT NOT NULL, 
    body TEXT NOT NULL, 
    payload JSONB NOT NULL, 
    deliver_after TIMESTAMP WITH TIME ZONE, 
    read_at TIMESTAMP WITH TIME ZONE, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_notifications_type CHECK (type IN ('answer.completed', 'answer.failed', 'answer.verified', 'answer.corrected', 'answer.kept', 'answer.rejected', 'card.created', 'briefing.ready', 'doc.review_needed', 'feedback.different', 'sync.completed', 'sync.failed')), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_notifications_user_id_read_at ON notifications (user_id, read_at);

CREATE INDEX ix_notifications_user_id_deliver_after ON notifications (user_id, deliver_after);

UPDATE alembic_version SET version_num='0006' WHERE alembic_version.version_num = '0005';

-- Running upgrade 0006 -> 0007

CREATE TABLE lessons (
    project_id UUID NOT NULL, 
    content TEXT NOT NULL, 
    content_hash TEXT NOT NULL, 
    status TEXT DEFAULT 'candidate' NOT NULL, 
    needs_recheck BOOLEAN DEFAULT false NOT NULL, 
    source_answer_id UUID, 
    last_used_at TIMESTAMP WITH TIME ZONE, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_lessons_status CHECK (status IN ('candidate', 'approved', 'deleted')), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(source_answer_id) REFERENCES answers (id)
);

CREATE INDEX ix_lessons_project_id_status ON lessons (project_id, status);

CREATE INDEX ix_lessons_project_id_content_hash ON lessons (project_id, content_hash);

CREATE TABLE briefing_runs (
    project_id UUID NOT NULL, 
    run_date DATE NOT NULL, 
    sent_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    CONSTRAINT uq_briefing_runs_project_date UNIQUE (project_id, run_date)
);

UPDATE alembic_version SET version_num='0007' WHERE alembic_version.version_num = '0006';

-- Running upgrade 0007 -> 0008

ALTER TABLE chunks ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE events ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE answer_citations ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE answer_citations ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE answers ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE answers ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE briefing_runs ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE briefing_runs ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE document_versions ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE document_versions ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE documents ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE documents ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE feedbacks ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE feedbacks ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE guidelines ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE guidelines ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE lessons ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE lessons ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE notifications ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE notifications ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE official_qas ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE official_qas ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE project_members ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE project_members ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE projects ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE projects ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE questions ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE questions ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE review_cards ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE review_cards ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

ALTER TABLE users ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE users ALTER COLUMN updated_at SET DEFAULT clock_timestamp();

UPDATE alembic_version SET version_num='0008' WHERE alembic_version.version_num = '0007';

-- Running upgrade 0008 -> 0009

CREATE TABLE integrations (
    project_id UUID NOT NULL, 
    provider TEXT NOT NULL, 
    config JSONB NOT NULL, 
    last_synced_at TIMESTAMP WITH TIME ZONE, 
    last_sync_status TEXT, 
    id UUID NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_integrations_provider CHECK (provider IN ('notion', 'github')), 
    CONSTRAINT ck_integrations_last_sync_status CHECK (last_sync_status IN ('ok', 'failed')), 
    FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE INDEX ix_integrations_project_id ON integrations (project_id);

UPDATE alembic_version SET version_num='0009' WHERE alembic_version.version_num = '0008';

-- Running upgrade 0009 -> 0010

CREATE UNIQUE INDEX uq_documents_source_ref ON documents (project_id, source_type, source_ref) WHERE source_ref IS NOT NULL;

UPDATE alembic_version SET version_num='0010' WHERE alembic_version.version_num = '0009';

-- Running upgrade 0010 -> 0011

CREATE TABLE llm_usage (
    project_id UUID NOT NULL, 
    user_id UUID, 
    question_id UUID, 
    step TEXT NOT NULL, 
    model TEXT NOT NULL, 
    input_tokens INTEGER NOT NULL, 
    output_tokens INTEGER NOT NULL, 
    reasoning_tokens INTEGER NOT NULL, 
    cost_usd NUMERIC(12, 6) NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(project_id) REFERENCES projects (id), 
    FOREIGN KEY(question_id) REFERENCES questions (id), 
    FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_llm_usage_project_created ON llm_usage (project_id, created_at);

CREATE INDEX ix_llm_usage_user_created ON llm_usage (user_id, created_at);

UPDATE alembic_version SET version_num='0011' WHERE alembic_version.version_num = '0010';

-- Running upgrade 0011 -> 0012

CREATE TABLE job_runs (
    job_name TEXT NOT NULL, 
    trigger TEXT NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    status TEXT NOT NULL, 
    processed_count INTEGER NOT NULL, 
    error TEXT, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_job_runs_status CHECK (status IN ('ok', 'failed')), 
    CONSTRAINT ck_job_runs_trigger CHECK (trigger IN ('interval', 'startup'))
);

CREATE INDEX ix_job_runs_job_name_started ON job_runs (job_name, started_at);

UPDATE alembic_version SET version_num='0012' WHERE alembic_version.version_num = '0011';

COMMIT;

